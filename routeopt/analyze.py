"""Analyze page: the Plan 7774 aging uploads (load or shipment level) and their pivots.

An upload is the result of the aging query (one row per load, or one per shipment).  It
is stored per snapshot day so the page can show any day and the trend over days.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

import pandas as pd

DROP_BUCKETS = ["0-3", "4-7", "8-14", "15-30", "31+", "Unknown"]
RDD_BUCKETS = ["Before RDD", "0-3 past", "4-7 past", "8-14 past", "15+ past", "Unknown"]
STATUSES = ["Hold", "Requested"]
REQUIRED = {"LOAD_ID", "ORIGIN_LOCATION_ID", "LOAD_STATUS", "VOLUME"}


def read_upload(data: bytes, name: str) -> pd.DataFrame:
    """The aging query result (xlsx or csv) with upper-case column names."""
    if name.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(data))
    else:
        df = pd.read_excel(io.BytesIO(data))
    df.columns = [str(c).strip().upper().replace(" ", "_") for c in df.columns]
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError(
            "This is not the aging query result: missing column(s) "
            + ", ".join(sorted(missing))
            + "."
        )
    return df


def detect_level(df: pd.DataFrame) -> str:
    """'shipment' when the file has a SHIPMENT_ID column, else 'load'."""
    return "shipment" if "SHIPMENT_ID" in df.columns else "load"


def snapshot_day(df: pd.DataFrame, today: dt.date) -> dt.date:
    if "SNAPSHOT_DATE" in df.columns and df["SNAPSHOT_DATE"].notna().any():
        return pd.to_datetime(df["SNAPSHOT_DATE"].dropna().iloc[0]).date()
    return today


def to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """JSON-safe rows (dates as ISO text, NaN as None)."""
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    out = out.astype(object).where(out.notna(), None)
    return out.to_dict("records")


def pg_number(value: Any) -> str:
    """'PG20' / 20 / '20.0' / '20, 30' -> '20' / '20' / '20' / '20, 30'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    nums = re.findall(r"\d+", str(value).replace(".0", ""))
    return ", ".join(dict.fromkeys(nums))


