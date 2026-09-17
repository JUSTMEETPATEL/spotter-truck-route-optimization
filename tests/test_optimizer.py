"""Minimum-cost refuelling: feasibility, purchases, and the Naive Driver."""

from typing import NamedTuple

import pytest

from apps.routing.optimizer import find_infeasible_stretch, naive_cost, plan_purchases

RANGE = 500.0
MPG = 10.0


class Opportunity(NamedTuple):
    """The only two fields the optimizer reads off a matched Candidate."""

    mile_marker: float
    price_per_gallon: float


class TestFeasibility:
    def test_a_zero_distance_route_is_feasible_with_no_candidates(self):
        assert find_infeasible_stretch([], 0.0, RANGE) is None

    def test_no_candidates_on_a_route_that_needs_fuel_is_infeasible(self):
        stretch = find_infeasible_stretch([], 120.0, RANGE)
        assert stretch is not None
        assert stretch.from_index is None
        assert stretch.to_index is None
        assert stretch.from_mile == 0.0
        assert stretch.to_mile == 120.0
        assert stretch.gap_miles == 120.0

    def test_a_truck_departing_empty_cannot_reach_a_candidate_off_mile_zero(self):
        # No Origin Fill: the tank is empty at mile 0, so even a nearby
        # Candidate is out of reach.
        stretch = find_infeasible_stretch([Opportunity(0.5, 3.0)], 100.0, RANGE)
        assert stretch is not None
        assert (stretch.from_index, stretch.to_index) == (None, 0)
        assert stretch.gap_miles == pytest.approx(0.5)

    def test_a_gap_between_candidates_wider_than_range_is_infeasible(self):
        candidates = [Opportunity(0.0, 3.0), Opportunity(600.0, 3.0), Opportunity(700.0, 3.0)]
        stretch = find_infeasible_stretch(candidates, 800.0, RANGE)
        assert stretch is not None
        assert (stretch.from_index, stretch.to_index) == (0, 1)
        assert stretch.gap_miles == 600.0

    def test_the_final_leg_to_the_destination_is_checked_too(self):
        candidates = [Opportunity(0.0, 3.0), Opportunity(300.0, 3.0), Opportunity(612.4, 3.0)]
        stretch = find_infeasible_stretch(candidates, 1184.9, RANGE)
        assert stretch is not None
        assert stretch.from_index == 2
        assert stretch.to_index is None
        assert stretch.from_mile == pytest.approx(612.4)
        assert stretch.to_mile == pytest.approx(1184.9)
        assert stretch.gap_miles == pytest.approx(572.5)

    def test_a_gap_of_exactly_range_is_feasible(self):
        candidates = [Opportunity(0.0, 3.0), Opportunity(500.0, 3.0)]
        assert find_infeasible_stretch(candidates, 1000.0, RANGE) is None

    def test_a_gap_one_mile_over_range_is_infeasible(self):
        candidates = [Opportunity(0.0, 3.0), Opportunity(501.0, 3.0)]
        assert find_infeasible_stretch(candidates, 1000.0, RANGE) is not None

    def test_the_first_gap_that_exceeds_range_is_the_one_reported(self):
        candidates = [Opportunity(0.0, 3.0), Opportunity(700.0, 3.0), Opportunity(1500.0, 3.0)]
        stretch = find_infeasible_stretch(candidates, 1600.0, RANGE)
        assert stretch is not None
        assert (stretch.from_index, stretch.to_index) == (0, 1)


