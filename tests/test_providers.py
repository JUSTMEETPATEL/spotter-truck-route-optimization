"""The OSRM client: URL shape, parsing, retries and call counting."""

import pytest
import requests

from apps.routing.exceptions import NoRouteFound, RoutingProviderUnavailable
from apps.routing.geo import Point
from apps.routing.providers import OsrmClient

DALLAS = Point(32.7767, -96.7970)
CHICAGO = Point(41.8781, -87.6298)

OK_BODY = {
    "code": "Ok",
    "routes": [
        {
            "distance": 1555625.3,
            "duration": 61571.0,
            "geometry": {
                "type": "LineString",
                "coordinates": [[-96.797, 32.7767], [-90.0, 38.0], [-87.6298, 41.8781]],
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


@pytest.fixture
def recorder(monkeypatch):
    """Record every call the client makes and reply from a queue."""

    class Recorder:
        def __init__(self):
            self.urls: list[str] = []
            self.kwargs: list[dict] = []
            self.replies: list = [FakeResponse(OK_BODY)]

        def __call__(self, url, **kwargs):
            self.urls.append(url)
            self.kwargs.append(kwargs)
            reply = self.replies[min(len(self.urls) - 1, len(self.replies) - 1)]
            if isinstance(reply, Exception):
                raise reply
            return reply

    recorder = Recorder()
    monkeypatch.setattr(requests, "get", recorder)
    return recorder


def client(**kwargs) -> OsrmClient:
    defaults = {
        "base_url": "https://router.example.org",
        "timeout_seconds": 3.0,
        "retries": 1,
    }
    return OsrmClient(**(defaults | kwargs))


class TestRequestShape:
    def test_coordinates_go_out_longitude_first(self, recorder):
        client().route(DALLAS, CHICAGO)
        assert recorder.urls[0] == (
            "https://router.example.org/route/v1/driving/-96.797,32.7767;-87.6298,41.8781"
        )

    def test_the_full_geojson_geometry_is_requested(self, recorder):
        client().route(DALLAS, CHICAGO)
        assert recorder.kwargs[0]["params"] == {
            "overview": "full",
            "geometries": "geojson",
            "alternatives": "false",
            "steps": "false",
        }

    def test_the_timeout_is_always_set(self, recorder):
        client(timeout_seconds=7.5).route(DALLAS, CHICAGO)
        assert recorder.kwargs[0]["timeout"] == 7.5


class TestParsing:
    def test_metres_and_seconds_become_miles_and_hours(self, recorder):
        route = client().route(DALLAS, CHICAGO)
        assert route.distance_miles == pytest.approx(966.62, abs=0.01)
        assert route.duration_hours == pytest.approx(17.10, abs=0.01)

    def test_the_geometry_comes_back_latitude_first(self, recorder):
        route = client().route(DALLAS, CHICAGO)
        assert route.geometry[0] == Point(32.7767, -96.797)
        assert len(route.geometry) == 3

    def test_one_successful_request_is_one_external_call(self, recorder):
        assert client().route(DALLAS, CHICAGO).provider_calls == 1
        assert len(recorder.urls) == 1


class TestFailures:
    def test_no_route_is_not_a_provider_failure(self, recorder):
        recorder.replies = [FakeResponse({"code": "NoRoute", "routes": []})]
        with pytest.raises(NoRouteFound):
            client().route(DALLAS, CHICAGO)

    def test_no_route_arrives_as_a_four_hundred_and_is_still_a_no_route(self, recorder):
        # The real OSRM answers an impossible route with HTTP 400 and a body
        # naming the reason, which must not be mistaken for a broken provider.
        recorder.replies = [
            FakeResponse(
                {"message": "Impossible route between points", "code": "NoRoute"},
                status_code=400,
            )
        ]
        with pytest.raises(NoRouteFound):
            client().route(DALLAS, CHICAGO)
        assert len(recorder.urls) == 1

    def test_a_provider_contract_error_is_not_retried_either(self, recorder):
        recorder.replies = [FakeResponse({"code": "InvalidOptions"}, status_code=400)]
        with pytest.raises(RoutingProviderUnavailable, match="InvalidOptions"):
            client().route(DALLAS, CHICAGO)
        assert len(recorder.urls) == 1

    def test_a_missing_segment_is_also_no_route(self, recorder):
        recorder.replies = [FakeResponse({"code": "NoSegment"})]
        with pytest.raises(NoRouteFound):
            client().route(DALLAS, CHICAGO)

    def test_a_route_that_is_not_retried_is_not_retried(self, recorder):
        recorder.replies = [FakeResponse({"code": "NoRoute", "routes": []})]
        with pytest.raises(NoRouteFound):
            client().route(DALLAS, CHICAGO)
        assert len(recorder.urls) == 1

    def test_a_timeout_is_retried_once_and_then_gives_up(self, recorder):
        recorder.replies = [requests.Timeout("too slow")]
        with pytest.raises(RoutingProviderUnavailable):
            client().route(DALLAS, CHICAGO)
        assert len(recorder.urls) == 2

    def test_a_server_error_is_retried(self, recorder):
        recorder.replies = [FakeResponse({}, status_code=503)]
        with pytest.raises(RoutingProviderUnavailable):
            client().route(DALLAS, CHICAGO)
        assert len(recorder.urls) == 2

    def test_a_transient_failure_followed_by_success_counts_two_calls(self, recorder):
        recorder.replies = [requests.ConnectionError("reset"), FakeResponse(OK_BODY)]
        route = client().route(DALLAS, CHICAGO)
        assert route.provider_calls == 2

    def test_retries_can_be_switched_off(self, recorder):
        recorder.replies = [requests.Timeout("too slow")]
        with pytest.raises(RoutingProviderUnavailable):
            client(retries=0).route(DALLAS, CHICAGO)
        assert len(recorder.urls) == 1

    def test_a_malformed_payload_is_a_provider_failure(self, recorder):
        recorder.replies = [FakeResponse({"code": "Ok", "routes": [{"distance": 1.0}]})]
        with pytest.raises(RoutingProviderUnavailable):
            client().route(DALLAS, CHICAGO)
