"""Orchestration: the Route Token, the call budget, and the caching policy."""

import pytest
from django.core.cache import cache

from apps.routing.exceptions import RoutingProviderUnavailable
from apps.routing.geo import Point
from apps.routing.providers import Route
from apps.routing.services import (
    CACHE_KEY_TEMPLATE,
    RouteRequest,
    plan_route,
    route_token,
)
from apps.stations import registry
from apps.stations.geocoding import Gazetteer, GazetteerPlace
from apps.stations.models import Station

pytestmark = pytest.mark.django_db

# A due-east route along the 40th parallel: 1,000 miles of straight line, with
# Stations every 200 miles so that a 500-mile Range is comfortably feasible.
ROUTE_START = Point(40.0, -100.0)
ROUTE_FINISH = Point(40.0, -81.6)


def straight_route(calls: int = 1) -> Route:
    geometry = [Point(40.0, -100.0 + degrees * 0.1) for degrees in range(185)]
    return Route(
        geometry=geometry,
        distance_miles=966.6,
        duration_hours=17.1,
        provider_calls=calls,
    )


class FakeProvider:
    """Stands in for OSRM, and counts what it was asked to do."""

    def __init__(self, route: Route | None = None, error: Exception | None = None):
        self._route = route or straight_route()
        self._error = error
        self.calls = 0

    def route(self, start: Point, finish: Point) -> Route:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._route


def gazetteer() -> Gazetteer:
    return Gazetteer(
        [
            GazetteerPlace("WESTVILLE", "KS", ROUTE_START, 30.0, 3),
            GazetteerPlace("EASTVILLE", "OH", ROUTE_FINISH, 30.0, 3),
        ]
    )


def station(opis_id: str, price: float, lat: float, lon: float, city: str = "Midtown") -> Station:
    return Station.objects.create(
        opis_id=opis_id,
        name=f"TRUCKSTOP {opis_id}",
        address="EXIT 1",
        city=city,
        state="KS",
        latitude=lat,
        longitude=lon,
        price_mean=price,
        price_min=price,
        price_max=price,
        price_sample_count=1,
    )


@pytest.fixture(autouse=True)
def _clean_slate():
    cache.clear()
    registry.reset()
    yield
    cache.clear()
    registry.reset()


@pytest.fixture
def stations():
    # One at the origin so the Origin Fill has a price, then every 2 degrees
    # (about 106 miles) along the route.
    station("1", 3.50, 40.0, -100.0, city="Westville")
    station("2", 2.90, 40.0, -98.0)
    station("3", 3.10, 40.0, -96.0)
    station("4", 2.70, 40.0, -94.0)
    station("5", 3.30, 40.0, -90.0)
    station("6", 3.05, 40.0, -86.0)
    station("7", 3.40, 40.0, -82.0)


def request_for(start: str = "Westville, KS", finish: str = "Eastville, OH", **overrides):
    defaults = {"mpg": 10.0, "max_range_miles": 500.0, "max_detour_miles": 10.0}
    return RouteRequest(start=start, finish=finish, **(defaults | overrides))


def run(request=None, provider=None):
    provider = provider or FakeProvider()
    payload = plan_route(request or request_for(), provider=provider, gazetteer=gazetteer())
    return payload, provider


class TestCallBudget:
    def test_an_uncached_request_costs_exactly_one_external_call(self, stations):
        payload, provider = run()
        assert provider.calls == 1
        assert payload["meta"]["external_api_calls"] == 1
        assert payload["meta"]["cached"] is False

    def test_a_repeated_request_costs_none(self, stations):
        payload, provider = run()
        repeated, second_provider = run()
        assert second_provider.calls == 0
        assert repeated["meta"]["external_api_calls"] == 0
        assert repeated["meta"]["cached"] is True

    def test_a_retry_inside_the_provider_is_reported_honestly(self, stations):
        payload, _ = run(provider=FakeProvider(route=straight_route(calls=2)))
        assert payload["meta"]["external_api_calls"] == 2


