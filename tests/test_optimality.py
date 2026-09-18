"""The greedy plan's total cost, checked against a linear program.

Testing a greedy against another greedy would prove nothing: if the reasoning
behind the rule is wrong, both are wrong identically. So the problem is
restated here as a linear program -- minimise the sum of price times gallons,
subject to the tank level after every leg staying within [0, Tank Capacity] and
starting empty -- and solved with scipy.

Only total cost is compared, at relative tolerance 1e-6: ties in price admit
many optimal purchase vectors, so asserting on gallons would flake against
scipy's choice of pivot.
"""

import random
from typing import NamedTuple

import numpy as np
import pytest
from scipy.optimize import linprog

from apps.routing.optimizer import find_infeasible_stretch, plan_purchases

RANGE = 500.0
MPG = 10.0
TANK = RANGE / MPG


class Opportunity(NamedTuple):
    mile_marker: float
    price_per_gallon: float


def lp_minimum_cost(candidates: list[Opportunity], total_distance_miles: float) -> float | None:
    """Minimum fuel cost for this Candidate list, or None if infeasible."""
    n = len(candidates)
    burn = [c.mile_marker / MPG for c in candidates]
    rows: list[list[float]] = []
    limits: list[float] = []

    for i in range(n):
        # The tank cannot be negative on arrival at Candidate i.
        rows.append([-1.0 if k < i else 0.0 for k in range(n)])
        limits.append(-burn[i])
        # Nor over capacity once the purchase at Candidate i is made.
        rows.append([1.0 if k <= i else 0.0 for k in range(n)])
        limits.append(TANK + burn[i])

    # And it cannot be negative on arrival at the destination.
    rows.append([-1.0] * n)
    limits.append(-total_distance_miles / MPG)

    result = linprog(
        c=np.array([c.price_per_gallon for c in candidates]),
        A_ub=np.array(rows),
        b_ub=np.array(limits),
        bounds=[(0.0, None)] * n,
        method="highs",
    )
    return float(result.fun) if result.success else None


def adversarial_layouts() -> list[tuple[str, list[Opportunity], float]]:
    """Layouts that force every branch of the greedy rule.

    Uniform-random prices almost never force a full fill, so the interesting
    branch would go untested. These are built to force it.
    """
    rng = random.Random(20260918)
    layouts: list[tuple[str, list[Opportunity], float]] = []

    def spaced(count: int, gap: float) -> list[float]:
        return [round(i * gap, 3) for i in range(count)]

    for trial in range(14):
        count = 3 + trial % 6
        gap = 80.0 + 60.0 * (trial % 4)
        miles = spaced(count, gap)
        distance = miles[-1] + 120.0 + 40.0 * (trial % 3)

        rising = [3.0 + 0.25 * i for i in range(count)]
        layouts.append(
            ("rising", [Opportunity(m, p) for m, p in zip(miles, rising, strict=True)], distance)
        )

        falling = [3.0 + 0.25 * (count - i) for i in range(count)]
        layouts.append(
            ("falling", [Opportunity(m, p) for m, p in zip(miles, falling, strict=True)], distance)
        )

        flat = [3.117] * count
        layouts.append(
            ("identical", [Opportunity(m, p) for m, p in zip(miles, flat, strict=True)], distance)
        )

        jagged = [round(rng.uniform(2.1, 4.9), 4) for _ in range(count)]
        layouts.append(
            ("jagged", [Opportunity(m, p) for m, p in zip(miles, jagged, strict=True)], distance)
        )

        # A gap of exactly Range, which must stay feasible, and one pump
        # either side of it priced to force a full fill.
        layouts.append(
            (
                "gap-exactly-range",
                [Opportunity(0.0, 4.50), Opportunity(RANGE, 2.00), Opportunity(RANGE + 90.0, 4.75)],
                RANGE + 90.0 + 200.0 + 10.0 * trial,
            )
        )

        # One mile under Range: the tightest feasible spacing.
        layouts.append(
            (
                "gap-range-minus-one",
                [
                    Opportunity(0.0, 2.60),
                    Opportunity(RANGE - 1.0, 4.80),
                    Opportunity(2 * RANGE - 2.0, 2.40),
                ],
                2 * RANGE - 2.0 + 180.0 + 5.0 * trial,
            )
        )

    layouts.append(("single-candidate", [Opportunity(0.0, 3.399)], 410.0))
    layouts.append(("single-candidate-zero-distance", [Opportunity(0.0, 3.399)], 0.0))
    return layouts


LAYOUTS = adversarial_layouts()


def test_the_layout_set_is_adversarial_and_large_enough():
    assert len(LAYOUTS) >= 80
    kinds = {name for name, _, _ in LAYOUTS}
    assert kinds == {
        "rising",
        "falling",
        "identical",
        "jagged",
        "gap-exactly-range",
        "gap-range-minus-one",
        "single-candidate",
        "single-candidate-zero-distance",
    }


@pytest.mark.parametrize(
    ("name", "candidates", "distance"),
    [pytest.param(*layout, id=f"{layout[0]}-{i}") for i, layout in enumerate(LAYOUTS)],
)
def test_greedy_total_cost_matches_the_linear_program(
    name: str, candidates: list[Opportunity], distance: float
):
    assert find_infeasible_stretch(candidates, distance, RANGE) is None, "layout must be feasible"
    plan = plan_purchases(candidates, distance, RANGE, MPG)
    assert plan.total_cost_usd == pytest.approx(lp_minimum_cost(candidates, distance), rel=1e-6)


def test_a_gap_one_mile_over_range_is_infeasible_for_both_formulations():
    candidates = [Opportunity(0.0, 3.0), Opportunity(RANGE + 1.0, 3.0)]
    distance = RANGE + 1.0 + 100.0
    assert plan_purchases(candidates, distance, RANGE, MPG).feasible is False
    assert lp_minimum_cost(candidates, distance) is None
