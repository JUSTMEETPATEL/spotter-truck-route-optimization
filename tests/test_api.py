"""The HTTP surface: validation, bounds, error codes, and the map page."""

import pytest
import requests
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from apps.routing.geo import Point
from apps.stations import geocoding, registry
from apps.stations.geocoding import Gazetteer, GazetteerPlace
from apps.stations.models import Station

pytestmark = pytest.mark.django_db

START = Point(40.0, -100.0)
FINISH = Point(40.0, -81.6)

OSRM_BODY = {
    "code": "Ok",
    "routes": [
        {
            "distance": 1555625.3,
            "duration": 61571.0,
            "geometry": {
                "type": "LineString",
                "coordinates": [[-100.0 + step * 0.1, 40.0] for step in range(185)],
            },
        }
    ],
}


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def provider(monkeypatch):
    """Patch the provider at the HTTP boundary and count its calls."""

    class Counter:
        def __init__(self):
            self.calls = 0
            self.reply = FakeResponse(OSRM_BODY)

        def __call__(self, url, **kwargs):
            self.calls += 1
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    counter = Counter()
    monkeypatch.setattr(requests, "get", counter)
    return counter


@pytest.fixture(autouse=True)
def _offline_gazetteer(monkeypatch):
    """A tiny Gazetteer, so the tests never read the committed 8 MB file."""
    gazetteer = Gazetteer(
        [
            GazetteerPlace("WESTVILLE", "KS", START, 30.0, 3),
            GazetteerPlace("EASTVILLE", "OH", FINISH, 30.0, 3),
            GazetteerPlace("TORONTO", "OH", Point(40.46, -80.60), 3.0, 3),
        ]
    )
    monkeypatch.setattr(geocoding, "load_gazetteer", lambda: gazetteer)
    monkeypatch.setattr("apps.routing.services.load_gazetteer", lambda: gazetteer)
    cache.clear()
    registry.reset()
    yield
    cache.clear()
    registry.reset()


@pytest.fixture
def stations():
    for index, (opis_id, price, lon) in enumerate(
        [
            ("1", 3.50, -100.0),
            ("2", 2.90, -98.0),
            ("3", 2.70, -94.0),
            ("4", 3.30, -90.0),
            ("5", 3.05, -86.0),
            ("6", 3.40, -82.0),
        ]
    ):
        Station.objects.create(
            opis_id=opis_id,
            name=f"TRUCKSTOP {opis_id}",
            address=f"EXIT {index}",
            city="Midtown" if lon != -100.0 else "Westville",
            state="KS",
            latitude=40.0,
            longitude=lon,
            price_mean=price,
            price_min=price,
            price_max=price,
            price_sample_count=1,
        )


@pytest.fixture
def client() -> APIClient:
    return APIClient()


def post(client, **payload):
    body = {"start": "Westville, KS", "finish": "Eastville, OH"} | payload
    return client.post(reverse("route"), body, format="json")


class TestRoutePlanning:
    def test_a_valid_request_returns_a_plan(self, client, provider, stations):
        response = post(client)
        assert response.status_code == 200
        body = response.json()
        assert body["fuel"]["feasible"] is True
        assert body["fuel"]["total_cost_usd"] > 0
        assert body["route"]["shape_points"] == 185
        assert body["meta"]["external_api_calls"] == 1
        assert provider.calls == 1

    def test_the_same_request_over_get_works_for_a_browser(self, client, provider, stations):
        response = client.get(
            reverse("route"), {"start": "Westville, KS", "finish": "Eastville, OH"}
        )
        assert response.status_code == 200
        assert response.json()["fuel"]["feasible"] is True

    def test_a_cached_repeat_makes_no_external_call(self, client, provider, stations):
        post(client)
        response = post(client)
        assert provider.calls == 1
        assert response.json()["meta"]["cached"] is True
        assert response.json()["meta"]["external_api_calls"] == 0

    def test_the_payload_is_identical_on_a_repeat(self, client, provider, stations):
        first = post(client).json()
        second = post(client).json()
        assert second | {"meta": first["meta"]} == first

    def test_infeasible_fuelling_is_an_answer_not_an_error(self, client, provider):
        response = post(client)
        assert response.status_code == 200
        assert response.json()["fuel"]["feasible"] is False
        assert response.json()["fuel"]["infeasible_stretch"]["gap_miles"] > 500


