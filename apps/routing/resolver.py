"""Resolving a start or finish into a coordinate, entirely offline.

Accepts ``"City, ST"``, ``"City, State"``, a bare city name (the largest match
in the country) or a ``"lat,lon"`` pair. Every form is answered from the
committed Gazetteer, so resolving two endpoints costs zero network calls.

US containment is tested by asking for the nearest US place within a radius,
rather than with a bounding box: a box around the lower 48 swallows Ontario,
Windsor and much of northern Mexico. The radius test cannot separate Detroit
from Windsor either -- two miles apart -- so the rejection names the place it
found and lets the caller see what was matched.
"""

import re
from dataclasses import dataclass

from apps.routing.exceptions import OutsideUnitedStates, PlaceNotFound
from apps.routing.geo import Point, haversine_miles
from apps.stations.cleaning import CANADIAN_PROVINCE_NAMES, CANADIAN_PROVINCES
from apps.stations.geocoding import US_STATE_CODES, Gazetteer

_COORDINATE_PAIR = re.compile(
    r"^\s*(?P<lat>[-+]?\d+(?:\.\d+)?)\s*,\s*(?P<lon>[-+]?\d+(?:\.\d+)?)\s*$"
)

#: USPS codes for the states and territories, plus their spelled-out names.
_STATE_BY_NAME = {name.upper(): code for name, code in US_STATE_CODES.items()}
_STATE_CODES = frozenset(US_STATE_CODES.values())


@dataclass(frozen=True)
class ResolvedEndpoint:
    """Where a start or finish actually is, and what to call it."""

    query: str
    label: str
    point: Point
    city: str | None
    state: str | None


def resolve_endpoint(
    query: str, gazetteer: Gazetteer, containment_radius_miles: float
) -> ResolvedEndpoint:
    """Resolve one endpoint, or raise the reason it cannot be resolved."""
    text = (query or "").strip()
    if not text.strip(" ,"):
        raise PlaceNotFound("A start and a finish are both required.")

    coordinates = _COORDINATE_PAIR.match(text)
    if coordinates is not None:
        return _resolve_coordinate(
            text,
            float(coordinates["lat"]),
            float(coordinates["lon"]),
            gazetteer,
            containment_radius_miles,
        )
    return _resolve_place_name(text, gazetteer)


def _resolve_coordinate(
    query: str,
    latitude: float,
    longitude: float,
    gazetteer: Gazetteer,
    containment_radius_miles: float,
) -> ResolvedEndpoint:
    if not -90.0 <= latitude <= 90.0:
        raise PlaceNotFound(f"{latitude} is not a valid latitude.", value=latitude)
    if not -180.0 <= longitude <= 180.0:
        raise PlaceNotFound(f"{longitude} is not a valid longitude.", value=longitude)

    point = Point(latitude, longitude)
    nearest = gazetteer.nearest(point, containment_radius_miles)
    if nearest is None:
        # Nothing within the radius, so this is not a US endpoint. Name the
        # closest US place anyway, so the caller can see what was matched.
        closest = gazetteer.nearest(point)
        raise OutsideUnitedStates(
            f"{query} is not inside the USA: no US place lies within "
            f"{containment_radius_miles:.0f} miles. "
            + (
                f"The nearest one is {closest.label}, "
                f"{haversine_miles(point, closest.point):,.0f} miles away."
                if closest
                else "The Gazetteer is empty."
            ),
            nearest_place=closest.label if closest else None,
        )
    return ResolvedEndpoint(
        query=query,
        label=nearest.label,
        point=point,
        city=nearest.name.title(),
        state=nearest.state,
    )


def _resolve_place_name(query: str, gazetteer: Gazetteer) -> ResolvedEndpoint:
    city, _, state_token = (part.strip() for part in query.partition(","))
    if not city:
        raise PlaceNotFound(f"Could not read a place name from {query!r}.")

    if not state_token:
        found = gazetteer.resolve_name(city)
        if found is None:
            raise PlaceNotFound(f"{city} is not a place this service knows.", query=query)
        return _endpoint(query, found)

    state = _state_code(state_token)
    if state in CANADIAN_PROVINCES:
        raise OutsideUnitedStates(
            f"{CANADIAN_PROVINCE_NAMES.get(state, state)} is not in the USA, and this service "
            "plans routes inside the USA only.",
            nearest_place=None,
        )
    if state not in _STATE_CODES:
        raise PlaceNotFound(f"{state_token} is not a US state.", query=query)

    found = gazetteer.resolve(city, state)
    if found is None:
        raise PlaceNotFound(
            f"{city.title()}, {state} is not a place this service knows.", query=query
        )
    return _endpoint(query, found)


def _state_code(token: str) -> str:
    """A state code from either a two-letter code or a spelled-out name."""
    cleaned = token.strip().upper()
    return _STATE_BY_NAME.get(cleaned, cleaned)


def _endpoint(query: str, found) -> ResolvedEndpoint:
    return ResolvedEndpoint(
        query=query,
        label=found.label,
        point=found.point,
        city=found.name.title(),
        state=found.state,
    )
