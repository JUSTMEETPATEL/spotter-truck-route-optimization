"""Endpoint resolution: city and state, bare city, coordinates, containment."""

import pytest

from apps.routing.exceptions import OutsideUnitedStates, PlaceNotFound
from apps.routing.geo import Point
from apps.routing.resolver import resolve_endpoint
from apps.stations.geocoding import Gazetteer, GazetteerPlace

RADIUS = 50.0


def place(name: str, state: str, lat: float, lon: float, area: float = 10.0) -> GazetteerPlace:
    return GazetteerPlace(name=name, state=state, point=Point(lat, lon), area_sqmi=area, tier=3)


@pytest.fixture
def gazetteer() -> Gazetteer:
    return Gazetteer(
        [
            place("DALLAS", "TX", 32.7767, -96.7970, area=340.0),
            place("DALLAS", "GA", 33.9237, -84.8408, area=6.0),
            place("CHICAGO", "IL", 41.8781, -87.6298, area=227.0),
            place("ANCHORAGE", "AK", 61.2181, -149.9003, area=1704.0),
            place("HONOLULU", "HI", 21.3069, -157.8583, area=68.0),
        ]
    )


def resolve(query: str, gazetteer: Gazetteer):
    return resolve_endpoint(query, gazetteer, containment_radius_miles=RADIUS)


class TestCityAndState:
    def test_a_city_and_state_code_resolve(self, gazetteer):
        endpoint = resolve("Dallas, TX", gazetteer)
        assert endpoint.point == pytest.approx((32.7767, -96.7970))
        assert endpoint.label == "Dallas, TX"
        assert endpoint.state == "TX"

    def test_a_spelled_out_state_resolves(self, gazetteer):
        assert resolve("dallas, texas", gazetteer).state == "TX"

    def test_case_and_spacing_do_not_matter(self, gazetteer):
        assert resolve("  DALLAS ,tx ", gazetteer).point == pytest.approx((32.7767, -96.7970))

    def test_an_unknown_city_names_itself_in_the_error(self, gazetteer):
        with pytest.raises(PlaceNotFound, match="Atlantis, TX"):
            resolve("Atlantis, TX", gazetteer)

    def test_alaska_and_hawaii_are_the_usa(self, gazetteer):
        assert resolve("Anchorage, AK", gazetteer).state == "AK"
        assert resolve("Honolulu, HI", gazetteer).state == "HI"


class TestBareCity:
    def test_a_bare_city_resolves_to_the_largest_match(self, gazetteer):
        assert resolve("Dallas", gazetteer).state == "TX"

    def test_an_unknown_bare_city_is_not_found(self, gazetteer):
        with pytest.raises(PlaceNotFound, match="Atlantis"):
            resolve("Atlantis", gazetteer)


class TestCoordinates:
    def test_a_lat_lon_pair_is_taken_as_written(self, gazetteer):
        endpoint = resolve("32.7767,-96.7970", gazetteer)
        assert endpoint.point == pytest.approx((32.7767, -96.7970))

    def test_a_coordinate_is_labelled_with_the_nearest_place(self, gazetteer):
        assert resolve("32.78,-96.80", gazetteer).label == "Dallas, TX"

    def test_spacing_inside_a_pair_does_not_matter(self, gazetteer):
        assert resolve(" 32.7767 , -96.7970 ", gazetteer).point == pytest.approx(
            (32.7767, -96.7970)
        )

    def test_an_impossible_coordinate_is_rejected(self, gazetteer):
        with pytest.raises(PlaceNotFound, match="latitude"):
            resolve("91.0,-96.0", gazetteer)
        with pytest.raises(PlaceNotFound, match="longitude"):
            resolve("32.0,-200.0", gazetteer)


class TestContainment:
    def test_a_coordinate_with_no_us_place_near_it_is_rejected(self, gazetteer):
        with pytest.raises(OutsideUnitedStates, match="USA"):
            resolve("19.4326,-99.1332", gazetteer)

    def test_the_rejection_is_actionable_when_a_place_was_found_but_too_far(self, gazetteer):
        # 120 miles south of Dallas: inside the country, but no Gazetteer place
        # is within the containment radius of this toy Gazetteer.
        with pytest.raises(OutsideUnitedStates) as raised:
            resolve("31.0,-96.7970", gazetteer)
        assert "nearest" in str(raised.value).lower()
        assert raised.value.detail["nearest_place"] == "Dallas, TX"

    def test_mid_ocean_still_names_the_closest_us_place(self, gazetteer):
        with pytest.raises(OutsideUnitedStates) as raised:
            resolve("20.0,-150.0", gazetteer)
        assert raised.value.detail["nearest_place"] == "Honolulu, HI"
        assert "miles away" in str(raised.value)

    def test_an_empty_gazetteer_rejects_everything_without_crashing(self):
        with pytest.raises(OutsideUnitedStates):
            resolve_endpoint("32.0,-96.0", Gazetteer([]), containment_radius_miles=RADIUS)

    def test_a_canadian_province_is_named_as_outside_the_usa(self, gazetteer):
        with pytest.raises(OutsideUnitedStates, match="Ontario"):
            resolve("Toronto, ON", gazetteer)


class TestMalformedInput:
    @pytest.mark.parametrize("query", ["", "   ", ","])
    def test_an_empty_query_is_rejected(self, gazetteer, query):
        with pytest.raises(PlaceNotFound):
            resolve(query, gazetteer)

    def test_an_unknown_state_token_is_reported(self, gazetteer):
        with pytest.raises(PlaceNotFound, match="ZZ"):
            resolve("Springfield, ZZ", gazetteer)
