"""The routing provider, behind one module.

Swapping OSRM for another engine should touch this file and nothing else. The
public demo server is the default because it needs no API key, which is what
lets a fresh clone work; it is a development service on a car profile, and a
real deployment would self-host OSRM with an HGV profile.
"""

from dataclasses import dataclass

import requests

from apps.routing.exceptions import NoRouteFound, RoutingProviderUnavailable
from apps.routing.geo import Point

METRES_PER_MILE = 1609.344
SECONDS_PER_HOUR = 3600.0

#: Provider codes that mean "no such route", as opposed to "provider broken".
_NO_ROUTE_CODES = frozenset({"NoRoute", "NoSegment", "NoTrips"})


@dataclass(frozen=True)
class Route:
    """A driving route: its shape, its length, and what it cost to fetch."""

    geometry: list[Point]
    distance_miles: float
    duration_hours: float
    provider_calls: int

    @property
    def shape_points(self) -> int:
        return len(self.geometry)


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
            "geometries": "geojson",
            "alternatives": "false",
            "steps": "false",
        }

        attempts = 0
        last_error: Exception | None = None
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
                return _parse(payload, attempts)
            except (KeyError, IndexError, TypeError, ValueError) as error:
                last_error = error
                continue

        raise RoutingProviderUnavailable(
            f"The routing provider did not answer after {attempts} attempt(s).",
            provider_error=str(last_error),
            attempts=attempts,
        )


def _parse(payload: dict, provider_calls: int) -> Route:
    route = payload["routes"][0]
    coordinates = route["geometry"]["coordinates"]
    if len(coordinates) < 2:
        raise ValueError("route geometry has fewer than two points")
    return Route(
        # The provider speaks GeoJSON, which is longitude first.
        geometry=[Point(float(lat), float(lon)) for lon, lat in coordinates],
        distance_miles=float(route["distance"]) / METRES_PER_MILE,
        duration_hours=float(route["duration"]) / SECONDS_PER_HOUR,
        provider_calls=provider_calls,
    )


def _trim(value: float) -> str:
    """A coordinate as OSRM wants it: decimal, without trailing zeroes."""
    return f"{value:.6f}".rstrip("0").rstrip(".")
