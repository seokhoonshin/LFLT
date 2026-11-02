"""Analyze page: the LFLT aging summary and the LFLT performance uploads."""

from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import pytest
from routeopt.analyze import aging_frame, aging_table, dash, read_aging_summary, to_records
from routeopt.db.repo import Repository


def _summary_frame(as_of: str = "2026-10-30") -> pd.DataFrame:
    """Shaped like the LFLT Aging summary query result (text, '-' for 0)."""
    cols = [
        "AS_OF",
        "Plan Group",
        "Status",
        "Volume",
        "0d (Vol)",
        "1d (Vol)",
        "2d (Vol)",
        "3d (Vol)",
        "4d+ (Vol)",
        "RDD Passed (Vol)",
        "RDD Not Passed (Vol)",
        "Total Vol",
    ]
    rows = [
        [as_of, "PG30,50,80", "Hold", "1,015", "62", "200", "185", "14", "553", None, None, "1,015"],
        [as_of, None, "Requested", "466", "90", "14", "362", "-", "-", None, None, "466"],
        [as_of, None, "Total", "1,481", "151", "215", "547", "14", "553", None, None, "1,481"],
        [as_of, "PG20", "Hold", "-", None, None, None, None, None, "-", "-", "-"],
        [as_of, None, "Requested", "20", None, None, None, None, None, "-", "20", "20"],
        [as_of, None, "Total", "20", None, None, None, None, None, "-", "20", "20"],
    ]  # fmt: skip
    return pd.DataFrame(rows, columns=cols)


def _xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def test_aging_summary_is_read_like_the_shared_table() -> None:
    day, df = read_aging_summary(_xlsx(_summary_frame()), "a.xlsx", dt.date(2000, 1, 1))
    assert day == dt.date(2026, 10, 30)
    t = df.set_index(["PLAN_GROUP", "STATUS"])
    assert t.loc[("PG30,50,80", "Hold"), "VOLUME"] == 1015  # "1,015"
    assert t.loc[("PG30,50,80", "Requested"), "D3"] == 0  # "-"
    assert t.loc[("PG30,50,80", "Requested"), "D2"] == 362  # blank Plan Group = same group
    assert t.loc[("PG20", "Requested"), "RDD_NOT_PASSED"] == 20
    stored = aging_frame([r | {"SNAPSHOT_DATE": "2026-10-30"} for r in to_records(df)])
    pg = aging_table(stored, "PG30,50,80")
    assert list(pg.columns) == ["Plan Group", "Status", "Volume", "0d (Vol)", "1d (Vol)",
                                "2d (Vol)", "3d (Vol)", "4d+ (Vol)", "Total Vol"]  # fmt: skip
    assert list(pg["Status"]) == ["Hold", "Requested", "Total"]
    assert pg.iloc[2]["Volume"] == 1481 and pg.iloc[2]["0d (Vol)"] == 151  # the query's total
    no_total = aging_table(stored[stored["STATUS"] != "Total"], "PG30,50,80")
    assert no_total.iloc[2]["0d (Vol)"] == 152  # re-added when the file has no Total row
    pg20 = aging_table(stored, "PG20")
    assert list(pg20.columns)[2:] == [
        "Volume",
        "RDD Passed (Vol)",
        "RDD Not Passed (Vol)",
        "Total Vol",
    ]
    assert pg20.iloc[0]["Volume"] == 0 and dash(pg20.iloc[0]["Volume"]) == "-"  # Hold shown
    assert dash(1015.0) == "1,015"
    with pytest.raises(ValueError, match="not the LFLT Aging summary"):
        read_aging_summary(_xlsx(pd.DataFrame({"A": [1]})), "x.xlsx", dt.date(2026, 1, 1))


def test_aging_uploads_are_kept_per_day(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from routeopt.analyze import AGING_LEVEL

    repo = Repository(f"sqlite:///{tmp_path / 'a.db'}")
    repo.init_schema()
    for as_of in ("2026-10-29", "2026-10-30", "2026-10-30"):
        day, df = read_aging_summary(_xlsx(_summary_frame(as_of)), "a.xlsx", dt.date(2026, 1, 1))
        repo.save_aging(AGING_LEVEL, day, to_records(df), "a.xlsx")
    assert [d for d, _n, _ in repo.list_aging(AGING_LEVEL)] == [
        dt.date(2026, 10, 30),
        dt.date(2026, 10, 29),
    ]
    assert len(aging_frame(repo.load_aging(AGING_LEVEL))) == 12


def _perf_rows(snapshot: str) -> list[dict]:
    base = {"SNAPSHOT_DATE": snapshot, "ORIGIN": "LGC2", "VOLUME": 100.0}
    return [
        base | {"LOAD_ID": "19,007,709", "STOPS": 2, "SHIPPED_DATE": "2026-11-02"},
        base
        | {"LOAD_ID": "19,005,028", "STOPS": 4, "SHIPPED_DATE": "2026-11-02", "ORIGIN": "LGC7"},
        base
        | {"LOAD_ID": "19,010,000", "STOPS": 3, "SHIPPED_DATE": None, "PICKUP_DATE": "2026-11-09"},
    ]


def test_perf_average_stops_per_period_and_latest_upload_wins() -> None:
    from routeopt.analyze import is_perf, perf_by_period, perf_frame

    later = {"SNAPSHOT_DATE": "2026-11-04", "LOAD_ID": "19007709", "STOPS": 3,
             "SHIPPED_DATE": "2026-11-02", "ORIGIN": "LGC2", "VOLUME": 100.0}  # fmt: skip
    rows = [*_perf_rows("2026-11-03"), later]
    df = perf_frame(rows)
    assert len(df) == 3  # one row per load
    assert df.set_index("LOAD_ID").loc["19007709", "STOPS"] == 3  # the later upload
    assert df.set_index("LOAD_ID").loc["19010000", "DAY"] == dt.date(2026, 11, 9)  # pickup
    day = perf_by_period(df, "Day").set_index("PERIOD")
    assert day.loc["11/02", "LOADS"] == 2 and day.loc["11/02", "AVG_STOPS"] == 3.5
    assert day.loc["11/02", "AT_TARGET"] == 1.0
    month = perf_by_period(df, "Month").set_index("PERIOD")
    assert month.loc["Nov 2026", "LOADS"] == 3 and month.loc["Nov 2026", "AVG_STOPS"] == 3.33
    assert is_perf(pd.DataFrame(columns=["LOAD_ID", "STOPS"]))
    assert not is_perf(pd.DataFrame(columns=["LOAD_ID", "STOPS", "LOAD_STATUS"]))
