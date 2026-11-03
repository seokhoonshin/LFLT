"""The Refresh button: run the queries listed in ``queries.toml`` against Oracle and store
each result where an upload would have put it (Analyze) or as an Excel file.

Oracle's login is in ``oracle.toml`` (host, port, service_name, username); the password is
``ORACLE_PASSWORD`` in the environment / ``.env``, else the file's ``password``, else the
page asks for it.  The driver (python-oracledb, thin mode) is only imported when a refresh
runs, and can be replaced in tests.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import re
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

TARGETS = ("analyze.performance", "analyze.aging", "file")
Connect = Callable[..., Any]


class RefreshError(RuntimeError):
    pass


@dataclass(frozen=True)
class OracleLogin:
    host: str
    port: int
    service_name: str
    username: str
    password: str = ""

    @property
    def dsn(self) -> str:
        return f"{self.host}:{self.port}/{self.service_name}"


@dataclass(frozen=True)
class QuerySpec:
    name: str
    file: str
    target: str
    params: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    enabled: bool = True


@dataclass
class QueryResult:
    name: str
    ok: bool
    rows: int = 0
    seconds: float = 0.0
    message: str = ""


def load_login(root: Path, env_password: str | None = None) -> OracleLogin:
    path = root / "oracle.toml"
    if not path.exists():
        raise RefreshError("oracle.toml not found: copy oracle.toml.example and fill it in.")
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8")).get("oracle", {})
    except tomllib.TOMLDecodeError as exc:
        raise RefreshError(f"oracle.toml is not valid TOML: {exc}") from exc
    missing = [k for k in ("host", "port", "service_name", "username") if not cfg.get(k)]
    if missing or str(cfg["host"]).strip().lower() in ("xxx", "xxxx"):
        raise RefreshError(
            "oracle.toml is not filled in yet"
            + (f" (missing {', '.join(missing)})" if missing else "")
            + "."
        )
    password = env_password or os.environ.get("ORACLE_PASSWORD") or str(cfg.get("password") or "")
    return OracleLogin(
        str(cfg["host"]), int(cfg["port"]), str(cfg["service_name"]), str(cfg["username"]), password
    )


def load_queries(root: Path) -> list[QuerySpec]:
    path = root / "queries.toml"
    if not path.exists():
        raise RefreshError("queries.toml not found.")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("query", [])
    except tomllib.TOMLDecodeError as exc:
        raise RefreshError(f"queries.toml is not valid TOML: {exc}") from exc
    specs = []
    for q in raw:
        if q.get("target") not in TARGETS:
            raise RefreshError(
                f"Query '{q.get('name')}': target must be one of {', '.join(TARGETS)}."
            )
        if not q.get("name") or not q.get("file"):
            raise RefreshError("Every [[query]] needs a name and a file.")
        specs.append(
            QuerySpec(
                name=q["name"],
                file=q["file"],
                target=q["target"],
                params=dict(q.get("params", {})),
                output=str(q.get("output", "")),
                enabled=bool(q.get("enabled", True)),
            )
        )
    return specs


def read_sql(root: Path, spec: QuerySpec) -> str:
    path = root / spec.file
    if not path.exists():
        raise RefreshError(f"SQL file not found: {spec.file}")
    sql = path.read_text(encoding="utf-8").strip().rstrip(";").strip()
    used = set(
        re.findall(r"(?<![:\w]):([A-Za-z_]\w*)", re.sub(r"'[^']*'", "''", strip_comments(sql)))
    )
    unknown = used - set(spec.params)
    if unknown:
        raise RefreshError(
            f"{spec.file} uses :{', :'.join(sorted(unknown))} but queries.toml has no such param."
        )
    return sql


def strip_comments(sql: str) -> str:
    """The SQL without its -- line comments and /* */ blocks (hints too), outside strings."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i : i + 2]
        if sql[i] == "'":  # a string literal: copy to its closing quote ('' is an escape)
            j = i + 1
            while j < n:
                if sql[j] == "'" and sql[j + 1 : j + 2] == "'":
                    j += 2
                elif sql[j] == "'":
                    break
                else:
                    j += 1
            out.append(sql[i : j + 1])
            i = j + 1
        elif two == "--":
            while i < n and sql[i] != "\n":
                i += 1
        elif two == "/*":
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def run_sql(connect: Connect, login: OracleLogin, sql: str, params: dict[str, Any]) -> pd.DataFrame:
    """Run one SELECT (read-only) and return it as a frame with upper-case column names."""
    if not re.match(r"\s*(with|select)\b", strip_comments(sql), re.IGNORECASE):
        raise RefreshError("Only SELECT queries can be refreshed.")
    used = set(
        re.findall(r"(?<![:\w]):([A-Za-z_]\w*)", re.sub(r"'[^']*'", "''", strip_comments(sql)))
    )
    with connect(user=login.username, password=login.password, dsn=login.dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, {k: v for k, v in params.items() if k in used})
        cols = [str(d[0]).upper() for d in cur.description]
        data = cur.fetchall()
    return pd.DataFrame(data, columns=cols)