def prepare(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Stored rows -> a frame ready to pivot (numbers numeric, PG normalised, buckets ordered)."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ("VOLUME", "ORDER_VALUE", "DECLARED_VALUE", "PIECES", "SHIPMENTS"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["PG"] = [pg_number(v) for v in df.get("PLAN_ID_GRP", pd.Series([None] * len(df)))]
    df["LOAD_STATUS"] = df["LOAD_STATUS"].fillna("Hold").astype(str)
    df["ORIGIN_LOCATION_ID"] = df["ORIGIN_LOCATION_ID"].fillna("").astype(str)
    if "DROP_AGING_BUCKET" in df.columns:
        df["DROP_AGING_BUCKET"] = pd.Categorical(
            df["DROP_AGING_BUCKET"].fillna("Unknown"), DROP_BUCKETS, ordered=True
        )
    if "RDD_AGING_BUCKET" in df.columns:
        df["RDD_AGING_BUCKET"] = pd.Categorical(
            df["RDD_AGING_BUCKET"].fillna("Unknown"), RDD_BUCKETS, ordered=True
        )
    df["SNAPSHOT_DATE"] = pd.to_datetime(df["SNAPSHOT_DATE"]).dt.date
    return df


def in_pg(df: pd.DataFrame, groups: set[str]) -> pd.DataFrame:
    """Rows whose plan group is one of ``groups`` (a mixed load counts if any matches)."""
    return df[[bool(set(p.split(", ")) & groups) for p in df["PG"]]]


def _totals(part: pd.DataFrame, level: str) -> dict[str, float]:
    out = {"Loads": float(part["LOAD_ID"].nunique()), "Volume": float(part["VOLUME"].sum())}
    if level == "shipment":
        out["Shipments"] = float(len(part))
    if "ORDER_VALUE" in part.columns:
        out["Order Value"] = float(part["ORDER_VALUE"].sum())
    return out


def summary_table(df: pd.DataFrame, level: str) -> pd.DataFrame:
    """Origin x Hold / Requested: loads (and shipments), volume, order value, and a grand
    total - the layout of the shared aging table."""
    rows = []
    for origin in sorted(df["ORIGIN_LOCATION_ID"].unique()):
        part = df[df["ORIGIN_LOCATION_ID"] == origin]
        for status in STATUSES:
            sub = part[part["LOAD_STATUS"] == status]
            rows.append({"Origin": origin, "Status": status} | _totals(sub, level))
    rows.append({"Origin": "Grand Total", "Status": ""} | _totals(df, level))
    out = pd.DataFrame(rows)
    for k in ("Loads", "Shipments"):
        if k in out.columns:
            out[k] = out[k].astype(int)
    return out


def _full_grid(
    df: pd.DataFrame, origins: list[str], column: str, order: list[str], value: str
) -> pd.DataFrame:
    """Every origin x Hold / Requested as rows (0 when empty), ``order`` as columns,
    ``value`` summed; Grand Total row and column."""
    index = pd.MultiIndex.from_product([origins, STATUSES], names=["Origin", "Status"])
    if df.empty:
        t = pd.DataFrame(0.0, index=index, columns=order)
    else:
        t = (
            df.assign(_col=df[column].astype(str))
            .groupby(["ORIGIN_LOCATION_ID", "LOAD_STATUS", "_col"], observed=True)[value]
            .sum()
            .unstack("_col")
            .reindex(index=index, columns=order)
            .fillna(0.0)
        )
        t.index.names = ["Origin", "Status"]
    t.columns.name = None
    t["Grand Total"] = t.sum(axis=1)
    t.loc[("Grand Total", ""), :] = t.sum(axis=0)
    return t.round(1)


def aging_pivot(
    df: pd.DataFrame,
    bucket: str,
    origins: list[str],
    value: str = "VOLUME",
) -> pd.DataFrame:
    """Drop in TMS / RDD aging buckets as columns (all of them; Unknown only if any)."""
    order = DROP_BUCKETS if bucket == "DROP_AGING_BUCKET" else RDD_BUCKETS
    if df.empty or not (df[bucket].astype(str) == "Unknown").any():
        order = [b for b in order if b != "Unknown"]
    return _full_grid(df, origins, bucket, order, value)


def rdd_split(df: pd.DataFrame, origins: list[str], value: str = "VOLUME") -> pd.DataFrame:
    """Past RDD (today is after RDD_TO) vs Not past RDD; No RDD only if any."""
    if df.empty:
        return _full_grid(df, origins, "RDD_STATE", ["Past RDD", "Not past RDD"], value)
    if "RDD_AGING_DAYS" in df.columns:
        days = pd.to_numeric(df["RDD_AGING_DAYS"], errors="coerce")
        state = ["No RDD" if pd.isna(d) else "Past RDD" if d > 0 else "Not past RDD" for d in days]
    else:
        state = [
            "No RDD"
            if str(b) == "Unknown"
            else "Not past RDD"
            if str(b) == "Before RDD"
            else "Past RDD"
            for b in df["RDD_AGING_BUCKET"]
        ]
    df = df.assign(RDD_STATE=state)
    order = ["Past RDD", "Not past RDD"] + (["No RDD"] if "No RDD" in state else [])
    return _full_grid(df, origins, "RDD_STATE", order, value)


def daily_trend(df: pd.DataFrame, split: str, value: str = "VOLUME") -> pd.DataFrame:
    """Per snapshot day and ``split`` value: the summed ``value`` (for the trend chart)."""
    g = df.groupby(["SNAPSHOT_DATE", split], observed=True)[value].sum().reset_index()
    g[split] = g[split].astype(str)
    return g
