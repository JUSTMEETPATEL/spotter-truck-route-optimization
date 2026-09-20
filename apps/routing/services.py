"""Orchestration and caching: the only module that knows the whole flow.

    resolve endpoints  committed Gazetteer   0 calls
    check the cache    LocMem or Redis       0 calls
    fetch the route    routing provider      1 call   <- the only egress
    match the Corridor in-memory Candidates  0 calls
    choose the Stops   greedy optimizer      0 calls
    price the Naive Driver                   0 calls
    serialise, cache, respond

Everything below this module is a component that could be reused or replaced on
its own; nothing below it knows there is an HTTP request involved.
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from apps.routing import optimizer_v2
from apps.routing.corridor import Candidate, MatchedCandidate, match_corridor
from apps.routing.exceptions import MapLinkUnavailable
from apps.routing.geo import Point, haversine_miles
from apps.routing.optimizer import FuelPlan, Stop, plan_purchases
from apps.routing.providers import OsrmClient, Route
from apps.routing.resolver import ResolvedEndpoint, resolve_endpoint
from apps.stations.geocoding import Gazetteer, load_gazetteer
from apps.stations.registry import candidates as registry_candidates

CACHE_KEY_TEMPLATE = "fuel-route:plan:{optimizer}:{token}"

#: The two implementations of the greedy rule. They return identical plans;
#: they differ in how they answer "cheaper Candidate ahead?" and "cheapest
#: within Range?" -- by scanning, or from precomputed indexes.
SCANNING = "scan"
INDEXED = "monotonic-stack+sparse-table"
OPTIMIZERS = {SCANNING: plan_purchases, INDEXED: optimizer_v2.plan_purchases}


@dataclass(frozen=True)
class RouteRequest:
    """A validated request: where to, and what the truck is."""

    start: str
    finish: str
    mpg: float
    max_range_miles: float
    max_detour_miles: float

    @property
    def tank_gallons(self) -> float:
        return tank_gallons(self.max_range_miles, self.mpg)


def tank_gallons(max_range_miles: float, mpg: float) -> float:
    """The most fuel the truck can hold: Range / mpg.

    Derived wherever it is shown, never stored. A stored constant would report
    50 gallons while the two fields beside it said otherwise.
    """
    return max_range_miles / mpg


class RouteProvider(Protocol):
    def route(self, start: Point, finish: Point) -> Route: ...


def plan_route(
    request: RouteRequest,
    *,
    optimizer: str = SCANNING,
    provider: RouteProvider | None = None,
    gazetteer: Gazetteer | None = None,
    corridor_candidates: list[Candidate] | None = None,
) -> dict[str, Any]:
    """The whole flow, from two place names to a cached, serialisable payload.

    The collaborators are injectable so that the components can be tested
    without a database, a network or a cache between them.
    """
    started = time.perf_counter()
    gazetteer = gazetteer if gazetteer is not None else load_gazetteer()
    radius = settings.US_CONTAINMENT_RADIUS_MILES
    start = resolve_endpoint(request.start, gazetteer, radius)
    finish = resolve_endpoint(request.finish, gazetteer, radius)

    token = route_token(start.point, finish.point, request)
    cached = cache.get(CACHE_KEY_TEMPLATE.format(optimizer=optimizer, token=token))
    if cached is not None:
        return cached | {
            "meta": cached["meta"]
            | {
                "cached": True,
                "external_api_calls": 0,
                # The stored figure is what the first caller waited for. This
                # one is what this caller waited for, which is the point of
                # reporting it.
                "compute_ms": _elapsed_ms(started),
            }
        }

    provider = provider if provider is not None else default_provider()
    route = provider.route(start.point, finish.point)

    all_candidates = (
        corridor_candidates if corridor_candidates is not None else registry_candidates()
    )
    matched = match_corridor(
        route.geometry,
        all_candidates,
        corridor_miles=request.max_detour_miles,
        thinning_miles=settings.CORRIDOR_THINNING_MILES,
        grid_cell_degrees=settings.CORRIDOR_GRID_CELL_DEGREES,
    )
    origin_fill = _origin_fill(start.point, all_candidates)
    sequence = _with_origin_fill(matched, origin_fill)

    plan = OPTIMIZERS[optimizer](
        sequence, route.distance_miles, request.max_range_miles, request.mpg
    )
    payload = _payload(request, start, finish, route, matched, sequence, plan, token, optimizer)
    payload["meta"]["compute_ms"] = _elapsed_ms(started)
    cache.set(
        CACHE_KEY_TEMPLATE.format(optimizer=optimizer, token=token),
        payload,
        settings.ROUTE_CACHE_TTL_SECONDS,
    )
    return payload


def plan_for_map(route_token: str, request: RouteRequest | None) -> dict[str, Any]:
    """The plan behind a map link.

    Served from the cache while it is there. Once it has expired the link can
    still be honoured if the request parameters travelled with it, which costs
    one routing call rather than 404-ing someone mid-demo. The recomputed plan
    has to hash back to the same token, or the link is not describing it.
    """
    for optimizer in OPTIMIZERS:
        cached = cache.get(CACHE_KEY_TEMPLATE.format(optimizer=optimizer, token=route_token))
        if cached is not None:
            return cached
    if request is None:
        raise MapLinkUnavailable(
            "This map link has expired. Re-run the route for a fresh one, or add "
            "?start=…&finish=… to this URL to recompute it.",
            route_token=route_token,
        )
    payload = plan_route(request)
    if payload["meta"]["route_token"] != route_token:
        raise MapLinkUnavailable(
            "Those parameters do not describe this route.", route_token=route_token
        )
    return payload


def route_token(start: Point, finish: Point, request: RouteRequest) -> str:
    """The identifier in a map URL, and simultaneously the cache key.

    A truncated SHA-256 of the resolved endpoints and vehicle parameters, with
    coordinates rounded first so that "Dallas", "dallas, tx" and
    "32.7767,-96.7970" collapse onto one entry. Deriving it from the request
    means the same request always yields the same map URL, with no token store
    to maintain.
    """
    decimals = settings.COORD_ROUNDING_DECIMALS
    parts = (
        f"{round(start.lat, decimals)},{round(start.lon, decimals)}",
        f"{round(finish.lat, decimals)},{round(finish.lon, decimals)}",
        f"{request.mpg:.4f}",
        f"{request.max_range_miles:.4f}",
        f"{request.max_detour_miles:.4f}",
    )
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return digest[: settings.ROUTE_TOKEN_LENGTH]


def default_provider() -> OsrmClient:
    """The configured routing provider."""
    return OsrmClient(
        base_url=settings.OSRM_BASE_URL,
        timeout_seconds=settings.OSRM_TIMEOUT_SECONDS,
        retries=settings.OSRM_RETRIES,
    )


def _origin_fill(start: Point, all_candidates: list[Candidate]) -> MatchedCandidate | None:
    """The cheapest Candidate near the start, placed at Mile Marker 0.

    The truck departs empty, so it must buy before it can move. Without this
    the first Corridor Candidate would be unreachable and every route would
    come back infeasible.
    """
    radius = settings.ORIGIN_FILL_RADIUS_MILES
    nearby = [(candidate, haversine_miles(start, candidate.point)) for candidate in all_candidates]
    within = [pair for pair in nearby if pair[1] <= radius]
    if not within:
        return None
    candidate, distance = min(within, key=lambda pair: (pair[0].price_per_gallon, pair[1]))
    return MatchedCandidate(candidate=candidate, mile_marker=0.0, detour_miles=distance)


def _with_origin_fill(
    matched: list[MatchedCandidate], origin_fill: MatchedCandidate | None
) -> list[MatchedCandidate]:
    """The Candidate sequence the optimizer sees, Origin Fill first.

    The Origin Fill's Station is dropped from the rest of the sequence when it
    appears there too: it is one truckstop, and at Mile Marker 0 it is reachable
    on an empty tank, which is the only place it can be bought.
    """
    if origin_fill is None:
        return list(matched)
    origin_id = origin_fill.candidate.opis_id
    rest = [entry for entry in matched if entry.candidate.opis_id != origin_id]
    return [origin_fill, *rest]


def _payload(
    request: RouteRequest,
    start: ResolvedEndpoint,
    finish: ResolvedEndpoint,
    route: Route,
    matched: list[MatchedCandidate],
    sequence: list[MatchedCandidate],
    plan: FuelPlan,
    token: str,
    optimizer: str,
) -> dict[str, Any]:
    """The response, as plain JSON-ready data so the cache stores it verbatim.

    The order is for the reader. The fuelling answer comes first and the route
    geometry last: the geometry runs to tens of thousands of points, so in
    Postman anything placed after it is about 100,000 lines down.
    """
    return {
        "fuel": {
            "feasible": plan.feasible,
            "infeasible_stretch": _stretch(plan, sequence, route.distance_miles),
            "total_gallons": round(plan.total_gallons, 2),
            "total_cost_usd": _round(plan.total_cost_usd, 2),
            "average_price_per_gallon": _round(plan.average_price_per_gallon, 3),
            "stops_count": plan.stops_count,
            "naive_cost_usd": _round(plan.naive_cost_usd, 2),
            "savings_usd": _round(plan.savings_usd, 2),
            "savings_percent": _round(plan.savings_percent, 2),
        },
        "fuel_stops": [_stop(stop) for stop in plan.stops],
        "request": {"start": _endpoint(start), "finish": _endpoint(finish)},
        "vehicle": {
            "max_range_miles": round(request.max_range_miles, 2),
            "mpg": round(request.mpg, 2),
            "tank_gallons": round(request.tank_gallons, 2),
            "max_detour_miles": round(request.max_detour_miles, 2),
        },
        "meta": {
            "external_api_calls": route.provider_calls,
            "cached": False,
            "compute_ms": None,
            "stations_in_corridor": sum(entry.candidate.stations_at_location for entry in matched),
            "candidates_considered": len(sequence),
            "map_url": reverse("route-map", kwargs={"route_token": token}),
            "route_token": token,
            "optimizer": optimizer,
        },
        "route": {
            "total_distance_miles": round(route.distance_miles, 2),
            "total_duration_hours": round(route.duration_hours, 2),
            "shape_points": route.shape_points,
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [round(point.lon, 5), round(point.lat, 5)] for point in route.geometry
                ],
            },
        },
    }


def _elapsed_ms(started: float) -> float:
    # Two decimals, because a warm cache hit is tens of microseconds and one
    # decimal would report it as 0.0.
    return round((time.perf_counter() - started) * 1000, 2)


def _endpoint(endpoint: ResolvedEndpoint) -> dict[str, Any]:
    return {
        "query": endpoint.query,
        "name": endpoint.label,
        "city": endpoint.city,
        "state": endpoint.state,
        "latitude": round(endpoint.point.lat, 6),
        "longitude": round(endpoint.point.lon, 6),
    }


def _stop(stop: Stop) -> dict[str, Any]:
    # A Stop wraps the matched Candidate, which wraps the Candidate itself:
    # the purchase, where it sits on this route, and the truckstop.
    # In production a Stop's Candidate is always one matched against this
    # route; the optimizer accepts anything priced so that it can be tested
    # with plain tuples.
    matched = cast(MatchedCandidate, stop.candidate)
    candidate = matched.candidate
    return {
        "sequence": stop.sequence,
        "opis_id": candidate.opis_id,
        "name": candidate.name,
        "city": candidate.city,
        "state": candidate.state,
        "latitude": round(candidate.point.lat, 6),
        "longitude": round(candidate.point.lon, 6),
        "mile_marker": round(matched.mile_marker, 1),
        "detour_miles_to_city": round(matched.detour_miles, 2),
        "stations_at_location": candidate.stations_at_location,
        "price_per_gallon": round(candidate.price_per_gallon, 4),
        "gallons": round(stop.gallons, 2),
        "cost_usd": round(stop.cost_usd, 2),
        "gallons_remaining_after": round(stop.gallons_remaining_after, 2),
    }


def _stretch(
    plan: FuelPlan, sequence: list[MatchedCandidate], total_distance_miles: float
) -> dict[str, Any] | None:
    """The infeasible stretch, with its ends named rather than indexed."""
    stretch = plan.infeasible_stretch
    if stretch is None:
        return None

    def label(index: int | None, mile: float, fallback: str) -> str:
        if index is None:
            return f"{fallback} (mile {mile:.1f})"
        candidate = sequence[index].candidate
        return f"{candidate.city}, {candidate.state} (mile {mile:.1f})"

    return {
        "from": label(stretch.from_index, stretch.from_mile, "start"),
        "to": label(stretch.to_index, stretch.to_mile, "destination"),
        "gap_miles": round(stretch.gap_miles, 1),
    }


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
