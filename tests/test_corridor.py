"""Corridor matching, checked against a brute-force reference.

The reference projects every Candidate onto every segment of the original
geometry. The matcher under test pre-filters with a thinned, grid-bucketed
coarse pass first; the agreement test is what proves the pre-filter drops
nothing it should have kept.
"""

import json
import math
import pathlib
import random

import pytest

from apps.routing.corridor import (
    Candidate,
    MatchedCandidate,
    cumulative_miles,
    match_corridor,
    opis_sort_key,
    route_length_miles,
)
from apps.routing.geo import (
    MILES_PER_DEGREE_LATITUDE,
    Point,
    haversine_miles,
    planar_offsets_miles,
)

THINNING = 1.0
CELL = 0.5
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "dallas_chicago_route.json"


def candidate_at(point: Point, price: float = 3.0, opis_id: str = "1") -> Candidate:
    return Candidate(
        point=point,
        opis_id=opis_id,
        name=f"STATION {opis_id}",
        city="Somewhere",
        state="TX",
        price_per_gallon=price,
        stations_at_location=1,
    )


def brute_force_match(
    geometry: list[Point], candidates: list[Candidate], corridor_miles: float
) -> list[MatchedCandidate]:
    """Every Candidate against every segment, the slow and obvious way."""
    cumulative = cumulative_miles(geometry)
    matched: list[MatchedCandidate] = []
    for candidate in candidates:
        best_distance = math.inf
        best_mile = 0.0
        for index in range(len(geometry) - 1):
            start, end = geometry[index], geometry[index + 1]
            seg_east, seg_north = planar_offsets_miles(start, end)
            to_east, to_north = planar_offsets_miles(start, candidate.point)
            seg_length_sq = seg_east**2 + seg_north**2
            if seg_length_sq == 0.0:
                fraction = 0.0
            else:
                dot = to_east * seg_east + to_north * seg_north
                fraction = min(1.0, max(0.0, dot / seg_length_sq))
            foot = Point(
                start.lat + (end.lat - start.lat) * fraction,
                start.lon + (end.lon - start.lon) * fraction,
            )
            distance = haversine_miles(candidate.point, foot)
            if distance < best_distance:
                best_distance = distance
                best_mile = cumulative[index] + fraction * (
                    cumulative[index + 1] - cumulative[index]
                )
        if best_distance <= corridor_miles:
            matched.append(
                MatchedCandidate(
                    candidate=candidate, mile_marker=best_mile, detour_miles=best_distance
                )
            )
    # Same tie-break as the matcher: Candidates can share a Mile Marker, and
    # the order of equals is a determinism rule, not a matching question.
    return sorted(matched, key=lambda m: (m.mile_marker, opis_sort_key(m.candidate.opis_id)))


def load_fixture_geometry() -> list[Point]:
    payload = json.loads(FIXTURE.read_text())
    return [Point(lat, lon) for lon, lat in payload["geometry"]["coordinates"]]


# A due-east route along the 40th parallel, one point every degree.
STRAIGHT = [Point(40.0, -100.0 + i) for i in range(6)]


class TestMileMarkers:
    def test_the_route_length_is_the_sum_of_its_segments(self):
        assert route_length_miles(STRAIGHT) == pytest.approx(
            sum(haversine_miles(STRAIGHT[i], STRAIGHT[i + 1]) for i in range(len(STRAIGHT) - 1))
        )

    def test_a_candidate_on_the_route_takes_its_distance_along_it(self):
        third_point_mile = cumulative_miles(STRAIGHT)[2]
        [matched] = match_corridor(STRAIGHT, [candidate_at(STRAIGHT[2])], 10.0, THINNING, CELL)
        assert matched.mile_marker == pytest.approx(third_point_mile, abs=0.01)
        assert matched.detour_miles == pytest.approx(0.0, abs=0.01)

    def test_results_are_ordered_by_mile_marker(self):
        candidates = [
            candidate_at(Point(40.0, -97.5), opis_id="c"),
            candidate_at(Point(40.0, -99.5), opis_id="a"),
            candidate_at(Point(40.0, -98.5), opis_id="b"),
        ]
        matched = match_corridor(STRAIGHT, candidates, 10.0, THINNING, CELL)
        assert [m.candidate.opis_id for m in matched] == ["a", "b", "c"]
        assert [m.mile_marker for m in matched] == sorted(m.mile_marker for m in matched)

    def test_a_candidate_past_the_finish_projects_onto_the_end(self):
        beyond = candidate_at(Point(40.0, -94.95))
        [matched] = match_corridor(STRAIGHT, [beyond], 10.0, THINNING, CELL)
        assert matched.mile_marker == pytest.approx(route_length_miles(STRAIGHT), abs=0.01)


