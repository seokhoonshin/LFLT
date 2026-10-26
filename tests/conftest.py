from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from routeopt.config import RunSettings
from routeopt.ingest.validator import GeocodeQuery, GeocodeResult
from routeopt.sample_data import generate_sample

from tests.factories import NEAR_LAT, NEAR_LON, PLAN_DATE, make_settings


class FakeGeocoder:
    """Deterministic geocoder: every query lands near the hub, unless the street contains 'NOWHERE'."""

    def __init__(self) -> None:
        self.calls = 0

    def geocode(self, queries: list[GeocodeQuery]) -> dict[str, GeocodeResult]:
        self.calls += 1
        out: dict[str, GeocodeResult] = {}
        for i, q in enumerate(queries):
            if "NOWHERE" in q.street.upper():
                out[q.key] = GeocodeResult(q.key, None, None, match_type="No_Match")
            else:
                out[q.key] = GeocodeResult(
                    q.key,
                    NEAR_LAT + 0.005 * i,
                    NEAR_LON - 0.005 * i,
                    matched_address=q.street.upper(),
                )
        return out


@pytest.fixture
def plan_date() -> dt.date:
    return PLAN_DATE


@pytest.fixture
def settings() -> RunSettings:
    return make_settings()


@pytest.fixture
def fake_geocoder() -> FakeGeocoder:
    return FakeGeocoder()


@pytest.fixture(scope="session")
def sample_workbook(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("samples") / "sample_120.xlsx"
    return generate_sample(120, PLAN_DATE, 42, out)


@pytest.fixture(scope="session")
def sample_workbook_150(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("samples") / "sample_150.xlsx"
    return generate_sample(150, PLAN_DATE, 7, out)
