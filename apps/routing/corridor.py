"""Matching Candidates to the band either side of a route.

Two passes. A coarse pass thins the route to one point every
``thinning_miles``, buckets those points into a degree grid, and tests each
Candidate against its own cell and the surrounding ring with flat-earth
arithmetic -- rejecting the large majority. An exact pass then projects the
survivors onto the original geometry's segments, which both measures the Detour
properly and yields the Mile Marker from the along-segment fraction.

Imports nothing from Django: plain data in, plain data out.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from apps.routing.geo import (
    MILES_PER_DEGREE_LATITUDE,
    Point,
    haversine_miles,
    planar_offsets_miles,
)


@dataclass(frozen=True)
class Candidate:
    """One fuel-buying opportunity: a coordinate and the cheapest Station on it.

    Co-located Stations collapse into one Candidate at build time, so
    ``stations_at_location`` records how many this one stands for.
    """

    point: Point
    opis_id: str
    name: str
    city: str
    state: str
    price_per_gallon: float
    stations_at_location: int


@dataclass(frozen=True)
class MatchedCandidate:
    """A Candidate that lies in the Corridor, with its Mile Marker and Detour."""

    candidate: Candidate
    mile_marker: float
    detour_miles: float

    @property
    def price_per_gallon(self) -> float:
        return self.candidate.price_per_gallon


def opis_sort_key(opis_id: str) -> tuple[int, int, str]:
    """Ascending order for OPIS Truckstop IDs, numerically where they are numeric.

    Two Candidates can share a Mile Marker, and the cheapest Station at a
    coordinate can tie on price. Both are broken by OPIS ID ascending so that a
    repeated request returns a byte-identical payload. Plain string order would
    put "1000" before "999", which is ascending by accident only.
    """
    return (0, int(opis_id), "") if opis_id.isdigit() else (1, 0, opis_id)


def cumulative_miles(geometry: Sequence[Point]) -> list[float]:
    """Distance along the geometry to each of its shape points."""
    distances = [0.0]
    for index in range(len(geometry) - 1):
        distances.append(distances[-1] + haversine_miles(geometry[index], geometry[index + 1]))
    return distances


def route_length_miles(geometry: Sequence[Point]) -> float:
    """Length of the geometry in miles."""
    if len(geometry) < 2:
        return 0.0
    return cumulative_miles(geometry)[-1]


def match_corridor(
    geometry: Sequence[Point],
    candidates: Iterable[Candidate],
    corridor_miles: float,
    thinning_miles: float,
    grid_cell_degrees: float,
) -> list[MatchedCandidate]:
    """Candidates within ``corridor_miles`` of the route, ordered by Mile Marker."""
    if len(geometry) < 2:
        return []

    cumulative = cumulative_miles(geometry)
    thinned = _thin(geometry, cumulative, thinning_miles)
    grid = _bucket(thinned, grid_cell_degrees)
    # Thinning moves the sampled route off the true one by up to the spacing,
    # so the coarse pass has to be that much more generous or it would drop
    # Candidates the exact pass would have kept.
    slack_miles = corridor_miles + thinning_miles

    matched: list[MatchedCandidate] = []
    for candidate in candidates:
        if not _near_thinned_route(candidate.point, grid, grid_cell_degrees, slack_miles):
            continue
        detour_miles, mile_marker = _project_onto_geometry(candidate.point, geometry, cumulative)
        if detour_miles <= corridor_miles:
            matched.append(
                MatchedCandidate(
                    candidate=candidate, mile_marker=mile_marker, detour_miles=detour_miles
                )
            )

    matched.sort(key=lambda m: (m.mile_marker, opis_sort_key(m.candidate.opis_id)))
    return matched


# --- coarse pass ------------------------------------------------------------

_GridCell = tuple[int, int]


def _thin(
    geometry: Sequence[Point], cumulative: Sequence[float], thinning_miles: float
) -> list[Point]:
    """Resample the route to one point roughly every ``thinning_miles``.

    Resampling rather than dropping points, so the spacing guarantee holds even
    where the provider returns two shape points hundreds of miles apart.
    """
    if thinning_miles <= 0.0:
        return list(geometry)

    thinned = [geometry[0]]
    next_mile = thinning_miles
    for index in range(len(geometry) - 1):
        start_mile, end_mile = cumulative[index], cumulative[index + 1]
        segment_miles = end_mile - start_mile
        while next_mile <= end_mile:
            fraction = 0.0 if segment_miles == 0.0 else (next_mile - start_mile) / segment_miles
            thinned.append(_interpolate(geometry[index], geometry[index + 1], fraction))
            next_mile += thinning_miles
    thinned.append(geometry[-1])
    return thinned


def _bucket(points: Sequence[Point], cell_degrees: float) -> dict[_GridCell, list[Point]]:
    grid: dict[_GridCell, list[Point]] = {}
    for point in points:
        grid.setdefault(_cell_of(point, cell_degrees), []).append(point)
    return grid


def _cell_of(point: Point, cell_degrees: float) -> _GridCell:
    return (
        math.floor(point.lat / cell_degrees),
        math.floor(point.lon / cell_degrees),
    )


def _near_thinned_route(
    point: Point,
    grid: dict[_GridCell, list[Point]],
    cell_degrees: float,
    slack_miles: float,
) -> bool:
    lat_cell, lon_cell = _cell_of(point, cell_degrees)
    lat_ring, lon_ring = _ring_radius(point, cell_degrees, slack_miles)
    for d_lat in range(-lat_ring, lat_ring + 1):
        for d_lon in range(-lon_ring, lon_ring + 1):
            for route_point in grid.get((lat_cell + d_lat, lon_cell + d_lon), ()):
                if _planar_within(point, route_point, slack_miles):
                    return True
    return False


def _ring_radius(point: Point, cell_degrees: float, slack_miles: float) -> tuple[int, int]:
    """How many cells out the coarse pass must look, per axis.

    One ring of neighbours is enough for a 10-mile Corridor against 0.5-degree
    cells, but ``max_detour_miles`` goes to 50 and longitude cells shrink
    towards the poles, so the radius is computed rather than assumed.
    """
    lat_miles_per_cell = cell_degrees * MILES_PER_DEGREE_LATITUDE
    # Take the longitude scale at the far edge of the search band, where cells
    # are narrowest, so the radius is never an underestimate.
    worst_lat = min(89.0, abs(point.lat) + slack_miles / MILES_PER_DEGREE_LATITUDE + cell_degrees)
    lon_miles_per_cell = lat_miles_per_cell * max(math.cos(math.radians(worst_lat)), 1e-6)
    return (
        math.ceil(slack_miles / lat_miles_per_cell),
        math.ceil(slack_miles / lon_miles_per_cell),
    )


def _planar_within(a: Point, b: Point, miles: float) -> bool:
    east, north = planar_offsets_miles(a, b)
    return east * east + north * north <= miles * miles


# --- exact pass -------------------------------------------------------------


def _project_onto_geometry(
    point: Point, geometry: Sequence[Point], cumulative: Sequence[float]
) -> tuple[float, float]:
    """Distance from ``point`` to the route, and the Mile Marker of the foot."""
    best_distance = math.inf
    best_mile = 0.0
    for index in range(len(geometry) - 1):
        start, end = geometry[index], geometry[index + 1]
        fraction = _projection_fraction(start, end, point)
        foot = _interpolate(start, end, fraction)
        distance = haversine_miles(point, foot)
        if distance < best_distance:
            best_distance = distance
            best_mile = cumulative[index] + fraction * (cumulative[index + 1] - cumulative[index])
    return best_distance, best_mile


def _projection_fraction(start: Point, end: Point, point: Point) -> float:
    """Where along ``start``->``end`` the point projects, clamped to [0, 1]."""
    seg_east, seg_north = planar_offsets_miles(start, end)
    seg_length_sq = seg_east * seg_east + seg_north * seg_north
    if seg_length_sq == 0.0:
        return 0.0
    to_east, to_north = planar_offsets_miles(start, point)
    fraction = (to_east * seg_east + to_north * seg_north) / seg_length_sq
    return min(1.0, max(0.0, fraction))


def _interpolate(start: Point, end: Point, fraction: float) -> Point:
    return Point(
        start.lat + (end.lat - start.lat) * fraction,
        start.lon + (end.lon - start.lon) * fraction,
    )
