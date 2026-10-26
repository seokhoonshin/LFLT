"""M1 tests: Excel loading, header mapping, row validation, geocode fill, consolidation."""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

from openpyxl import Workbook
from routeopt.domain import DayTag, OrderType, ShipmentType
from routeopt.ingest.consolidate import consolidate, stop_tier
from routeopt.ingest.excel_loader import (
    CANONICAL_COLUMNS,
    RawRow,
    apply_mapping,
    auto_map_columns,
    load_rows,
    normalize_header,
    read_table,
)
from routeopt.ingest.validator import (
    normalize_address_key,
    parse_date,
    parse_zip,
    validate_rows,
)

from tests.factories import NEAR_LAT, NEAR_LON, PLAN_DATE, make_settings, make_shipment


def _row(n: int, **values: object) -> RawRow:
    base: dict[str, object] = {
        "shipment_id": f"SH{n:04d}_LMD",
        "purchase_order": "PO1",
        "delivery_date": PLAN_DATE,
        "order_type": "OBS",
        "customer_name": "Jane Doe",
        "phone": "(909) 555-0100",
        "address": "100 Main St",
        "city": "Rialto",
        "state": "CA",
        "zip": "92377",
        "latitude": NEAR_LAT,
        "longitude": NEAR_LON,
        "model_code": "OLED65C4PUA",
        "pieces": 1,
        "weight_lb": 60,
        "volume_cuft": 10,
        "service_minutes": 60,
        "notes": None,
    }
    base.update(values)
    return RawRow(row_number=n, values=base)


def _workbook_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---- loader ---------------------------------------------------------------------------


def test_normalize_header_is_case_and_whitespace_insensitive() -> None:
    assert normalize_header("  Shipment_ID ") == "shipment id"
    assert normalize_header("Volume (cuft)") == "volume cuft"


def test_auto_map_accepts_aliases_and_reports_missing() -> None:
    headers = [
        "Shipment ID",
        "PO",
        "Delivery Date",
        "Order Type",
        "Customer",
        "Street",
        "City",
        "ST",
        "Zip Code",
        "Lat",
        "Lng",
        "Model",
        "Qty",
        "Weight",
        "Volume",
        "Service Min",
        "Comments",
    ]
    m = auto_map_columns(headers)
    assert m.complete, m.missing_required
    assert m.mapping["zip"] == "Zip Code"
    assert m.mapping["longitude"] == "Lng"
    assert m.mapping["service_minutes"] == "Service Min"
    m2 = auto_map_columns(["Shipment ID", "Customer"])
    assert "delivery_date" in m2.missing_required and not m2.complete


def test_read_table_skips_blank_rows_and_keeps_row_numbers() -> None:
    data = _workbook_bytes(
        ["shipment_id", "customer_name"], [["A_LMD", "x"], [None, None], ["B_LMD", "y"]]
    )
    table = read_table(data)
    assert table.headers == ["shipment_id", "customer_name"]
    assert table.row_numbers == [2, 4]
    rows = apply_mapping(table, auto_map_columns(table.headers).mapping)
    assert rows[1].get("shipment_id") == "B_LMD" and rows[1].row_number == 4
    assert rows[1].get("volume_cuft") is None  # absent column projected as None


def test_read_csv() -> None:
    csv = b"shipment_id,customer_name\nA_LMD,x\n\nB_LMD,y\n"
    table = read_table(csv)
    assert [r["shipment_id"] for r in table.rows] == ["A_LMD", "B_LMD"]


def test_load_rows_with_manual_mapping_override(tmp_path: Path) -> None:
    data = _workbook_bytes(["Ref", "Who"], [["A_LMD", "x"]])
    p = tmp_path / "t.xlsx"
    p.write_bytes(data)
    _table, mapping, rows = load_rows(p, mapping={"shipment_id": "Ref", "customer_name": "Who"})
    assert mapping.mapping["shipment_id"] == "Ref"
    assert rows[0].get("customer_name") == "x"


# ---- field parsers ------------------------------------------------------------------


def test_parse_date_variants() -> None:
    assert parse_date("2026-09-11") == dt.date(2026, 9, 11)
    assert parse_date("9/11/2026") == dt.date(2026, 9, 11)
    assert parse_date(dt.datetime(2026, 9, 11, 10)) == dt.date(2026, 9, 11)
    assert parse_date(46276) == dt.date(2026, 9, 11)  # Excel serial


def test_parse_zip_keeps_leading_zeros() -> None:
    assert parse_zip(2134) == "02134"
    assert parse_zip(92377.0) == "92377"
    assert parse_zip("92377-1234") == "92377-1234"
    assert normalize_address_key("  100  main st. ", "92377-1234") == "100 MAIN ST|92377"


# ---- validation ---------------------------------------------------------------------


