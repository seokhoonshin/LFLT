"""Analyze page: Plan 7774 aging uploads, their pivots and the stored snapshots."""

from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import pytest
from routeopt.analyze import (
    aging_pivot,
    detect_level,
    in_pg,
    pg_number,
    prepare,
    read_upload,
    snapshot_day,
    summary_table,
    to_records,
)
from routeopt.db.repo import Repository


def _ship_frame(day: str = "2026-10-30") -> pd.DataFrame:
    return pd.DataFrame(
        [
            # load, origin, status, pg, volume, order value, drop bucket, rdd bucket
            ("L1", "LGC2", "Hold", "PG20", 100.0, 1000.0, "0-3", "Before RDD"),
            ("L1", "LGC2", "Hold", "PG20", 50.0, 500.0, "8-14", "0-3 past"),
            ("L2", "LGC2", "Requested", "PG30", 30.0, 300.0, "4-7", "Before RDD"),
            ("L3", "LGC7", "Hold", "PG50", 70.0, 700.0, "31+", "15+ past"),
        ],
        columns=[
            "LOAD_ID",
            "ORIGIN_LOCATION_ID",
            "LOAD_STATUS",
            "PLAN_ID_GRP",
            "VOLUME",
            "ORDER_VALUE",
            "DROP_AGING_BUCKET",
            "RDD_AGING_BUCKET",
        ],
    ).assign(SHIPMENT_ID=["S1", "S2", "S3", "S4"], SNAPSHOT_DATE=pd.Timestamp(day))


def _xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def test_upload_is_read_and_its_level_detected() -> None:
    raw = _ship_frame()
    raw.columns = [c.lower() for c in raw.columns]  # any case
    df = read_upload(_xlsx(raw), "aging.xlsx")
    assert detect_level(df) == "shipment"
    assert detect_level(df.drop(columns=["SHIPMENT_ID"])) == "load"
    assert snapshot_day(df, dt.date(2000, 1, 1)) == dt.date(2026, 10, 30)
    assert snapshot_day(df.drop(columns=["SNAPSHOT_DATE"]), dt.date(2000, 1, 1)) == dt.date(
        2000, 1, 1
    )
    with pytest.raises(ValueError, match="not the aging query"):
        read_upload(_xlsx(pd.DataFrame({"A": [1]})), "x.xlsx")


def test_pivots_match_the_rows() -> None:
    df = prepare(to_records(_ship_frame()))
    assert pg_number("PG20") == "20" and pg_number(30.0) == "30" and pg_number("20, 30") == "20, 30"
    t = summary_table(df, "shipment").set_index(["Origin", "Status"])
    assert t.loc[("LGC2", "Hold"), "Volume"] == 150 and t.loc[("LGC2", "Hold"), "Loads"] == 1
    assert t.loc[("LGC2", "Requested"), "Order Value"] == 300
    assert t.loc[("LGC7", "Requested"), "Volume"] == 0
    assert t.loc[("Grand Total", ""), "Volume"] == 250 and t.loc[("Grand Total", ""), "Loads"] == 3
    assert t.loc[("Grand Total", ""), "Shipments"] == 4
    pg20 = aging_pivot(in_pg(df, {"20"}), "RDD_AGING_BUCKET")
    assert pg20.loc[("LGC2", "Hold"), "Before RDD"] == 100
    assert pg20.loc[("LGC2", "Hold"), "0-3 past"] == 50
    pg3050 = aging_pivot(in_pg(df, {"30", "50"}), "DROP_AGING_BUCKET")
    assert pg3050.loc[("LGC7", "Hold"), "31+"] == 70
    assert pg3050.loc[("Grand Total", ""), "Grand Total"] == 100


def test_snapshots_are_kept_per_day_and_replaced(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo = Repository(f"sqlite:///{tmp_path / 'a.db'}")
    repo.init_schema()
    d1, d2 = dt.date(2026, 10, 29), dt.date(2026, 10, 30)
    repo.save_aging("shipment", d1, to_records(_ship_frame("2026-10-29")), "a.xlsx")
    repo.save_aging("shipment", d2, to_records(_ship_frame()), "b.xlsx")
    repo.save_aging("shipment", d2, to_records(_ship_frame().head(2)), "c.xlsx")  # replaces
    assert [(d, n) for d, n, _ in repo.list_aging("shipment")] == [(d2, 2), (d1, 4)]
    assert repo.list_aging("load") == []
    df = prepare(repo.load_aging("shipment"))
    assert set(df["SNAPSHOT_DATE"]) == {d1, d2} and len(df) == 6
    repo.delete_aging("shipment", d1)
    assert [d for d, _n, _ in repo.list_aging("shipment")] == [d2]