def default_connect() -> Connect:
    try:
        import oracledb
    except ImportError as exc:  # pragma: no cover - environment
        raise RefreshError("The oracledb package is missing: run  pip install oracledb") from exc
    return oracledb.connect  # type: ignore[no-any-return]


def store(repo: Any, spec: QuerySpec, df: pd.DataFrame, data_dir: Path, today: dt.date) -> str:
    """Put a query result where an upload would have: Analyze or an Excel file."""
    from routeopt.analyze import (
        AGING_LEVEL,
        PERF_LEVEL,
        is_perf,
        read_aging_summary,
        to_records,
    )

    if spec.target == "file":
        out_dir = data_dir / "refresh"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = spec.output or re.sub(r"\W+", "_", spec.name).strip("_").lower() + ".xlsx"
        df.to_excel(out_dir / name, index=False)
        return f"saved {out_dir / name}"
    if spec.target == "analyze.performance":
        if not is_perf(df):
            raise RefreshError(
                "The result has no LOAD_ID / STOPS columns (not the performance query)."
            )
        repo.save_aging(PERF_LEVEL, today, to_records(df), f"refresh {today}")
        return f"{len(df)} loads"
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    day, frame = read_aging_summary(buf.getvalue(), "refresh.xlsx", today)
    repo.save_aging(AGING_LEVEL, day, to_records(frame), f"refresh {today}")
    return f"aging of {day:%m/%d/%Y}"


def refresh(
    root: Path,
    repo: Any,
    data_dir: Path,
    only: str | None = None,
    password: str | None = None,
    connect: Connect | None = None,
    today: dt.date | None = None,
    on_progress: Callable[[QueryResult], None] | None = None,
) -> list[QueryResult]:
    """Run every enabled query (or just ``only``); one failing query does not stop the rest."""
    specs = [s for s in load_queries(root) if (s.name == only if only else s.enabled)]
    if not specs:
        raise RefreshError("No query to run (all are disabled in queries.toml).")
    login = load_login(root, password)
    if not login.password:
        raise RefreshError("No Oracle password: set ORACLE_PASSWORD in .env or enter it.")
    run = connect or default_connect()
    today = today or dt.date.today()
    results: list[QueryResult] = []
    for spec in specs:
        t0 = time.perf_counter()
        try:
            sql = read_sql(root, spec)
            df = run_sql(run, login, sql, spec.params)
            note = store(repo, spec, df, data_dir, today)
            res = QueryResult(spec.name, True, len(df), time.perf_counter() - t0, note)
        except Exception as exc:
            res = QueryResult(spec.name, False, 0, time.perf_counter() - t0, _short(exc))
        results.append(res)
        if on_progress:
            on_progress(res)
    return results


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0] if text else ''}"[:300]