def test_valid_rows_become_shipments() -> None:
    rows = [
        _row(2),
        _row(3, shipment_id="SH0003_RTN", volume_cuft=50, weight_lb=200, service_minutes=20),
        _row(
            4,
            shipment_id="SH0004_INC",
            pieces=0,
            volume_cuft=None,
            weight_lb=None,
            model_code=None,
            service_minutes=30,
        ),
    ]
    res = validate_rows(rows, PLAN_DATE, make_settings())
    assert res.ok, res.errors
    assert [s.shipment_type for s in res.shipments] == [
        ShipmentType.LMD,
        ShipmentType.RTN,
        ShipmentType.INC,
    ]
    assert res.shipments[1].pickup_volume == 50 and res.shipments[1].delivery_volume == 0
    assert res.shipments[2].volume_cuft == 0 and res.shipments[2].day_tag == DayTag.D


def test_hard_errors_are_reported_per_row() -> None:
    rows = [
        _row(2, shipment_id="SH0002_XYZ"),  # bad suffix
        _row(3, order_type="RETAIL"),  # unknown order type
        _row(4, delivery_date="not a date"),
        _row(5, volume_cuft=-5),
        _row(6, volume_cuft=650),  # bigger than a truck
        _row(7, pieces="two"),
        _row(8, shipment_id="SH0009_LMD"),
        _row(9, shipment_id="SH0009_LMD"),  # duplicate
        _row(10, service_minutes=None),
        _row(11, customer_name=""),
    ]
    res = validate_rows(rows, PLAN_DATE, make_settings())
    assert not res.ok
    by_row = {e.row: e.message for e in res.errors}
    assert "_LMD, _RTN or _INC" in by_row[2]
    assert "Unknown order type" in by_row[3]
    assert "not a date" in by_row[4]
    assert "negative" in by_row[5]
    assert "larger than one truck" in by_row[6]
    assert "whole number" in by_row[7]
    assert "Duplicate" in by_row[9] and 8 not in by_row
    assert "Service minutes is missing" in by_row[10]
    assert "Customer name is missing" in by_row[11]
    assert res.errors == sorted(res.errors, key=lambda e: e.row)
    table = res.issues_table("error")
    assert table[0]["Row"] == 2 and "Problem" in table[0]


def test_warnings_do_not_block() -> None:
    rows = [
        _row(2, delivery_date=PLAN_DATE + dt.timedelta(days=5)),  # outside D..D+2 -> excluded
        _row(3, shipment_id="SH0003_INC", volume_cuft=12, weight_lb=5, pieces=0),  # INC with volume
        _row(4, latitude=38.58, longitude=-121.49),  # Sacramento: ~380 miles from the hub
        _row(5, shipment_id="SH0005_RTN", volume_cuft=0),  # return with no volume
    ]
    res = validate_rows(rows, PLAN_DATE, make_settings())
    assert res.ok
    assert [s.source_row for s in res.shipments] == [3, 4, 5]
    msgs = " | ".join(w.message for w in res.warnings)
    assert (
        "outside D..D+2" in msgs
        and "treated as 0 cu ft" in msgs
        and "miles from the hub" in msgs
        and "nothing to pick up" in msgs
    )
    assert res.shipments[0].volume_cuft == 0.0


def test_day_tags_relative_to_plan_date() -> None:
    rows = [
        _row(2),
        _row(3, delivery_date=PLAN_DATE + dt.timedelta(days=1)),
        _row(4, delivery_date=PLAN_DATE + dt.timedelta(days=2)),
    ]
    res = validate_rows(rows, PLAN_DATE, make_settings())
    assert [s.day_tag for s in res.shipments] == [DayTag.D, DayTag.D1, DayTag.D2]


def test_geocode_fill_and_failures(fake_geocoder) -> None:  # type: ignore[no-untyped-def]
    rows = [
        _row(2, latitude=None, longitude=None),
        _row(3, latitude=None, longitude=None, address="1 Nowhere Ln"),
        _row(4, latitude="abc", longitude=None),  # unusable -> geocode
    ]
    res = validate_rows(rows, PLAN_DATE, make_settings(), geocoder=fake_geocoder)
    assert fake_geocoder.calls == 1
    assert sorted(res.geocoded_rows) == [2, 4]
    assert [e.row for e in res.errors] == [3]
    assert "could not be geocoded" in res.errors[0].message
    # planner chooses to exclude the failing row and continue
    res2 = validate_rows(rows, PLAN_DATE, make_settings(), geocoder=fake_geocoder, exclude_rows={3})
    assert res2.ok and len(res2.shipments) == 2


def test_missing_coordinates_without_geocoder_is_error() -> None:
    res = validate_rows([_row(2, latitude=None, longitude=None)], PLAN_DATE, make_settings())
    assert not res.ok and "no geocoder" in res.errors[0].message


