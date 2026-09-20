"""The routing provider, behind one module.

Swapping OSRM for another engine should touch this file and nothing else. The
public demo server is the default because it needs no API key, which is what
lets a fresh clone work; it is a development service on a car profile, and a
real deployment would self-host OSRM with an HGV profile.

``CachingRouteProvider`` wraps any of them. It is here rather than in
``services.py`` because remembering a road route is a fact about the provider,
not about the flow; the cache and its tunables are handed in, so this module
still imports nothing from Django.
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from apps.routing.exceptions import NoRouteFound, RoutingProviderUnavailable
from apps.routing.geo import Point

METRES_PER_MILE = 1609.344
SECONDS_PER_HOUR = 3600.0

#: Decimal places the provider's encoded polylines carry. Five is the default
#: for OSRM and for Google's Encoded Polyline Algorithm Format, and is about a
#: metre -- far finer than the city-centroid coordinates it is matched against.
POLYLINE_PRECISION = 5

#: Provider codes that mean "no such route", as opposed to "provider broken".
_NO_ROUTE_CODES = frozenset({"NoRoute", "NoSegment", "NoTrips"})

ROAD_CACHE_KEY_TEMPLATE = "fuel-route:road:{token}"

#: Characters of the road cache digest kept. Sixteen hex characters is 64 bits;
#: this is an internal key with no URL to appear in, so it only has to not
#: collide, not be short.
_ROAD_TOKEN_LENGTH = 16


@dataclass(frozen=True)
class Route:
    """A driving route: its shape, its length, and what it cost to fetch."""

    geometry: list[Point]
    distance_miles: float
    duration_hours: float
    provider_calls: int
    #: Wall-clock milliseconds spent talking to the provider, retries included,
    #: so a slow answer can be blamed on the network rather than guessed at.
    #: Zero when the route came from a cache and nothing left the process.
    provider_ms: float = 0.0

    @property
    def shape_points(self) -> int:
        return len(self.geometry)


class RouteProvider(Protocol):
    def route(self, start: Point, finish: Point) -> Route: ...


class Cache(Protocol):
    """The slice of the Django cache interface the road cache uses."""

    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any, timeout: int | None = ...) -> Any: ...


class CachingRouteProvider:
    """Remembers road routes independently of the truck driving them.

    The plan cache upstream is keyed on the vehicle parameters, because the
    plan depends on them. The road does not: change ``mpg`` from 10 to 8 and
    the route between the same two places is byte-identical, yet the plan cache
    misses and the provider is called again. That call is 93% of an uncached
    request, so this layer is where the time actually goes.

    Only successes are stored. A provider that is down, and a pair of points no
    road connects, are both answers that might differ next time or that would
    otherwise be remembered for a day; the existing rule that failures never
    cache is kept.
    """

    def __init__(
        self,
        inner: RouteProvider,
        *,
        cache: Cache,
        ttl_seconds: int,
        coord_decimals: int,
        namespace: str,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._ttl_seconds = ttl_seconds
        self._coord_decimals = coord_decimals
        self._namespace = namespace

    def route(self, start: Point, finish: Point) -> Route:
        key = self._key(start, finish)
        stored = self._cache.get(key)
        if stored is not None:
            return Route(
                geometry=decode_polyline(stored["geometry"]),
                distance_miles=stored["distance_miles"],
                duration_hours=stored["duration_hours"],
                # Nothing left the process, and the meta block says so.
                provider_calls=0,
                provider_ms=0.0,
            )

        route = self._inner.route(start, finish)
        self._cache.set(
            key,
            {
                # Encoded, not decoded: the same points in 33 KB of string
                # rather than ~9,000 pickled Points, and about a millisecond
                # to decode again.
                "geometry": encode_polyline(route.geometry),
                "distance_miles": route.distance_miles,
                "duration_hours": route.duration_hours,
            },
            self._ttl_seconds,
        )
        return route

    def _key(self, start: Point, finish: Point) -> str:
        """A digest of the rounded endpoints, under the provider's namespace.

        The namespace is part of it because a self-hosted HGV profile and the
        public car profile answer the same question differently, and a stale
        entry from one must not be served as the other.
        """
        decimals = self._coord_decimals
        parts = (
            self._namespace,
            f"{round(start.lat, decimals)},{round(start.lon, decimals)}",
            f"{round(finish.lat, decimals)},{round(finish.lon, decimals)}",
        )
        digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
        return ROAD_CACHE_KEY_TEMPLATE.format(token=digest[:_ROAD_TOKEN_LENGTH])


class OsrmClient:
    """Fetches driving routes from an OSRM server.

    One request per route, with an explicit timeout and a bounded number of
    retries: demo servers wobble, and a retry is honestly reported in
    ``provider_calls`` rather than hidden.
    """

    def __init__(self, base_url: str, timeout_seconds: float, retries: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._retries = max(0, retries)

    def route(self, start: Point, finish: Point) -> Route:
        """The driving route between two coordinates."""
        url = (
            f"{self._base_url}/route/v1/driving/"
            f"{_trim(start.lon)},{_trim(start.lat)};{_trim(finish.lon)},{_trim(finish.lat)}"
        )
        params = {
            "overview": "full",
            # Polyline rather than GeoJSON: the identical points, 84% fewer
            # bytes on the wire, and roughly 190ms off a 1,000-mile route.
            "geometries": "polyline",
            "alternatives": "false",
            "steps": "false",
        }

        attempts = 0
        last_error: Exception | None = None
        # Started before the first attempt and read at whatever exit is taken,
        # so a timeout that is retried is counted in full rather than lost.
        began = time.perf_counter()
        while attempts <= self._retries:
            attempts += 1
            try:
                response = requests.get(url, params=params, timeout=self._timeout_seconds)
                # The body is read before the status is judged: OSRM answers an
                # impossible route with HTTP 400 and a code that says so, and
                # that is an answer about the route, not a broken provider.
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                last_error = error
                continue

            code = payload.get("code") if isinstance(payload, dict) else None
            if code in _NO_ROUTE_CODES:
                # A definite answer, so there is nothing to retry.
                raise NoRouteFound("No road route connects those two points.", provider_code=code)
            if response.status_code >= 500:
                # Server-side wobble, which is exactly what a retry is for.
                last_error = requests.HTTPError(f"HTTP {response.status_code}")
                continue
            if code != "Ok":
                # Any other named code is the provider refusing the request
                # itself. Retrying the same request would get the same refusal.
                raise RoutingProviderUnavailable(
                    f"The routing provider refused the request: {code}.",
                    provider_code=code,
                    status_code=response.status_code,
                )
            try:
                return _parse(payload, attempts, _ms_since(began))
            except (KeyError, IndexError, TypeError, ValueError) as error:
                last_error = error
                continue

        raise RoutingProviderUnavailable(
            f"The routing provider did not answer after {attempts} attempt(s).",
            provider_error=str(last_error),
            attempts=attempts,
        )


def decode_polyline(encoded: str, precision: int = POLYLINE_PRECISION) -> list[Point]:
    """Decode an encoded polyline into points, latitude first.

    The format stores each coordinate as a delta from the one before it, in
    chunks of five bits with a continuation flag, sign-encoded by shifting left
    one bit and inverting when negative.
    """
    points: list[Point] = []
    scale = 10.0**precision
    index = 0
    lat = lon = 0
    length = len(encoded)

    while index < length:
        for axis in range(2):
            result = 0
            shift = 0
            while True:
                if index >= length:
                    raise ValueError("encoded polyline ended mid-coordinate")
                chunk = ord(encoded[index]) - 63
                index += 1
                result |= (chunk & 0x1F) << shift
                shift += 5
                if chunk < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        points.append(Point(round(lat / scale, precision), round(lon / scale, precision)))
    return points


def encode_polyline(points: list[Point], precision: int = POLYLINE_PRECISION) -> str:
    """Encode points into an encoded polyline, the inverse of ``decode_polyline``.

    Lossless for geometry that came out of ``decode_polyline``, which has
    already rounded to ``precision`` places.
    """
    encoded: list[str] = []
    scale = 10**precision
    last_lat = last_lon = 0

    for point in points:
        lat, lon = round(point.lat * scale), round(point.lon * scale)
        for delta in (lat - last_lat, lon - last_lon):
            # Sign-encoded: shifted left one bit, inverted when negative.
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                encoded.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            encoded.append(chr(value + 63))
        last_lat, last_lon = lat, lon
    return "".join(encoded)


def _parse(payload: dict, provider_calls: int, provider_ms: float) -> Route:
    route = payload["routes"][0]
    geometry = decode_polyline(route["geometry"])
    if len(geometry) < 2:
        raise ValueError("route geometry has fewer than two points")
    return Route(
        geometry=geometry,
        distance_miles=float(route["distance"]) / METRES_PER_MILE,
        duration_hours=float(route["duration"]) / SECONDS_PER_HOUR,
        provider_calls=provider_calls,
        provider_ms=provider_ms,
    )


def _ms_since(began: float) -> float:
    return round((time.perf_counter() - began) * 1000, 2)


def _trim(value: float) -> str:
    """A coordinate as OSRM wants it: decimal, without trailing zeroes."""
    return f"{value:.6f}".rstrip("0").rstrip(".")