class TestRouteToken:
    def test_the_same_request_always_yields_the_same_token(self):
        assert route_token(ROUTE_START, ROUTE_FINISH, request_for()) == route_token(
            ROUTE_START, ROUTE_FINISH, request_for()
        )

    def test_the_token_is_the_configured_length(self, settings):
        settings.ROUTE_TOKEN_LENGTH = 12
        assert len(route_token(ROUTE_START, ROUTE_FINISH, request_for())) == 12

    def test_different_vehicle_parameters_are_different_tokens(self):
        base = route_token(ROUTE_START, ROUTE_FINISH, request_for())
        assert route_token(ROUTE_START, ROUTE_FINISH, request_for(mpg=8.0)) != base
        assert route_token(ROUTE_START, ROUTE_FINISH, request_for(max_range_miles=400.0)) != base
        assert route_token(ROUTE_START, ROUTE_FINISH, request_for(max_detour_miles=25.0)) != base

    def test_swapping_start_and_finish_is_a_different_token(self):
        assert route_token(ROUTE_START, ROUTE_FINISH, request_for()) != route_token(
            ROUTE_FINISH, ROUTE_START, request_for()
        )

    def test_coordinates_that_round_together_share_a_token(self):
        nudged = Point(ROUTE_START.lat + 0.000001, ROUTE_START.lon)
        assert route_token(nudged, ROUTE_FINISH, request_for()) == route_token(
            ROUTE_START, ROUTE_FINISH, request_for()
        )

    def test_a_name_and_its_coordinates_share_a_token(self, stations):
        by_name, _ = run(request_for(start="Westville, KS"))
        by_coordinate, provider = run(request_for(start="40.0,-100.0"))
        assert by_name["meta"]["map_url"] == by_coordinate["meta"]["map_url"]
        # Which is the point of it: the second request was already cached.
        assert provider.calls == 0


class TestCachingPolicy:
    def test_a_repeat_returns_an_identical_payload(self, stations):
        first, _ = run()
        second, _ = run()
        assert second | {"meta": first["meta"]} == first

    def test_an_infeasible_result_is_cached_too(self):
        # No Stations at all, so nothing can be bought anywhere.
        first, _ = run()
        assert first["fuel"]["feasible"] is False
        second, provider = run()
        assert provider.calls == 0
        assert second["fuel"]["feasible"] is False

    def test_a_provider_failure_is_never_cached(self, stations):
        failing = FakeProvider(error=RoutingProviderUnavailable("down"))
        with pytest.raises(RoutingProviderUnavailable):
            run(provider=failing)
        payload, provider = run()
        assert provider.calls == 1
        assert payload["meta"]["cached"] is False


class TestOriginFill:
    def test_the_first_stop_sits_at_mile_zero(self, stations):
        payload, _ = run()
        assert payload["fuel_stops"][0]["mile_marker"] == 0.0

    def test_the_origin_fill_is_the_cheapest_station_near_the_start(self, stations):
        station("8", 3.20, 40.05, -100.05, city="Nearby")  # ~4 miles off the start
        station("9", 2.10, 41.0, -100.0, city="Faraway")  # ~69 miles away, so out of radius
        payload, _ = run()
        first = payload["fuel_stops"][0]
        assert first["opis_id"] == "8"
        assert first["mile_marker"] == 0.0
        assert first["detour_miles_to_city"] == pytest.approx(4.4, abs=0.2)

    def test_a_station_beyond_the_origin_radius_cannot_be_the_origin_fill(self, settings):
        # Only Station is 4 miles off the start, and the radius is 2, so there
        # is nowhere to buy the first gallon.
        settings.ORIGIN_FILL_RADIUS_MILES = 2.0
        station("8", 3.20, 40.05, -100.05, city="Nearby")
        payload, _ = run()
        assert payload["fuel"]["feasible"] is False

    def test_nothing_near_the_start_is_infeasible_from_the_start(self):
        station("5", 3.30, 40.0, -90.0)
        payload, _ = run()
        assert payload["fuel"]["feasible"] is False
        stretch = payload["fuel"]["infeasible_stretch"]
        assert stretch["from"] == "start (mile 0.0)"
        assert stretch["gap_miles"] > 0

    def test_the_origin_fill_station_is_not_also_listed_further_along(self, stations):
        payload, _ = run()
        # Station 1 sits on the route at mile 0 and would otherwise appear
        # twice: once synthesised, once matched.
        appearances = [stop for stop in payload["fuel_stops"] if stop["opis_id"] == "1"]
        assert len(appearances) <= 1