class TestValidation:
    def test_a_missing_finish_is_rejected(self, client, provider):
        response = client.post(reverse("route"), {"start": "Westville, KS"}, format="json")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert "finish" in response.json()["error"]["detail"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("mpg", 0.9),
            ("mpg", 20.1),
            ("max_range_miles", 49.0),
            ("max_range_miles", 1001.0),
            ("max_detour_miles", -1.0),
            ("max_detour_miles", 50.1),
        ],
    )
    def test_a_parameter_beyond_its_bound_is_rejected(self, client, provider, field, value):
        response = post(client, **{field: value})
        assert response.status_code == 400
        assert field in response.json()["error"]["detail"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("mpg", 1.0),
            ("mpg", 20.0),
            ("max_range_miles", 50.0),
            ("max_range_miles", 1000.0),
            ("max_detour_miles", 0.0),
            ("max_detour_miles", 50.0),
        ],
    )
    def test_a_parameter_at_its_bound_is_accepted(self, client, provider, stations, field, value):
        assert post(client, **{field: value}).status_code == 200

    def test_the_tank_tracks_mpg_and_range(self, client, provider, stations):
        body = post(client, mpg=8.0, max_range_miles=400.0).json()
        assert body["vehicle"]["tank_gallons"] == 50.0
        assert body["vehicle"]["mpg"] == 8.0

    def test_an_unknown_place_is_a_four_hundred_naming_it(self, client, provider):
        response = post(client, start="Atlantis, KS")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "place_not_found"
        assert "Atlantis" in response.json()["error"]["message"]

    def test_a_point_outside_the_usa_is_a_four_hundred(self, client, provider):
        response = post(client, start="19.4326,-99.1332")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "outside_united_states"

    def test_a_canadian_province_is_a_four_hundred(self, client, provider):
        response = post(client, finish="Toronto, ON")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "outside_united_states"
        assert "Ontario" in response.json()["error"]["message"]


class TestProviderFailures:
    def test_no_road_route_is_a_four_oh_four(self, client, provider, stations):
        provider.reply = FakeResponse({"code": "NoRoute", "routes": []})
        response = post(client)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "no_route_found"

    def test_an_unreachable_provider_is_a_five_oh_three(self, client, provider, stations):
        provider.reply = requests.Timeout("gone")
        response = post(client)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "routing_provider_unavailable"

    def test_a_five_oh_three_is_not_cached(self, client, provider, stations):
        provider.reply = requests.Timeout("gone")
        assert post(client).status_code == 503
        provider.reply = FakeResponse(OSRM_BODY)
        assert post(client).status_code == 200


class TestMapPage:
    def test_the_map_url_from_a_plan_renders(self, client, provider, stations):
        plan = post(client).json()
        response = client.get(plan["meta"]["map_url"])
        assert response.status_code == 200
        assert b"leaflet" in response.content.lower()
        assert plan["request"]["start"]["name"].encode() in response.content

    def test_an_expired_link_with_parameters_recomputes(self, client, provider, stations):
        plan = post(client).json()
        cache.clear()
        response = client.get(
            plan["meta"]["map_url"], {"start": "Westville, KS", "finish": "Eastville, OH"}
        )
        assert response.status_code == 200
        assert provider.calls == 2

    def test_an_expired_link_without_parameters_says_so(self, client, provider, stations):
        plan = post(client).json()
        cache.clear()
        assert client.get(plan["meta"]["map_url"]).status_code == 404

    def test_parameters_that_do_not_match_the_token_are_refused(self, client, provider, stations):
        plan = post(client).json()
        cache.clear()
        response = client.get(
            plan["meta"]["map_url"], {"start": "Eastville, OH", "finish": "Westville, KS"}
        )
        assert response.status_code == 404


class TestOtherEndpoints:
    def test_health_reports_what_is_loaded(self, client, stations):
        body = client.get(reverse("health")).json()
        assert body["status"] == "ok"
        assert body["stations_loaded"] == 6
        assert body["candidates"] == 6
        assert body["backends"]["database"] == "sqlite"
        assert body["vehicle_defaults"]["tank_gallons"] == 50.0

    def test_health_publishes_the_excluded_count(self, client):
        body = client.get(reverse("health")).json()
        assert "stations_excluded" in body["dataset"]
        assert "excluded_cities" in body["dataset"]

    def test_stations_can_be_browsed_and_filtered(self, client, stations):
        body = client.get(reverse("station-list")).json()
        assert body["count"] == 6
        filtered = client.get(reverse("station-list"), {"state": "ks"}).json()
        assert filtered["count"] == 6
        searched = client.get(reverse("station-list"), {"search": "truckstop 3"}).json()
        assert searched["count"] == 1
        assert searched["results"][0]["opis_id"] == "3"

    def test_the_schema_is_published(self, client):
        assert client.get(reverse("schema")).status_code == 200
        assert client.get(reverse("swagger-ui")).status_code == 200
