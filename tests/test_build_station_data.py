"""Build time: cleaning plus geocoding into the committed CSV and report."""

import csv
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.routing.geo import Point
from apps.stations.geocoding import Gazetteer, GazetteerPlace

PRICE_ROWS = [
    # Two observations of one truckstop, so the mean is exercised.
    ("20", "PILOT TRAVEL CENTER #1243", "I-8, EXIT 119", "Gila Bend", "AZ", "930", "3.00"),
    ("20", "PILOT #1243", "I-8, EXIT 119", "Gila Bend", "AZ", "930", "4.00"),
    ("7", "WOODSHED", "I-44, EXIT 283", "Big Cabin", "OK", "307", "3.10"),
    # A city the Gazetteer cannot place, which must be excluded and counted.
    ("99", "GHOST TRUCKSTOP", "NOWHERE", "Atlantis", "OK", "307", "3.50"),
    # A Canadian row, which must never reach the output at all.
    ("55", "PETRO CANADA", "HWY 401", "Cornwall", "ON", "999", "5.00"),
]


@pytest.fixture
def prices_csv(tmp_path):
    path = tmp_path / "prices.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "OPIS Truckstop ID",
                "Truckstop Name",
                "Address",
                "City",
                "State",
                "Rack ID",
                "Retail Price",
            ]
        )
        writer.writerows(PRICE_ROWS)
    return path


@pytest.fixture
def gazetteer_csv(tmp_path):
    path = tmp_path / "gazetteer.csv"
    Gazetteer(
        [
            GazetteerPlace("GILA BEND", "AZ", Point(32.9479, -112.7166), 23.5, 3),
            GazetteerPlace("BIG CABIN", "OK", Point(36.5390, -95.2210), 1.5, 3),
        ]
    ).write_csv(path)
    return path


@pytest.fixture
def overrides_csv(tmp_path):
    path = tmp_path / "city_overrides.csv"
    path.write_text("city,state,latitude,longitude,source\n", encoding="utf-8")
    return path


def build(tmp_path, prices_csv, gazetteer_csv, overrides_csv, **extra):
    output = tmp_path / "stations_geocoded.csv"
    report = tmp_path / "build_report.json"
    call_command(
        "build_station_data",
        "--prices-csv",
        str(prices_csv),
        "--gazetteer-csv",
        str(gazetteer_csv),
        "--overrides-csv",
        str(overrides_csv),
        "--output",
        str(output),
        "--report",
        str(report),
        verbosity=0,
        **extra,
    )
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows, json.loads(report.read_text())


class TestOutput:
    def test_stations_are_written_with_their_coordinates_and_price_spread(
        self, tmp_path, prices_csv, gazetteer_csv, overrides_csv
    ):
        rows, _ = build(tmp_path, prices_csv, gazetteer_csv, overrides_csv)
        by_id = {row["opis_id"]: row for row in rows}
        assert set(by_id) == {"20", "7"}
        assert float(by_id["20"]["price_mean"]) == pytest.approx(3.50)
        assert float(by_id["20"]["price_min"]) == pytest.approx(3.00)
        assert float(by_id["20"]["price_max"]) == pytest.approx(4.00)
        assert int(by_id["20"]["price_sample_count"]) == 2
        assert float(by_id["20"]["latitude"]) == pytest.approx(32.9479)

    def test_the_canadian_row_never_reaches_the_output(
        self, tmp_path, prices_csv, gazetteer_csv, overrides_csv
    ):
        rows, report = build(tmp_path, prices_csv, gazetteer_csv, overrides_csv)
        assert all(row["state"] != "ON" for row in rows)
        assert report["non_us_observations_dropped"] == 1


class TestReport:
    def test_the_report_publishes_what_was_excluded(
        self, tmp_path, prices_csv, gazetteer_csv, overrides_csv
    ):
        _, report = build(tmp_path, prices_csv, gazetteer_csv, overrides_csv)
        assert report["stations_total"] == 3
        assert report["stations_geocoded"] == 2
        assert report["stations_excluded"] == 1
        assert report["excluded_cities"] == [{"city": "Atlantis", "state": "OK", "stations": 1}]

    def test_coverage_is_reported_per_state(
        self, tmp_path, prices_csv, gazetteer_csv, overrides_csv
    ):
        _, report = build(tmp_path, prices_csv, gazetteer_csv, overrides_csv)
        assert report["coverage_by_state"] == {
            "AZ": {"stations": 1, "geocoded": 1, "coverage": 1.0},
            "OK": {"stations": 2, "geocoded": 1, "coverage": 0.5},
        }

    def test_every_written_coordinate_is_inside_the_usa(
        self, tmp_path, prices_csv, gazetteer_csv, overrides_csv
    ):
        from apps.stations.geocoding import inside_us_bounds

        rows, report = build(tmp_path, prices_csv, gazetteer_csv, overrides_csv)
        assert report["stations_out_of_us_bounds"] == 0
        for row in rows:
            assert inside_us_bounds(Point(float(row["latitude"]), float(row["longitude"])))


class TestOverrides:
    def test_a_hand_correction_places_a_city_the_gazetteer_cannot(
        self, tmp_path, prices_csv, gazetteer_csv, overrides_csv
    ):
        overrides_csv.write_text(
            "city,state,latitude,longitude,source\n"
            'Atlantis,OK,35.5,-97.5,"GNIS: a traceable feature"\n',
            encoding="utf-8",
        )
        rows, report = build(tmp_path, prices_csv, gazetteer_csv, overrides_csv)
        assert report["stations_geocoded"] == 3
        assert report["stations_excluded"] == 0
        assert report["stations_from_overrides"] == 1
        placed = next(row for row in rows if row["opis_id"] == "99")
        assert float(placed["latitude"]) == pytest.approx(35.5)


class TestCoverageGate:
    def test_low_coverage_is_a_warning_by_default(
        self, tmp_path, prices_csv, gazetteer_csv, overrides_csv, settings
    ):
        settings.GAZETTEER_COVERAGE_FLOOR = 0.98
        rows, report = build(tmp_path, prices_csv, gazetteer_csv, overrides_csv)
        # Two of three placed is well below the floor, and the build still
        # produces its output so the figures can be looked at.
        assert report["gazetteer_coverage"] < 0.98
        assert len(rows) == 2

    def test_strict_turns_the_floor_into_a_failure(
        self, tmp_path, prices_csv, gazetteer_csv, overrides_csv, settings
    ):
        settings.GAZETTEER_COVERAGE_FLOOR = 0.98
        with pytest.raises(CommandError, match="below the 98% floor"):
            build(tmp_path, prices_csv, gazetteer_csv, overrides_csv, strict=True)

    def test_a_missing_gazetteer_says_which_command_builds_it(
        self, tmp_path, prices_csv, overrides_csv
    ):
        with pytest.raises(CommandError, match="build_gazetteer"):
            build(tmp_path, prices_csv, tmp_path / "absent.csv", overrides_csv)