# ---- consolidation (acceptance criterion 1) -----------------------------------------


def test_grouping_three_shipments_one_address_one_stop() -> None:
    settings = make_settings()
    ships = [
        make_shipment(
            "LMD", address="500 Oak Dr", volume=70, service=45, po="PO-A", order_type="OBS"
        ),
        make_shipment(
            "RTN", address="500  oak dr.", volume=60, service=20, po="PO-B", order_type="OBS"
        ),
        make_shipment(
            "INC", address="500 OAK DR", service=30, po="PO-C", order_type="BUILDER", day_offset=1
        ),
    ]
    result = consolidate(ships, settings)
    assert len(result.stops) == 1
    stop = result.stops[0]
    assert stop.service_total_minutes == 45 + 20 + 30 + settings.stop_buffer_minutes
    assert stop.delivery_volume == 70 and stop.pickup_volume == 60
    assert stop.day_tag == DayTag.D  # contains a D shipment, so it is a D stop
    assert stop.tier == 1  # contains an OBS shipment
    assert sorted(stop.purchase_orders) == ["PO-A", "PO-B", "PO-C"]
    assert stop.pieces_in == 1 and stop.pieces_out == 1
    assert result.summary == "3 shipments -> 1 stops"


def test_stop_tiers() -> None:
    assert stop_tier(DayTag.D, [OrderType.LTL, OrderType.OBS]) == 1
    assert stop_tier(DayTag.D, [OrderType.BUILDER, OrderType.LTL]) == 2
    assert stop_tier(DayTag.D, [OrderType.LTL]) == 3
    assert stop_tier(DayTag.D1, [OrderType.OBS]) == 4
    assert stop_tier(DayTag.D2, [OrderType.OBS]) == 5


def test_consolidation_warnings() -> None:
    settings = make_settings()
    ships = [
        make_shipment(
            address="1 A St", lat=NEAR_LAT + 0.01, lon=NEAR_LON - 0.01, service=250
        ),  # long service
        make_shipment(
            address="1 A Street", lat=NEAR_LAT + 0.01, lon=NEAR_LON - 0.01
        ),  # same coords, different text
        make_shipment(address="2 B St", lat=NEAR_LAT + 0.02, lon=NEAR_LON - 0.01),
    ]
    result = consolidate(ships, settings)
    assert len(result.stops) == 3
    msgs = " | ".join(w.message for w in result.warnings)
    assert "minutes of service" in msgs and "same coordinates" in msgs


def test_sample_workbook_round_trips_through_loader(sample_workbook: Path, fake_geocoder) -> None:  # type: ignore[no-untyped-def]
    table, mapping, rows = load_rows(sample_workbook)
    assert table.headers == CANONICAL_COLUMNS
    assert mapping.complete
    res = validate_rows(rows, PLAN_DATE, make_settings(), geocoder=fake_geocoder)
    assert res.ok, res.errors[:3]
    assert len(res.shipments) == 120
    assert res.geocoded_rows  # blank lat/lon rows were filled
    cons = consolidate(res.shipments, make_settings())
    assert len(cons.stops) < 120
    assert max(s.delivery_volume for s in cons.stops) >= 550  # the big builder stop
    types = {s.shipment_type for s in res.shipments}
    assert types == {ShipmentType.LMD, ShipmentType.RTN, ShipmentType.INC}
    assert {s.day_tag for s in res.shipments} == {DayTag.D, DayTag.D1, DayTag.D2}


def test_address_mode_geocodes_rows_that_already_have_coordinates(fake_geocoder) -> None:  # type: ignore[no-untyped-def]
    rows = [
        _row(2, latitude=33.0, longitude=-117.0),  # e.g. a ZIP centroid in the file
        _row(3, latitude=33.0, longitude=-117.0, address="1 Nowhere Ln"),
    ]
    settings = make_settings(geocode_mode="address")
    res = validate_rows(rows, PLAN_DATE, settings, geocoder=fake_geocoder)
    assert res.ok and res.geocoded_rows == [2]
    assert (res.shipments[0].latitude, res.shipments[0].longitude) != (33.0, -117.0)
    # unmatched address keeps the file's coordinates, with a warning instead of an error
    assert (res.shipments[1].latitude, res.shipments[1].longitude) == (33.0, -117.0)
    assert any(w.row == 3 and "from the file" in w.message for w in res.warnings)


def test_fill_mode_keeps_file_coordinates(fake_geocoder) -> None:  # type: ignore[no-untyped-def]
    rows = [_row(2, latitude=33.0, longitude=-117.0)]
    res = validate_rows(rows, PLAN_DATE, make_settings(geocode_mode="fill"), geocoder=fake_geocoder)
    assert fake_geocoder.calls == 0 and res.geocoded_rows == []
    assert (res.shipments[0].latitude, res.shipments[0].longitude) == (33.0, -117.0)
