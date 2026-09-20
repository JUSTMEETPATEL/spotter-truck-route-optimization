"""The same refuelling plan, computed with precomputed indexes.

:mod:`apps.routing.optimizer` answers the greedy rule's two questions by
scanning the Candidates within Range at every step:

* *is there a cheaper Candidate ahead within Range?*  -- a left-to-right scan;
* *which reachable Candidate is cheapest?*            -- a minimum over a slice.

Both are linear in the number of reachable Candidates, so a plan costs O(n·k)
with k the size of the reachable window, and O(n²) when Range covers the whole
route. This module answers the same two questions from structures built once:

* a **monotonic stack** gives, for every Candidate, the nearest strictly
  cheaper Candidate ahead of it, for all Candidates in O(n) total;
* a **sparse table** answers "cheapest Candidate in this index range" in O(1)
  after an O(n log n) build.

A plan then costs O(n log n) to prepare and O(1) per Stop chosen.

The answer is identical, by construction and by test: the nearest strictly
cheaper Candidate ahead is the nearest one within Range whenever it is
reachable at all, and the sparse table reduces with the same comparator the
scan uses -- cheapest first, ties broken by the furthest along.

Imports nothing from Django. Feasibility and the Naive Driver are shared with
the scanning optimizer rather than reimplemented, because they are not what
this module changes.
"""

from bisect import bisect_right
from collections.abc import Sequence

from apps.routing.optimizer import (
    _GALLON_EPSILON,
    FuelPlan,
    Purchasable,
    Stop,
    find_infeasible_stretch,
    naive_cost,
)


def next_cheaper_indexes(candidates: Sequence[Purchasable]) -> list[int | None]:
    """For each Candidate, the nearest strictly cheaper one ahead of it.

    One right-to-left pass with a monotonic stack: each index is pushed once
    and popped at most once, so the whole table costs O(n).
    """
    nearest: list[int | None] = [None] * len(candidates)
    stack: list[int] = []
    for index in reversed(range(len(candidates))):
        price = candidates[index].price_per_gallon
        # Anything at or above this price can never be the nearest cheaper
        # Candidate for something further left: this one is nearer and no
        # dearer.
        while stack and candidates[stack[-1]].price_per_gallon >= price:
            stack.pop()
        nearest[index] = stack[-1] if stack else None
        stack.append(index)
    return nearest


class CheapestInRange:
    """A sparse table answering "cheapest Candidate in [left, right]" in O(1).

    Ties go to the Candidate furthest along the route, which is the tie-break
    the scanning optimizer uses: equal prices cost the same, so the one that
    makes more progress is weakly better.
    """

    def __init__(self, candidates: Sequence[Purchasable]) -> None:
        self._candidates = candidates
        size = len(candidates)
        self._log = [0] * (size + 1)
        for index in range(2, size + 1):
            self._log[index] = self._log[index // 2] + 1
        levels = self._log[size] + 1 if size else 1
        self._table: list[list[int]] = [list(range(size))]
        for level in range(1, levels):
            span = 1 << level
            previous = self._table[level - 1]
            half = span >> 1
            self._table.append(
                [
                    self._better(previous[start], previous[start + half])
                    for start in range(size - span + 1)
                ]
            )

    def _better(self, left: int, right: int) -> int:
        left_key = (self._candidates[left].price_per_gallon, -self._candidates[left].mile_marker)
        right_key = (self._candidates[right].price_per_gallon, -self._candidates[right].mile_marker)
        return left if left_key <= right_key else right

    def query(self, left: int, right: int) -> int:
        """The index of the cheapest Candidate in the inclusive range."""
        if left > right:
            raise ValueError("empty range")
        level = self._log[right - left + 1]
        row = self._table[level]
        return self._better(row[left], row[right - (1 << level) + 1])


def plan_purchases(
    candidates: Sequence[Purchasable],
    total_distance_miles: float,
    max_range_miles: float,
    mpg: float,
) -> FuelPlan:
    """The cheapest legal sequence of purchases, using precomputed indexes.

    Contract-identical to :func:`apps.routing.optimizer.plan_purchases`.
    """
    gallons_burned = total_distance_miles / mpg
    stretch = find_infeasible_stretch(candidates, total_distance_miles, max_range_miles)
    if stretch is not None:
        return FuelPlan(
            feasible=False,
            infeasible_stretch=stretch,
            stops=(),
            total_gallons=gallons_burned,
            total_cost_usd=None,
            naive_cost_usd=None,
        )

    tank_capacity = max_range_miles / mpg
    stops: list[Stop] = []
    index = 0
    gallons_in_tank = 0.0

    if candidates:
        miles = [candidate.mile_marker for candidate in candidates]
        nearest_cheaper = next_cheaper_indexes(candidates)
        # Built on first use. A route whose prices fall as it goes never takes
        # the fill-the-tank branch, so it never pays for the table.
        cheapest: CheapestInRange | None = None

        for _ in range(len(candidates)):
            current = candidates[index]
            remaining_miles = total_distance_miles - current.mile_marker
            reach_limit = current.mile_marker + max_range_miles

            cheaper = nearest_cheaper[index]
            if cheaper is not None and miles[cheaper] <= reach_limit:
                # The nearest cheaper Candidate anywhere ahead is also the
                # nearest cheaper one within Range, since nothing between them
                # is cheaper.
                next_index = cheaper
                gallons_needed = (miles[next_index] - current.mile_marker) / mpg - gallons_in_tank
            elif remaining_miles <= max_range_miles:
                gallons_needed = remaining_miles / mpg - gallons_in_tank
                _record(stops, current, gallons_needed, gallons_in_tank)
                break
            else:
                if cheapest is None:
                    cheapest = CheapestInRange(candidates)
                furthest = bisect_right(miles, reach_limit) - 1
                next_index = cheapest.query(index + 1, furthest)
                gallons_needed = tank_capacity - gallons_in_tank

            gallons_in_tank = _record(stops, current, gallons_needed, gallons_in_tank)
            gallons_in_tank -= (miles[next_index] - current.mile_marker) / mpg
            index = next_index

    return FuelPlan(
        feasible=True,
        infeasible_stretch=None,
        stops=tuple(stops),
        total_gallons=gallons_burned,
        total_cost_usd=sum(stop.cost_usd for stop in stops),
        naive_cost_usd=naive_cost(candidates, total_distance_miles, max_range_miles, mpg),
    )


def _record(
    stops: list[Stop], candidate: Purchasable, gallons: float, gallons_in_tank: float
) -> float:
    if gallons <= _GALLON_EPSILON:
        return gallons_in_tank
    remaining_after = gallons_in_tank + gallons
    stops.append(
        Stop(
            sequence=len(stops) + 1,
            candidate=candidate,
            gallons=gallons,
            cost_usd=gallons * candidate.price_per_gallon,
            gallons_remaining_after=remaining_after,
        )
    )
    return remaining_after