class TestDisclosure:
    def test_the_corridor_counts_stations_and_the_sequence_counts_candidates(self):
        # Two Stations on one coordinate collapse to one Candidate.
        station("1", 3.50, 40.0, -100.0, city="Westville")
        station("2", 3.60, 40.0, -100.0, city="Westville")
        station("3", 2.90, 40.0, -98.0)
        payload, _ = run()
        assert payload["meta"]["stations_in_corridor"] == 3
        assert payload["meta"]["candidates_considered"] == 2

    def test_compute_ms_is_what_this_caller_waited_for(self, stations):
        first, _ = run()
        assert first["meta"]["compute_ms"] > 0

        key = CACHE_KEY_TEMPLATE.format(
            optimizer=first["meta"]["optimizer"], token=first["meta"]["route_token"]
        )
        stored = cache.get(key)
        second, _ = run()
        # A warm cache hit can be faster than the reporting resolution, so
        # this is the honest bound.
        assert second["meta"]["compute_ms"] >= 0
        # The cached entry keeps the first caller's figure untouched, and the
        # repeat reports its own rather than replaying that one.
        assert stored["meta"]["compute_ms"] == first["meta"]["compute_ms"]
        assert cache.get(key)["meta"]["compute_ms"] == first["meta"]["compute_ms"]

    def test_the_map_url_carries_the_route_token(self, stations):
        payload, _ = run()
        assert payload["meta"]["route_token"] in payload["meta"]["map_url"]

    def test_the_tank_is_derived_from_range_and_mpg(self, stations):
        payload, _ = run(request_for(mpg=8.0, max_range_miles=400.0))
        assert payload["vehicle"]["tank_gallons"] == pytest.approx(50.0)
        payload, _ = run(request_for(mpg=5.0, max_range_miles=400.0))
        assert payload["vehicle"]["tank_gallons"] == pytest.approx(80.0)


class TestFuelFigures:
    def test_a_feasible_plan_reports_cost_gallons_and_savings(self, stations):
        payload, _ = run()
        fuel = payload["fuel"]
        assert fuel["feasible"] is True
        assert fuel["total_gallons"] == pytest.approx(96.66, abs=0.01)
        assert fuel["total_cost_usd"] > 0
        assert fuel["naive_cost_usd"] >= fuel["total_cost_usd"]
        assert fuel["savings_usd"] == pytest.approx(
            fuel["naive_cost_usd"] - fuel["total_cost_usd"], abs=0.02
        )
        assert fuel["average_price_per_gallon"] == pytest.approx(
            fuel["total_cost_usd"] / fuel["total_gallons"], abs=0.01
        )

    def test_the_stops_spend_what_the_total_says(self, stations):
        payload, _ = run()
        assert sum(stop["cost_usd"] for stop in payload["fuel_stops"]) == pytest.approx(
            payload["fuel"]["total_cost_usd"], abs=0.02
        )
        assert sum(stop["gallons"] for stop in payload["fuel_stops"]) == pytest.approx(
            payload["fuel"]["total_gallons"], abs=0.02
        )

    def test_an_infeasible_plan_reports_gallons_but_no_cost(self):
        payload, _ = run()
        fuel = payload["fuel"]
        assert fuel["feasible"] is False
        assert fuel["total_cost_usd"] is None
        assert fuel["naive_cost_usd"] is None
        assert fuel["stops_count"] == 0
        assert fuel["total_gallons"] == pytest.approx(96.66, abs=0.01)

    def test_an_infeasible_stretch_names_the_city_it_leaves_from(self):
        station("1", 3.50, 40.0, -100.0, city="Westville")
        payload, _ = run()
        stretch = payload["fuel"]["infeasible_stretch"]
        assert stretch["from"] == "Westville, KS (mile 0.0)"
        assert stretch["to"] == "destination (mile 966.6)"
        assert stretch["gap_miles"] == pytest.approx(966.6, abs=0.1)
