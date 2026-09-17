import csv
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SUPPLIED_CSV = REPO_ROOT / "data" / "fuel-prices-for-be-assessment.csv"


@pytest.fixture(scope="session")
def supplied_observations() -> list[dict[str, str]]:
    """Every row of the supplied price file, as read from disk."""
    with SUPPLIED_CSV.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


class FakeResponse:
    """Stands in for a ``requests`` response, as the provider reads one.

    The provider deliberately reads the body before judging the status, since
    OSRM answers an impossible route with HTTP 400 and a code that says so.
    """

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload
