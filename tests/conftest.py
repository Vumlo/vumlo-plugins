import pytest

from vumlo_plugins import clock
from vumlo_plugins.testing import FakeCore


@pytest.fixture(autouse=True)
def frozen_clock():
    """Every test starts at the same noon, so signatures, deadlines and 30-day windows are the same on every run."""
    clock.freeze("2026-03-02T12:00:00Z")
    yield clock
    clock.unfreeze()


@pytest.fixture
def fake() -> FakeCore:
    return FakeCore()
