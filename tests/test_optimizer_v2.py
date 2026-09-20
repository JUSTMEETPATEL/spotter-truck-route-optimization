"""The indexed optimizer: same answers as the scanning one, cheaper to compute.

The scanning optimizer is already checked against a linear program, so the
question here is equivalence, not optimality: every plan, Stop for Stop and
cent for cent, must match :mod:`apps.routing.optimizer`.
"""

import random
from typing import NamedTuple

import pytest

from apps.routing import optimizer as scanning
from apps.routing import optimizer_v2 as indexed
from apps.routing.optimizer_v2 import CheapestInRange, next_cheaper_indexes

RANGE = 500.0
MPG = 10.0


class Opportunity(NamedTuple):
    mile_marker: float
    price_per_gallon: float


class TestNextCheaperIndexes:
    def test_the_nearest_strictly_cheaper_candidate_ahead_is_found(self):
        prices = [4.0, 3.5, 3.8, 3.0, 3.2]
        candidates = [Opportunity(i * 100.0, price) for i, price in enumerate(prices)]
        assert next_cheaper_indexes(candidates) == [1, 3, 3, None, None]

    def test_equal_prices_do_not_count_as_cheaper(self):
        candidates = [Opportunity(i * 100.0, 3.0) for i in range(4)]
        assert next_cheaper_indexes(candidates) == [None, None, None, None]

    def test_a_single_candidate_has_nothing_ahead(self):
        assert next_cheaper_indexes([Opportunity(0.0, 3.0)]) == [None]

    def test_an_empty_list_is_an_empty_table(self):
        assert next_cheaper_indexes([]) == []

    def test_it_agrees_with_a_brute_force_scan_on_random_prices(self):
        rng = random.Random(7)
        for _ in range(200):
            candidates = [
                Opportunity(i * 10.0, round(rng.uniform(2.0, 4.0), 2))
                for i in range(rng.randint(1, 40))
            ]
            expected = [
                next(
                    (
                        j
                        for j in range(i + 1, len(candidates))
                        if candidates[j].price_per_gallon < candidates[i].price_per_gallon
                    ),
                    None,
                )
                for i in range(len(candidates))
            ]
            assert next_cheaper_indexes(candidates) == expected


class TestCheapestInRange:
    def test_it_finds_the_cheapest_in_the_window(self):
        prices = [4.0, 3.5, 3.8, 3.0, 3.2]
        candidates = [Opportunity(i * 100.0, price) for i, price in enumerate(prices)]
        table = CheapestInRange(candidates)
        assert table.query(0, 4) == 3
        assert table.query(0, 2) == 1
        assert table.query(2, 2) == 2
        assert table.query(1, 2) == 1

    def test_a_tie_goes_to_the_candidate_furthest_along(self):
        candidates = [Opportunity(0.0, 3.0), Opportunity(100.0, 3.0), Opportunity(200.0, 3.0)]
        assert CheapestInRange(candidates).query(0, 2) == 2

    def test_an_empty_range_is_rejected(self):
        with pytest.raises(ValueError, match="empty range"):
            CheapestInRange([Opportunity(0.0, 3.0)]).query(1, 0)

    def test_it_agrees_with_a_brute_force_minimum_on_random_data(self):
        rng = random.Random(11)
        for _ in range(100):
            size = rng.randint(1, 60)
            candidates = [
                Opportunity(i * 10.0, round(rng.uniform(2.0, 4.0), 2)) for i in range(size)
            ]
            table = CheapestInRange(candidates)
            for _ in range(20):
                left = rng.randrange(size)
                right = rng.randrange(left, size)
                expected = min(
                    range(left, right + 1),
                    key=lambda i: (candidates[i].price_per_gallon, -candidates[i].mile_marker),
                )
                assert table.query(left, right) == expected


def assert_same_plan(candidates, distance):
    """Both optimizers must agree on every field of the plan."""
    first = scanning.plan_purchases(candidates, distance, RANGE, MPG)
    second = indexed.plan_purchases(candidates, distance, RANGE, MPG)
    assert second.feasible == first.feasible
    assert second.infeasible_stretch == first.infeasible_stretch
    assert second.total_gallons == pytest.approx(first.total_gallons)
    assert second.stops_count == first.stops_count
    if first.total_cost_usd is None:
        assert second.total_cost_usd is None
    else:
        assert second.total_cost_usd == pytest.approx(first.total_cost_usd, rel=1e-12)
        assert second.naive_cost_usd == pytest.approx(first.naive_cost_usd, rel=1e-12)
    assert [
        (stop.sequence, stop.candidate.mile_marker, stop.gallons, stop.cost_usd)
        for stop in second.stops
    ] == pytest.approx(
        [
            (stop.sequence, stop.candidate.mile_marker, stop.gallons, stop.cost_usd)
            for stop in first.stops
        ]
    )


class TestEquivalence:
    @pytest.mark.parametrize(
        ("candidates", "distance"),
        [
            ([], 0.0),
            ([Opportunity(0.0, 3.5)], 0.0),
            ([Opportunity(0.0, 3.5)], 240.0),
            ([Opportunity(0.0, 4.0), Opportunity(300.0, 3.0)], 800.0),
            ([Opportunity(0.0, 3.0), Opportunity(100.0, 4.0)], 600.0),
            ([Opportunity(0.0, 4.0), Opportunity(100.0, 3.0), Opportunity(200.0, 2.0)], 300.0),
            ([Opportunity(0.0, 3.0), Opportunity(500.0, 3.0)], 1000.0),
            ([Opportunity(0.0, 3.0), Opportunity(501.0, 3.0)], 1000.0),
            ([Opportunity(0.0, 3.0)], 1200.0),
        ],
        ids=[
            "no-candidates",
            "zero-distance",
            "inside-range",
            "adr-counter-example",
            "rising",
            "falling",
            "gap-exactly-range",
            "gap-over-range",
            "infeasible",
        ],
    )
    def test_the_worked_cases_agree(self, candidates, distance):
        assert_same_plan(candidates, distance)

    def test_the_adversarial_layouts_agree(self):
        from tests.test_optimality import LAYOUTS

        assert len(LAYOUTS) >= 80
        for _, candidates, distance in LAYOUTS:
            assert_same_plan(candidates, distance)

    def test_random_layouts_agree(self):
        rng = random.Random(2026)
        for _ in range(300):
            count = rng.randint(1, 25)
            gap = rng.uniform(40.0, 480.0)
            candidates = [
                Opportunity(round(i * gap, 3), round(rng.uniform(2.1, 4.9), 4))
                for i in range(count)
            ]
            distance = candidates[-1].mile_marker + rng.uniform(0.0, 600.0)
            assert_same_plan(candidates, distance)

    def test_a_long_dense_route_agrees(self):
        # 400 Candidates every 8 miles: Range covers ~62 of them at once, which
        # is where the scanning optimizer does the most work.
        rng = random.Random(99)
        candidates = [Opportunity(i * 8.0, round(rng.uniform(2.5, 4.5), 4)) for i in range(400)]
        assert_same_plan(candidates, 3300.0)


class TestOptimalityIsPreserved:
    def test_the_indexed_plan_also_matches_the_linear_program(self):
        pytest.importorskip("scipy")
        from tests.test_optimality import LAYOUTS, lp_minimum_cost

        for _, candidates, distance in LAYOUTS[:40]:
            plan = indexed.plan_purchases(candidates, distance, RANGE, MPG)
            assert plan.total_cost_usd == pytest.approx(
                lp_minimum_cost(candidates, distance), rel=1e-6
            )