class TestPurchases:
    def test_a_zero_distance_route_buys_nothing(self):
        plan = plan_purchases([Opportunity(0.0, 3.0)], 0.0, RANGE, MPG)
        assert plan.feasible is True
        assert plan.stops == ()
        assert plan.total_gallons == 0.0
        assert plan.total_cost_usd == 0.0

    def test_a_route_inside_range_buys_just_the_fuel_it_burns(self):
        # 240 miles at 10 mpg is 24 gallons; 24 * 3.50 = 84.00.
        plan = plan_purchases([Opportunity(0.0, 3.50)], 240.0, RANGE, MPG)
        assert plan.total_gallons == pytest.approx(24.0)
        assert plan.total_cost_usd == pytest.approx(84.00)
        assert len(plan.stops) == 1
        stop = plan.stops[0]
        assert stop.sequence == 1
        assert stop.gallons == pytest.approx(24.0)
        assert stop.cost_usd == pytest.approx(84.00)
        assert stop.gallons_remaining_after == pytest.approx(24.0)

    def test_the_origin_fill_buys_just_enough_rather_than_filling(self):
        # ADR-0001's counter-example: $4.00 at mile 0, $3.00 at mile 300,
        # destination at mile 800. Just enough costs $270; a full tank at the
        # origin costs $290.
        candidates = [Opportunity(0.0, 4.00), Opportunity(300.0, 3.00)]
        plan = plan_purchases(candidates, 800.0, RANGE, MPG)
        assert plan.total_cost_usd == pytest.approx(270.00)
        assert [stop.gallons for stop in plan.stops] == pytest.approx([30.0, 50.0])

    def test_rising_prices_buy_the_biggest_legal_amount_at_the_cheapest_pump(self):
        # 600 miles needs 60 gallons. The $3.00 pump can supply at most a
        # tankful, so 50 * 3.00 + 10 * 4.00 = 190.00.
        candidates = [Opportunity(0.0, 3.00), Opportunity(100.0, 4.00)]
        plan = plan_purchases(candidates, 600.0, RANGE, MPG)
        assert plan.total_cost_usd == pytest.approx(190.00)
        assert [stop.gallons for stop in plan.stops] == pytest.approx([50.0, 10.0])

    def test_falling_prices_buy_hand_to_mouth(self):
        # Each pump is cheaper than the last, so buy only enough to reach the
        # next one: 10 gal @ 4.00 + 10 @ 3.00 + 10 @ 2.00 = 90.00 for 300 miles.
        candidates = [
            Opportunity(0.0, 4.00),
            Opportunity(100.0, 3.00),
            Opportunity(200.0, 2.00),
        ]
        plan = plan_purchases(candidates, 300.0, RANGE, MPG)
        assert plan.total_cost_usd == pytest.approx(90.00)
        assert [stop.gallons for stop in plan.stops] == pytest.approx([10.0, 10.0, 10.0])

    def test_a_candidate_dearer_than_the_tank_already_holds_is_not_a_stop(self):
        # The $3.00 origin fill covers the whole trip, so the $9.00 pump at
        # mile 100 is passed by and never becomes a Stop.
        candidates = [Opportunity(0.0, 3.00), Opportunity(100.0, 9.00)]
        plan = plan_purchases(candidates, 400.0, RANGE, MPG)
        assert len(plan.stops) == 1
        assert plan.total_cost_usd == pytest.approx(120.00)

    def test_the_tank_never_goes_negative_or_over_capacity(self):
        candidates = [
            Opportunity(0.0, 3.10),
            Opportunity(220.0, 2.60),
            Opportunity(480.0, 3.40),
            Opportunity(700.0, 2.90),
            Opportunity(1150.0, 3.80),
        ]
        plan = plan_purchases(candidates, 1400.0, RANGE, MPG)
        assert plan.feasible is True
        tank_capacity = RANGE / MPG
        for stop in plan.stops:
            assert 0.0 <= stop.gallons_remaining_after <= tank_capacity + 1e-9
            assert stop.gallons > 0.0

    def test_total_gallons_is_the_fuel_the_route_burns(self):
        candidates = [Opportunity(0.0, 3.10), Opportunity(220.0, 2.60), Opportunity(480.0, 3.40)]
        plan = plan_purchases(candidates, 900.0, RANGE, MPG)
        assert plan.total_gallons == pytest.approx(90.0)
        assert plan.average_price_per_gallon == pytest.approx(
            plan.total_cost_usd / plan.total_gallons
        )

    def test_an_infeasible_route_reports_the_stretch_and_no_cost(self):
        plan = plan_purchases([Opportunity(0.0, 3.0)], 1200.0, RANGE, MPG)
        assert plan.feasible is False
        assert plan.infeasible_stretch is not None
        assert plan.stops == ()
        assert plan.total_cost_usd is None
        assert plan.naive_cost_usd is None
        # The gallons the route would burn are still knowable.
        assert plan.total_gallons == pytest.approx(120.0)


class TestNaiveDriver:
    def test_ignores_price_and_runs_the_tank_down(self):
        # The Naive Driver fills at mile 0, drives 480 miles to the furthest
        # Candidate in Range and tops up there at $5.00: 50 * 3.00 + 40 * 5.00.
        candidates = [Opportunity(0.0, 3.00), Opportunity(100.0, 2.00), Opportunity(480.0, 5.00)]
        assert naive_cost(candidates, 900.0, RANGE, MPG) == pytest.approx(350.00)

    def test_buys_the_same_total_gallons_as_the_plan(self):
        candidates = [Opportunity(0.0, 3.00), Opportunity(100.0, 2.00), Opportunity(480.0, 5.00)]
        plan = plan_purchases(candidates, 900.0, RANGE, MPG)
        # 90 gallons either way; only where they are bought differs.
        assert plan.total_gallons == pytest.approx(90.0)
        assert plan.naive_cost_usd == pytest.approx(350.00)
        assert plan.total_cost_usd == pytest.approx(280.00)
        assert plan.savings_usd == pytest.approx(70.00)
        assert plan.savings_percent == pytest.approx(20.0)

    def test_obeys_range_and_is_never_cheaper_than_the_plan(self):
        layouts = [
            ([(0.0, 3.0)], 400.0),
            ([(0.0, 4.0), (300.0, 3.0)], 800.0),
            ([(0.0, 2.5), (120.0, 3.9), (400.0, 2.1), (860.0, 3.3)], 1300.0),
            ([(0.0, 3.0), (500.0, 3.0), (1000.0, 3.0)], 1400.0),
            ([(0.0, 3.0), (100.0, 3.0), (200.0, 2.0), (650.0, 4.0)], 1100.0),
        ]
        for prices, distance in layouts:
            candidates = [Opportunity(*p) for p in prices]
            plan = plan_purchases(candidates, distance, RANGE, MPG)
            assert plan.feasible is True
            assert plan.naive_cost_usd >= plan.total_cost_usd - 1e-9

    def test_a_zero_distance_route_costs_nothing(self):
        assert naive_cost([Opportunity(0.0, 3.0)], 0.0, RANGE, MPG) == 0.0
