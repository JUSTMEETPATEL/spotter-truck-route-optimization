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
