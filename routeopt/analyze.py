"""Analyze page data: the LFLT aging summary and the LFLT performance uploads.

Each upload is a query result; it is stored per day (aging: its AS_OF day) so the page
can show any day and the trend over days.
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

import pandas as pd


def to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """JSON-safe rows (dates as ISO text, NaN as None)."""
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    out = out.astype(object).where(out.notna(), None)
    return out.to_dict("records")


AGING_LEVEL = "aging_summary"  # stored with the other uploads, one per AS_OF day
GROUPS = ["PG30,50,80", "PG20"]
STATUSES = ["Hold", "Requested", "Total"]
DAY_COLS = ["D0", "D1", "D2", "D3", "D4P"]
DAY_LABELS = {"D0": "0d", "D1": "1d", "D2": "2d", "D3": "3d", "D4P": "4d+"}
RDD_COLS = ["RDD_PASSED", "RDD_NOT_PASSED"]
RDD_LABELS = {"RDD_PASSED": "RDD Passed", "RDD_NOT_PASSED": "RDD Not Passed"}
# result headers (spaces / brackets / case ignored) -> column
_HEADERS = {
    "ASOF": "AS_OF",
    "PLANGROUP": "PLAN_GROUP",
    "STATUS": "STATUS",
    "VOLUME": "VOLUME",
    "0DVOL": "D0",
    "1DVOL": "D1",
    "2DVOL": "D2",
    "3DVOL": "D3",
    "4DVOL": "D4P",
    "4D+VOL": "D4P",
    "RDDPASSEDVOL": "RDD_PASSED",
    "RDDNOTPASSEDVOL": "RDD_NOT_PASSED",
    "TOTALVOL": "TOTAL",
}
NUMBER_COLS = ["VOLUME", *DAY_COLS, *RDD_COLS, "TOTAL"]


def _key(header: Any) -> str:
    return "".join(ch for ch in str(header).upper() if ch.isalnum() or ch == "+")


def _number(v: Any) -> float:
    """'1,015' -> 1015, '-' / blank -> 0."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    text = str(v).replace(",", "").replace("$", "").strip()
    if text in ("", "-"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def read_aging_summary(data: bytes, name: str, today: dt.date) -> tuple[dt.date, pd.DataFrame]:
    """The LFLT Aging summary query result -> (its AS_OF day, one row per plan group x
    status with numeric volumes)."""
    if name.lower().endswith(".csv"):
        raw = pd.read_csv(io.BytesIO(data), dtype=str)
    else:
        raw = pd.read_excel(io.BytesIO(data), dtype=str)
    raw.columns = [_HEADERS.get(_key(c), str(c)) for c in raw.columns]
    if not {"STATUS", "VOLUME"} <= set(raw.columns) or "PLAN_GROUP" not in raw.columns:
        raise ValueError(
            "This is not the LFLT Aging summary query result "
            "(needs Plan Group, Status and Volume columns)."
        )
    raw["PLAN_GROUP"] = raw["PLAN_GROUP"].replace("", None).ffill()  # blank = same group
    raw = raw[raw["PLAN_GROUP"].isin(GROUPS) & raw["STATUS"].isin(STATUSES)].copy()
    for c in NUMBER_COLS:
        raw[c] = [_number(v) for v in raw[c]] if c in raw.columns else 0.0
    day = today
    if "AS_OF" in raw.columns and raw["AS_OF"].notna().any():
        day = pd.to_datetime(raw["AS_OF"].dropna().iloc[0]).date()
    out = raw[["PLAN_GROUP", "STATUS", *NUMBER_COLS]].reset_index(drop=True)
    return day, out


def aging_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Stored rows (every day) -> a frame with the day as a date."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["DAY"] = pd.to_datetime(df["SNAPSHOT_DATE"]).dt.date
    for c in NUMBER_COLS:
        df[c] = pd.to_numeric(df.get(c, 0.0), errors="coerce").fillna(0.0)
    return df


def aging_table(df: pd.DataFrame, group: str) -> pd.DataFrame:
    """One day's table of a group, Hold / Requested / Total (the query's Total; re-added
    when missing), in the shared layout."""
    part = df[df["PLAN_GROUP"] == group].set_index("STATUS")
    cols = ["VOLUME", *(DAY_COLS if group != "PG20" else RDD_COLS)]
    rows = []
    for status in ("Hold", "Requested"):
        rows.append({"Status": status} | {c: float(part[c].get(status, 0.0)) for c in cols})
    if "Total" in part.index:  # the query's own total (summed before rounding)
        total = {c: float(part[c].get("Total", 0.0)) for c in cols}
    else:
        total = {c: sum(r[c] for r in rows) for c in cols}
    rows.append({"Status": "Total"} | total)
    t = pd.DataFrame(rows)
    t.insert(0, "Plan Group", [group, "", ""])
    t["Total Vol"] = t["VOLUME"]
    labels = {"VOLUME": "Volume"} | {c: f"{DAY_LABELS[c]} (Vol)" for c in DAY_COLS}
    labels |= {c: f"{RDD_LABELS[c]} (Vol)" for c in RDD_COLS}
    return t.rename(columns=labels)


def dash(v: Any) -> str:
    """1015 -> '1,015', 0 -> '-' (the shared table's style)."""
    if isinstance(v, str):
        return v
    return "-" if round(float(v)) == 0 else f"{round(float(v)):,}"


# ---- LFLT performance (the load-level performance query) ---------------------------------
PERF_REQUIRED = {"LOAD_ID", "STOPS"}
PERF_LEVEL = "perf"  # stored with the aging snapshots, one upload per day


def is_perf(df: pd.DataFrame) -> bool:
    """The LFLT performance query result: loads with STOPS (and no aging columns)."""
    return set(df.columns) >= PERF_REQUIRED and "LOAD_STATUS" not in df.columns


def read_perf(data: bytes, name: str) -> pd.DataFrame:
    if name.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(data))
    else:
        df = pd.read_excel(io.BytesIO(data))
    df.columns = [str(c).strip().upper().replace(" ", "_") for c in df.columns]
    if not is_perf(df):
        raise ValueError(
            "This is not the LFLT performance query result (needs LOAD_ID and STOPS columns)."
        )
    return df


def perf_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """All uploads -> one row per load (the latest upload of a load wins), with the day it
    counts on: shipped date, else pickup date."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # "19,007,709" (as copied from a grid) and 19007709 are the same load
    df["LOAD_ID"] = df["LOAD_ID"].astype(str).str.replace(",", "", regex=False).str.strip()
    df["LOAD_ID"] = df["LOAD_ID"].str.replace(r"\.0$", "", regex=True)
    df = df.sort_values("SNAPSHOT_DATE").drop_duplicates("LOAD_ID", keep="last")
    for c in ("STOPS", "SHIPMENTS", "VOLUME", "DISTANCE", "ORDER_VALUE"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    shipped = pd.to_datetime(df.get("SHIPPED_DATE"), errors="coerce")
    pickup = pd.to_datetime(df.get("PICKUP_DATE"), errors="coerce")
    day = shipped.fillna(pickup) if shipped is not None else pickup
    df["DAY"] = day.dt.date
    df["ORIGIN"] = df.get("ORIGIN", pd.Series([""] * len(df))).fillna("").astype(str)
    return df[df["DAY"].notna() & df["STOPS"].notna()]


def perf_by_period(df: pd.DataFrame, period: str, target: float = 3.0) -> pd.DataFrame:
    """Per day / week / month / year: loads, average stops per load, share of loads at or
    above the target, average volume."""
    from routeopt.appointments import period_key

    keys = [period_key(d, period) for d in df["DAY"]]
    g = df.assign(START=[k[0] for k in keys], PERIOD=[k[1] for k in keys])
    out = (
        g.groupby(["START", "PERIOD"])
        .agg(
            LOADS=("LOAD_ID", "nunique"),
            AVG_STOPS=("STOPS", "mean"),
            STOPS=("STOPS", "sum"),
            AT_TARGET=("STOPS", lambda s: float((s >= target).mean())),
            AVG_VOLUME=("VOLUME", "mean") if "VOLUME" in g.columns else ("STOPS", "size"),
        )
        .reset_index()
        .sort_values("START")
    )
    out["AVG_STOPS"] = out["AVG_STOPS"].round(2)
    out["AVG_VOLUME"] = out["AVG_VOLUME"].round(0)
    return out