class TestCorridorWidth:
    def test_a_candidate_inside_the_corridor_matches(self):
        inside = candidate_at(Point(40.07, -98.0))  # about 4.8 miles north
        assert len(match_corridor(STRAIGHT, [inside], 10.0, THINNING, CELL)) == 1

    def test_a_candidate_outside_the_corridor_does_not(self):
        outside = candidate_at(Point(40.3, -98.0))  # about 20.7 miles north
        assert match_corridor(STRAIGHT, [outside], 10.0, THINNING, CELL) == []

    def test_the_corridor_edge_is_inclusive(self):
        ten_miles_north = Point(40.0 + 10.0 / MILES_PER_DEGREE_LATITUDE, -98.0)
        assert len(match_corridor(STRAIGHT, [candidate_at(ten_miles_north)], 10.0, THINNING, CELL))

    def test_a_zero_width_corridor_keeps_only_candidates_on_the_line(self):
        on_line = candidate_at(STRAIGHT[1], opis_id="on")
        off_line = candidate_at(Point(40.05, -98.5), opis_id="off")
        matched = match_corridor(STRAIGHT, [on_line, off_line], 0.0, THINNING, CELL)
        assert [m.candidate.opis_id for m in matched] == ["on"]


class TestDeterminism:
    def test_opis_ids_order_numerically_not_lexically(self):
        assert sorted(["1000", "999", "7"], key=opis_sort_key) == ["7", "999", "1000"]

    def test_candidates_sharing_a_mile_marker_order_by_opis_id(self):
        # Two Candidates beyond the finish both project onto the end point.
        beyond = [
            candidate_at(Point(40.02, -94.9), opis_id="1000"),
            candidate_at(Point(39.98, -94.9), opis_id="999"),
        ]
        matched = match_corridor(STRAIGHT, beyond, 10.0, THINNING, CELL)
        assert [m.candidate.opis_id for m in matched] == ["999", "1000"]


class TestSegmentProjection:
    def test_a_candidate_between_two_distant_shape_points_is_found(self):
        # Two points 380 miles apart with a Candidate beside the midpoint: the
        # nearest shape point is 190 miles away, so only segment projection
        # finds it.
        sparse = [Point(40.0, -100.0), Point(40.0, -93.0)]
        midway = candidate_at(Point(40.05, -96.5))
        [matched] = match_corridor(sparse, [midway], 10.0, THINNING, CELL)
        assert matched.detour_miles == pytest.approx(3.45, abs=0.2)
        assert matched.mile_marker == pytest.approx(route_length_miles(sparse) / 2, rel=0.01)

    def test_repeated_shape_points_do_not_divide_by_zero(self):
        doubled = [Point(40.0, -100.0), Point(40.0, -100.0), Point(40.0, -99.0)]
        candidates = [candidate_at(Point(40.0, -99.5))]
        assert len(match_corridor(doubled, candidates, 10.0, THINNING, CELL)) == 1


class TestBruteForceAgreement:
    @staticmethod
    @pytest.fixture(scope="class")
    def geometry() -> list[Point]:
        return load_fixture_geometry()

    @staticmethod
    @pytest.fixture(scope="class")
    def scattered_candidates() -> list[Candidate]:
        """Points scattered over the route's bounding box, widened a little."""
        rng = random.Random(4242)
        return [
            candidate_at(
                Point(rng.uniform(30.0, 44.0), rng.uniform(-99.0, -85.0)),
                price=round(rng.uniform(2.2, 4.4), 4),
                opis_id=str(i),
            )
            for i in range(600)
        ]

    @pytest.mark.parametrize("corridor_miles", [0.5, 10.0, 50.0])
    def test_matches_the_brute_force_reference(
        self, geometry, scattered_candidates, corridor_miles
    ):
        expected = brute_force_match(geometry, scattered_candidates, corridor_miles)
        actual = match_corridor(geometry, scattered_candidates, corridor_miles, THINNING, CELL)
        assert [m.candidate.opis_id for m in actual] == [m.candidate.opis_id for m in expected]
        for got, want in zip(actual, expected, strict=True):
            assert got.mile_marker == pytest.approx(want.mile_marker, abs=0.05)
            assert got.detour_miles == pytest.approx(want.detour_miles, abs=0.05)

    def test_a_wide_corridor_still_agrees_when_the_grid_ring_must_widen(
        self, geometry, scattered_candidates
    ):
        # A 50-mile corridor is wider than a 0.5-degree grid cell, so the
        # coarse pass has to search more than the eight immediate neighbours.
        expected = brute_force_match(geometry, scattered_candidates, 50.0)
        actual = match_corridor(geometry, scattered_candidates, 50.0, THINNING, 0.25)
        assert [m.candidate.opis_id for m in actual] == [m.candidate.opis_id for m in expected]

    def test_thinning_spacing_is_a_speed_knob_not_an_accuracy_one(
        self, geometry, scattered_candidates
    ):
        baseline = match_corridor(geometry, scattered_candidates, 10.0, 1.0, CELL)
        coarser = match_corridor(geometry, scattered_candidates, 10.0, 5.0, CELL)
        assert [m.candidate.opis_id for m in coarser] == [m.candidate.opis_id for m in baseline]
