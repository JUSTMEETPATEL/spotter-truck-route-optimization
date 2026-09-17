"""Cleaning the supplied Price Observations into Stations."""

import pytest

from apps.stations.cleaning import clean_price_observations

DALLAS_ROW = {
    "OPIS Truckstop ID": "20",
    "Truckstop Name": "PILOT TRAVEL CENTER #1243",
    "Address": "I-8, EXIT 119 & SR-85",
    "City": "Gila Bend",
    "State": "AZ",
    "Rack ID": "930",
    "Retail Price": "3.899",
}


def observation(**overrides) -> dict[str, str]:
    return DALLAS_ROW | overrides


class TestNonUsRows:
    def test_canadian_rows_are_dropped(self):
        result = clean_price_observations(
            [
                observation(),
                observation(**{"OPIS Truckstop ID": "21", "State": "ON", "City": "Cornwall"}),
                observation(**{"OPIS Truckstop ID": "22", "State": "BC", "City": "Hope"}),
            ]
        )
        assert [s.opis_id for s in result.stations] == ["20"]
        assert result.observations_read == 3
        assert result.non_us_observations_dropped == 2

    def test_the_supplied_file_yields_the_documented_counts(self, supplied_observations):
        result = clean_price_observations(supplied_observations)
        assert result.observations_read == 8151
        assert result.non_us_observations_dropped == 620
        assert len(result.stations) == 6626


class TestDuplicateCollapse:
    def test_repeated_opis_ids_collapse_into_one_station(self):
        result = clean_price_observations(
            [
                observation(**{"Retail Price": "3.00"}),
                observation(**{"Retail Price": "4.00", "Truckstop Name": "PILOT #1243"}),
            ]
        )
        [station] = result.stations
        assert station.price_sample_count == 2

    def test_the_price_is_the_mean_of_the_observations(self):
        result = clean_price_observations(
            [
                observation(**{"Retail Price": "3.00"}),
                observation(**{"Retail Price": "4.00"}),
                observation(**{"Retail Price": "3.50"}),
            ]
        )
        [station] = result.stations
        assert station.price_mean == pytest.approx(3.50)
        assert station.price_min == pytest.approx(3.00)
        assert station.price_max == pytest.approx(4.00)
        assert station.price_sample_count == 3

    def test_the_spread_of_a_single_observation_is_that_observation(self):
        [station] = clean_price_observations([observation()]).stations
        assert (station.price_mean, station.price_min, station.price_max) == pytest.approx(
            (3.899, 3.899, 3.899)
        )
        assert station.price_sample_count == 1

    def test_the_longest_name_wins_so_the_choice_is_not_file_order(self):
        result = clean_price_observations(
            [
                observation(**{"Truckstop Name": "PILOT #1243"}),
                observation(**{"Truckstop Name": "PILOT TRAVEL CENTER #1243"}),
            ]
        )
        assert result.stations[0].name == "PILOT TRAVEL CENTER #1243"
        reversed_result = clean_price_observations(
            [
                observation(**{"Truckstop Name": "PILOT TRAVEL CENTER #1243"}),
                observation(**{"Truckstop Name": "PILOT #1243"}),
            ]
        )
        assert reversed_result.stations[0].name == "PILOT TRAVEL CENTER #1243"

    def test_stations_come_back_in_opis_id_order(self, supplied_observations):
        stations = clean_price_observations(supplied_observations).stations
        ids = [int(s.opis_id) for s in stations]
        assert ids == sorted(ids)


class TestMalformedRows:
    def test_a_row_with_no_price_is_not_an_observation(self):
        result = clean_price_observations(
            [observation(), observation(**{"OPIS Truckstop ID": "99", "Retail Price": ""})]
        )
        assert [s.opis_id for s in result.stations] == ["20"]
        assert result.unusable_observations_dropped == 1

    def test_a_row_with_no_city_cannot_be_geocoded_and_is_dropped(self):
        result = clean_price_observations([observation(**{"City": "   "})])
        assert result.stations == []
        assert result.unusable_observations_dropped == 1

    def test_surrounding_whitespace_is_stripped(self):
        [station] = clean_price_observations(
            [observation(**{"City": "  Gila Bend ", "State": " AZ ", "Retail Price": " 3.50 "})]
        ).stations
        assert (station.city, station.state) == ("Gila Bend", "AZ")
        assert station.price_mean == pytest.approx(3.50)
