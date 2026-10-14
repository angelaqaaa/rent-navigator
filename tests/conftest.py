"""Pure tests use an explicit snapshot-era date, never a current service-age claim."""

from datetime import date

import pytest

from rent_navigator import service_checks
from rent_navigator.corpus import load_corpus


@pytest.fixture(scope="session")
def synthetic_service_day() -> date:
    return load_corpus().snapshot_date


@pytest.fixture(autouse=True)
def snapshot_service_clock(monkeypatch: pytest.MonkeyPatch, synthetic_service_day: date) -> None:
    monkeypatch.setattr(service_checks, "utc_today", lambda: synthetic_service_day)
