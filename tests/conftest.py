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


def encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    """Encode points the way the routing provider does, for provider stubs.

    The inverse of ``providers.decode_polyline``, written independently here so
    a test fixture can say what the provider returns in coordinates rather than
    in an opaque string.
    """
    encoded: list[str] = []
    last_lat = last_lon = 0
    scale = 10**precision
    for lat, lon in points:
        int_lat, int_lon = round(lat * scale), round(lon * scale)
        for delta in (int_lat - last_lat, int_lon - last_lon):
            value = ~(delta << 1) if delta < 0 else (delta << 1)
            while value >= 0x20:
                encoded.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            encoded.append(chr(value + 63))
        last_lat, last_lon = int_lat, int_lon
    return "".join(encoded)
