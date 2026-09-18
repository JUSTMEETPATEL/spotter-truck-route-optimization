"""The Candidate registry: coordinate collapse and lazy loading."""

import subprocess
import sys

import pytest

from apps.stations import registry
from apps.stations.models import Station

pytestmark = pytest.mark.django_db


def station(opis_id: str, price: float, lat: float = 32.9479, lon: float = -112.7166) -> Station:
    return Station.objects.create(
        opis_id=opis_id,
        name=f"TRUCKSTOP {opis_id}",
        address="I-8, EXIT 119",
        city="Gila Bend",
        state="AZ",
        latitude=lat,
        longitude=lon,
        price_mean=price,
        price_min=price,
        price_max=price,
        price_sample_count=1,
    )


@pytest.fixture(autouse=True)
def _clear_registry():
    registry.reset()
    yield
    registry.reset()


class TestCoordinateCollapse:
    def test_co_located_stations_collapse_to_the_cheapest(self):
        station("10", 3.90)
        station("11", 2.80)
        station("12", 3.10)
        [candidate] = registry.candidates()
        assert candidate.opis_id == "11"
        assert candidate.price_per_gallon == pytest.approx(2.80)
        assert candidate.stations_at_location == 3

    def test_a_price_tie_is_broken_by_opis_id_ascending(self):
        station("1000", 2.80)
        station("999", 2.80)
        [candidate] = registry.candidates()
        assert candidate.opis_id == "999"

    def test_stations_at_different_coordinates_stay_separate(self):
        station("10", 3.90)
        station("11", 2.80, lat=33.5, lon=-112.0)
        assert len(registry.candidates()) == 2

    def test_stations_at_the_same_city_centroid_collapse_whatever_their_name(self):
        station("10", 3.90)
        station("11", 3.20)
        candidates = registry.candidates()
        assert len(candidates) == 1
        assert candidates[0].city == "Gila Bend"
        assert candidates[0].state == "AZ"


class TestLazyLoading:
    def test_the_registry_is_empty_until_it_is_asked(self):
        station("10", 3.90)
        # Nothing has called candidates() yet, so nothing is cached.
        assert registry._REGISTRY._candidates is None
        registry.candidates()
        assert registry._REGISTRY._candidates is not None

    def test_the_result_is_cached_after_the_first_access(self):
        station("10", 3.90)
        first = registry.candidates()
        station("11", 2.10, lat=40.0, lon=-90.0)
        assert registry.candidates() is first

    def test_resetting_rebuilds_from_the_database(self):
        station("10", 3.90)
        assert len(registry.candidates()) == 1
        station("11", 2.10, lat=40.0, lon=-90.0)
        registry.reset()
        assert len(registry.candidates()) == 2


def test_importing_the_registry_touches_no_database():
    """A fresh clone runs ``migrate`` before the Station table exists."""
    script = (
        "import django, os;"
        "os.environ['DJANGO_SETTINGS_MODULE']='config.settings';"
        "os.environ['DATABASE_URL']='sqlite:////nonexistent/path/db.sqlite3';"
        "django.setup();"
        "import apps.stations.registry;"
        "print('imported')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "imported" in completed.stdout
