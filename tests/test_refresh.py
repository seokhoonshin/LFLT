"""Refresh button: queries.toml / oracle.toml, bind variables, routing of each result."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from routeopt.analyze import AGING_LEVEL, PERF_LEVEL
from routeopt.db.repo import Repository
from routeopt.refresh import (
    RefreshError,
    load_login,
    load_queries,
    read_sql,
    refresh,
    run_sql,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeCursor:
    def __init__(self, tables: dict[str, tuple[list[str], list[tuple]]]) -> None:
        self.tables, self.description, self.rows, self.sql, self.binds = tables, [], [], "", {}

    def execute(self, sql: str, binds: dict) -> None:
        self.sql, self.binds = sql, binds
        for key, (cols, rows) in self.tables.items():
            if key in sql:
                self.description = [(c,) for c in cols]
                self.rows = rows
                return
        raise RuntimeError("ORA-00942: table or view does not exist")

    def fetchall(self) -> list[tuple]:
        return self.rows


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self.c = cursor

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.c


def _connect(tables):  # type: ignore[no-untyped-def]
    cursor = FakeCursor(tables)

    def connect(**kw: str) -> FakeConn:
        assert kw["dsn"] == "db.example:1521/SVC" and kw["user"] == "ro"
        return FakeConn(cursor)

    return connect, cursor


def _project(tmp_path: Path) -> Path:
    (tmp_path / "queries").mkdir()
    (tmp_path / "queries" / "perf.sql").write_text(
        "SELECT 1 AS LOAD_ID FROM PERF_TABLE WHERE d >= TO_DATE(:start_date, 'YYYY-MM-DD');\n"
    )
    (tmp_path / "queries" / "aging.sql").write_text("SELECT 1 FROM AGING_TABLE\n")
    (tmp_path / "queries" / "cand.sql").write_text("SELECT 1 FROM CAND_TABLE\n")
    (tmp_path / "queries.toml").write_text(
        """
[[query]]
name = "perf"
file = "queries/perf.sql"
target = "analyze.performance"
params = { start_date = "2026-01-01" }

[[query]]
name = "aging"
file = "queries/aging.sql"
target = "analyze.aging"

[[query]]
name = "cand"
file = "queries/cand.sql"
target = "file"
output = "cand.xlsx"
enabled = false
"""
    )
    (tmp_path / "oracle.toml").write_text(
        '[oracle]\nhost = "db.example"\nport = 1521\nservice_name = "SVC"\nusername = "ro"\n'
    )
    return tmp_path


PERF = (
    ["LOAD_ID", "STOPS", "SHIPPED_DATE", "ORIGIN", "VOLUME"],
    [(1, 3, "2026-11-02", "LGC2", 500.0)],
)
AGING = (
    [
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
    ],
    [
        (
            "2026-11-03",
            "PG30,50,80",
            "Hold",
            "1,015",
            "62",
            "200",
            "185",
            "14",
            "553",
            None,
            None,
            "1,015",
        )
    ],
)


def test_refresh_all_routes_each_result_and_skips_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = _project(tmp_path)
    repo = Repository(f"sqlite:///{tmp_path / 'r.db'}")
    repo.init_schema()
    connect, _ = _connect({"PERF_TABLE": PERF, "AGING_TABLE": AGING})
    seen: list[str] = []
    out = refresh(
        root, repo, tmp_path / "data", password="pw", connect=connect,
        today=dt.date(2026, 11, 3), on_progress=lambda r: seen.append(r.name),
    )  # fmt: skip
    assert [r.name for r in out] == ["perf", "aging"] and all(r.ok for r in out)
    assert seen == ["perf", "aging"]  # progress reported as it goes
    assert repo.list_aging(PERF_LEVEL)[0][:2] == (dt.date(2026, 11, 3), 1)
    assert repo.list_aging(AGING_LEVEL)[0][0] == dt.date(2026, 11, 3)
    assert not (tmp_path / "data" / "refresh").exists()  # the disabled file query did not run


def test_one_failing_query_does_not_stop_the_others(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = _project(tmp_path)
    repo = Repository(f"sqlite:///{tmp_path / 'r.db'}")
    repo.init_schema()
    connect, _ = _connect({"AGING_TABLE": AGING})  # PERF_TABLE is missing -> ORA error
    out = refresh(root, repo, tmp_path / "data", password="pw", connect=connect,
                  today=dt.date(2026, 11, 3))  # fmt: skip
    assert [r.ok for r in out] == [False, True]
    assert "ORA-00942" in out[0].message and repo.list_aging(AGING_LEVEL)


def test_a_query_can_be_run_alone_even_when_disabled_and_saves_a_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = _project(tmp_path)
    repo = Repository(f"sqlite:///{tmp_path / 'r.db'}")
    repo.init_schema()
    connect, _ = _connect({"CAND_TABLE": (["SHIPMENT_ID", "VOLUME_CUFT"], [("S1", 12.5)])})
    out = refresh(root, repo, tmp_path / "data", only="cand", password="pw", connect=connect)
    assert out[0].ok and (tmp_path / "data" / "refresh" / "cand.xlsx").exists()


def test_bind_variables_and_the_select_only_guard(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = _project(tmp_path)
    spec = load_queries(root)[0]
    sql = read_sql(root, spec)
    assert not sql.endswith(";") and ":start_date" in sql
    connect, cursor = _connect({"PERF_TABLE": PERF})
    login = load_login(root, "pw")
    df = run_sql(connect, login, sql, {**spec.params, "unused": 1})
    assert cursor.binds == {"start_date": "2026-01-01"}  # only the binds the SQL uses
    assert list(df.columns)[:2] == ["LOAD_ID", "STOPS"]
    with pytest.raises(RefreshError, match="Only SELECT"):
        run_sql(connect, login, "DELETE FROM PERF_TABLE", {})
    (root / "queries" / "bad.sql").write_text("SELECT :nope FROM dual")
    bad = load_queries(root)[0].__class__("bad", "queries/bad.sql", "file")
    with pytest.raises(RefreshError, match="no such param"):
        read_sql(root, bad)


def test_login_file_checks(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ORACLE_PASSWORD", raising=False)
    with pytest.raises(RefreshError, match="not found"):
        load_login(tmp_path)
    (tmp_path / "oracle.toml").write_text(
        '[oracle]\nhost = "xxx"\nport = 1521\nservice_name = "xxxxxxx"\nusername = "xxxxxx"\n'
    )
    with pytest.raises(RefreshError, match="not filled in"):  # the example placeholders
        load_login(tmp_path)
    ex = ROOT / "oracle.toml.example"
    (tmp_path / "oracle.toml").write_text(ex.read_text().replace('"xxx"', '"db"'))
    login = load_login(tmp_path)
    assert login.dsn.startswith("db:1521/") and login.password == ""
    monkeypatch.setenv("ORACLE_PASSWORD", "from-env")
    assert load_login(tmp_path).password == "from-env"


def test_the_shipped_queries_toml_is_valid() -> None:
    specs = load_queries(ROOT)
    assert [s.target for s in specs] == ["analyze.performance", "analyze.aging", "file"]
    for s in specs:
        read_sql(ROOT, s)  # every file exists and every :bind has a param
