"""The OSRM client: URL shape, parsing, retries, call counting and the road cache."""

import pytest
import requests

from apps.routing.exceptions import NoRouteFound, RoutingProviderUnavailable
from apps.routing.geo import Point
from apps.routing.providers import (
    CachingRouteProvider,
    OsrmClient,
    Route,
    decode_polyline,
    encode_polyline,
)
from tests.conftest import FakeResponse
from tests.conftest import encode_polyline as reference_encode

DALLAS = Point(32.7767, -96.7970)
CHICAGO = Point(41.8781, -87.6298)

#: Dallas, a midpoint, and Chicago, encoded the way OSRM returns them.
OK_GEOMETRY = "ku`gEftxmQsd{^gpnh@cmtVw|mM"

OK_BODY = {
    "code": "Ok",
    "routes": [{"distance": 1555625.3, "duration": 61571.0, "geometry": OK_GEOMETRY}],
}


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


class TestPolylineDecoding:
    def test_the_worked_example_from_the_polyline_specification(self):
        # The canonical example in Google's Encoded Polyline Algorithm Format,
        # which is the encoding OSRM uses at precision 5.
        assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [
            Point(38.5, -120.2),
            Point(40.7, -120.95),
            Point(43.252, -126.453),
        ]

    def test_an_empty_string_is_no_points(self):
        assert decode_polyline("") == []

    def test_negative_and_small_deltas_survive_the_round_trip(self):
        # One point south-west of the last, a tenth of a degree away.
        assert decode_polyline("_p~iF~ps|U~oR~oR") == [
            Point(38.5, -120.2),
            Point(38.4, -120.3),
        ]


class TestRequestShape:
    def test_coordinates_go_out_longitude_first(self, recorder):
        client().route(DALLAS, CHICAGO)
        assert recorder.urls[0] == (
            "https://router.example.org/route/v1/driving/-96.797,32.7767;-87.6298,41.8781"
        )

    def test_the_full_geometry_is_requested_as_a_polyline(self, recorder):
        # Polyline carries the identical points in 84% fewer bytes, which is
        # ~190ms off a Dallas-Chicago request.
        client().route(DALLAS, CHICAGO)
        assert recorder.kwargs[0]["params"] == {
            "overview": "full",
            "geometries": "polyline",
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

    def test_the_geometry_comes_back_decoded_latitude_first(self, recorder):
        route = client().route(DALLAS, CHICAGO)
        assert route.geometry[0] == pytest.approx((32.7767, -96.797))
        assert route.geometry[-1] == pytest.approx((41.8781, -87.6298))
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


class TestPolylineEncoding:
    """The encoder the road cache stores geometry with."""

    def test_the_worked_example_from_the_polyline_specification(self):
        assert (
            encode_polyline([Point(38.5, -120.2), Point(40.7, -120.95), Point(43.252, -126.453)])
            == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
        )

    def test_it_round_trips_the_geometry_the_provider_returned(self):
        points = decode_polyline(OK_GEOMETRY)
        assert encode_polyline(points) == OK_GEOMETRY

    def test_it_agrees_with_the_independent_encoder_in_conftest(self):
        # conftest's encoder was written separately, to build provider stubs.
        points = [Point(32.7767, -96.797), Point(35.1, -92.4), Point(41.8781, -87.6298)]
        assert encode_polyline(points) == reference_encode(
            [(point.lat, point.lon) for point in points]
        )

    def test_an_empty_route_encodes_to_nothing(self):
        assert encode_polyline([]) == ""


class FakeInnerProvider:
    """A RouteProvider that counts what the cache let through to it."""

    def __init__(self, route: Route | None = None, error: Exception | None = None):
        self._route = route or Route(
            geometry=decode_polyline(OK_GEOMETRY),
            distance_miles=966.6,
            duration_hours=17.1,
            provider_calls=1,
        )
        self._error = error
        self.calls = 0

    def route(self, start: Point, finish: Point) -> Route:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._route


def caching(inner: FakeInnerProvider, **overrides) -> CachingRouteProvider:
    defaults = {
        "cache": DictCache(),
        "ttl_seconds": 86400,
        "coord_decimals": 4,
        "namespace": "https://router.example.org",
    }
    return CachingRouteProvider(inner, **(defaults | overrides))


class DictCache:
    """The slice of the Django cache interface the wrapper uses."""

    def __init__(self):
        self.store: dict[str, object] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, timeout=None):
        self.store[key] = value


class TestRoadRouteCaching:
    def test_the_first_call_reaches_the_provider(self):
        inner = FakeInnerProvider()
        route = caching(inner).route(DALLAS, CHICAGO)
        assert inner.calls == 1
        assert route.provider_calls == 1

    def test_the_second_call_for_the_same_pair_does_not(self):
        inner = FakeInnerProvider()
        provider = caching(inner)
        provider.route(DALLAS, CHICAGO)
        route = provider.route(DALLAS, CHICAGO)
        assert inner.calls == 1
        assert route.provider_calls == 0

    def test_a_cached_route_is_returned_intact(self):
        inner = FakeInnerProvider()
        provider = caching(inner)
        first = provider.route(DALLAS, CHICAGO)
        second = provider.route(DALLAS, CHICAGO)
        assert second.geometry == first.geometry
        assert second.distance_miles == first.distance_miles
        assert second.duration_hours == first.duration_hours

    def test_a_different_pair_is_a_different_entry(self):
        inner = FakeInnerProvider()
        provider = caching(inner)
        provider.route(DALLAS, CHICAGO)
        provider.route(CHICAGO, DALLAS)
        assert inner.calls == 2

    def test_coordinates_that_round_together_share_an_entry(self):
        inner = FakeInnerProvider()
        provider = caching(inner)
        provider.route(DALLAS, CHICAGO)
        provider.route(Point(DALLAS.lat + 0.000001, DALLAS.lon), CHICAGO)
        assert inner.calls == 1

    def test_a_different_provider_does_not_reuse_the_entry(self):
        shared = DictCache()
        first = FakeInnerProvider()
        second = FakeInnerProvider()
        caching(first, cache=shared).route(DALLAS, CHICAGO)
        caching(second, cache=shared, namespace="https://other.example.org").route(DALLAS, CHICAGO)
        assert second.calls == 1

    def test_a_provider_failure_is_not_cached(self):
        inner = FakeInnerProvider(error=RoutingProviderUnavailable("down"))
        provider = caching(inner)
        for _ in range(2):
            with pytest.raises(RoutingProviderUnavailable):
                provider.route(DALLAS, CHICAGO)
        assert inner.calls == 2

    def test_an_impossible_route_is_not_cached_either(self):
        inner = FakeInnerProvider(error=NoRouteFound("nowhere"))
        provider = caching(inner)
        for _ in range(2):
            with pytest.raises(NoRouteFound):
                provider.route(DALLAS, CHICAGO)
        assert inner.calls == 2

    def test_the_entry_is_stored_under_the_configured_ttl(self):
        class RecordingCache(DictCache):
            timeout = None

            def set(self, key, value, timeout=None):
                self.timeout = timeout
                super().set(key, value, timeout)

        store = RecordingCache()
        caching(FakeInnerProvider(), cache=store, ttl_seconds=60).route(DALLAS, CHICAGO)
        assert store.timeout == 60

    def test_geometry_is_stored_encoded_rather_than_as_points(self):
        # 33 KB of string beats ~9,000 pickled Points in LocMem or Redis.
        store = DictCache()
        caching(FakeInnerProvider(), cache=store).route(DALLAS, CHICAGO)
        (entry,) = store.store.values()
        assert entry["geometry"] == OK_GEOMETRY
