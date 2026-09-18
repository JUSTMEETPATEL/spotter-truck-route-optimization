"""Load time: the committed CSV into the database, with no network."""

import csv
import socket

import pytest
from django.core.management import call_command

from apps.stations import registry
from apps.stations.management.commands.build_station_data import STATION_FIELDS
from apps.stations.models import Station

pytestmark = pytest.mark.django_db

ROWS = [
    {
        "opis_id": "20",
        "name": "PILOT TRAVEL CENTER #1243",
        "address": "I-8, EXIT 119 & SR-85",
        "city": "Gila Bend",
        "state": "AZ",
        "latitude": "32.947900",
        "longitude": "-112.716600",
        "price_mean": "3.899000",
        "price_min": "3.899000",
        "price_max": "3.899000",
        "price_sample_count": "2",
    },
    {
        "opis_id": "7",
        "name": "WOODSHED OF BIG CABIN",
        "address": "I-44, EXIT 283 & US-69",
        "city": "Big Cabin",
        "state": "OK",
        "latitude": "36.539000",
        "longitude": "-95.221000",
        "price_mean": "3.007333",
        "price_min": "3.007333",
        "price_max": "3.007333",
        "price_sample_count": "1",
    },
]


@pytest.fixture
def stations_csv(tmp_path):
    path = tmp_path / "stations_geocoded.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATION_FIELDS)
        writer.writeheader()
        writer.writerows(ROWS)
    return path


@pytest.fixture(autouse=True)
def _clear_registry():
    registry.reset()
    yield
    registry.reset()


class TestLoading:
    def test_every_row_becomes_a_station(self, stations_csv):
        call_command("load_stations", "--csv", str(stations_csv), verbosity=0)
        assert Station.objects.count() == 2
        station = Station.objects.get(opis_id="20")
        assert station.name == "PILOT TRAVEL CENTER #1243"
        assert station.city == "Gila Bend"
        assert station.latitude == pytest.approx(32.9479)
        assert station.price_mean == pytest.approx(3.899)
        assert station.price_sample_count == 2

    def test_loading_twice_does_not_duplicate_stations(self, stations_csv):
        call_command("load_stations", "--csv", str(stations_csv), verbosity=0)
        call_command("load_stations", "--csv", str(stations_csv), verbosity=0)
        assert Station.objects.count() == 2

    def test_loading_drops_stations_that_left_the_file(self, stations_csv, tmp_path):
        call_command("load_stations", "--csv", str(stations_csv), verbosity=0)
        smaller = tmp_path / "smaller.csv"
        with smaller.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=STATION_FIELDS)
            writer.writeheader()
            writer.writerows(ROWS[:1])
        call_command("load_stations", "--csv", str(smaller), verbosity=0)
        assert [s.opis_id for s in Station.objects.all()] == ["20"]

    def test_loading_invalidates_the_candidate_registry(self, stations_csv):
        assert registry.candidates() == []
        call_command("load_stations", "--csv", str(stations_csv), verbosity=0)
        assert len(registry.candidates()) == 2

    def test_a_missing_file_is_a_clear_error(self, tmp_path):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="build_station_data"):
            call_command("load_stations", "--csv", str(tmp_path / "absent.csv"), verbosity=0)


class TestNoNetwork:
    def test_loading_opens_no_socket(self, stations_csv, monkeypatch):
        """Serving stays at one external call because loading makes none."""

        def refuse(*args, **kwargs):
            raise AssertionError("load_stations must not touch the network")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        call_command("load_stations", "--csv", str(stations_csv), verbosity=0)
        assert Station.objects.count() == 2
