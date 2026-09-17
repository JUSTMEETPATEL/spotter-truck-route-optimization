"""Minimum-cost refuelling over an ordered list of Candidates.

Imports nothing from Django: plain data in, plain data out. Callers hand in
anything exposing a Mile Marker and a price, which in production is a
:class:`~apps.routing.corridor.MatchedCandidate` and in tests is a two-field
tuple.

The greedy rule, and the exchange argument that makes it optimal, are set out
under "The optimizer" in the README.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class Purchasable(Protocol):
    """A fuel-buying opportunity as the optimizer sees it."""

    @property
    def mile_marker(self) -> float: ...

    @property
    def price_per_gallon(self) -> float: ...


@dataclass(frozen=True)
class InfeasibleStretch:
    """The first stretch of the route no truck with this Range can cross.

    ``from_index`` and ``to_index`` index the Candidate list; ``None`` means the
    start and the destination respectively. Labels are the serializer's job.
    """

    from_index: int | None
    to_index: int | None
    from_mile: float
    to_mile: float

    @property
    def gap_miles(self) -> float:
        return self.to_mile - self.from_mile


def find_infeasible_stretch(
    candidates: Sequence[Purchasable],
    total_distance_miles: float,
    max_range_miles: float,
) -> InfeasibleStretch | None:
    """Return the first gap in ``[start, candidates…, destination]`` over Range.

    The final leg from the last Candidate to the destination is part of the
    sequence, so an empty Candidate list on a route that needs fuel is
    infeasible rather than free.

    The first gap is special: the truck departs empty, so it cannot cover any
    distance at all before its first purchase. In production the Origin Fill
    sits at Mile Marker 0 and the gap is zero; when no Station lies within
    ``ORIGIN_FILL_RADIUS_MILES`` there is no Candidate there and the route is
    genuinely infeasible.
    """
    previous_mile = 0.0
    previous_index: int | None = None
    for index, candidate in enumerate(candidates):
        reach = 0.0 if previous_index is None else max_range_miles
        if candidate.mile_marker - previous_mile > reach:
            return InfeasibleStretch(
                from_index=previous_index,
                to_index=index,
                from_mile=previous_mile,
                to_mile=candidate.mile_marker,
            )
        previous_mile = candidate.mile_marker
        previous_index = index

    reach = 0.0 if previous_index is None else max_range_miles
    if total_distance_miles - previous_mile > reach:
        return InfeasibleStretch(
            from_index=previous_index,
            to_index=None,
            from_mile=previous_mile,
            to_mile=total_distance_miles,
        )
    return None


#: Purchases below this many gallons are rounding noise, not a Stop.
_GALLON_EPSILON = 1e-9


@dataclass(frozen=True)
class Stop:
    """A Candidate the plan actually buys at."""

    sequence: int
    candidate: Purchasable
    gallons: float
    cost_usd: float
    gallons_remaining_after: float


@dataclass(frozen=True)
class FuelPlan:
    """The purchases a plan makes, or the reason it cannot make any."""

    feasible: bool
    infeasible_stretch: InfeasibleStretch | None
    stops: tuple[Stop, ...]
    total_gallons: float
    total_cost_usd: float | None
    naive_cost_usd: float | None

    @property
    def stops_count(self) -> int:
        return len(self.stops)

    @property
    def average_price_per_gallon(self) -> float | None:
        if self.total_cost_usd is None or self.total_gallons <= 0.0:
            return None
        return self.total_cost_usd / self.total_gallons

    @property
    def savings_usd(self) -> float | None:
        if self.total_cost_usd is None or self.naive_cost_usd is None:
            return None
        return self.naive_cost_usd - self.total_cost_usd

    @property
    def savings_percent(self) -> float | None:
        if self.savings_usd is None or not self.naive_cost_usd:
            return None
        return 100.0 * self.savings_usd / self.naive_cost_usd


def plan_purchases(
    candidates: Sequence[Purchasable],
    total_distance_miles: float,
    max_range_miles: float,
    mpg: float,
) -> FuelPlan:
    """Choose the cheapest legal sequence of purchases along the route.

    ``candidates`` must be ordered by Mile Marker, with the Origin Fill at
    Mile Marker 0. The truck departs empty and pays for every gallon it burns,
    so ``total_gallons`` is always ``total_distance_miles / mpg``.

    The rule, applied at each Candidate the plan visits:

    * a strictly cheaper Candidate within Range -> buy just enough to reach the
      nearest one, and drive there;
    * else destination within Range -> buy just enough to finish, and stop;
    * else -> fill the tank and drive to the cheapest Candidate in Range.

    "Just enough" is always net of the fuel already in the tank.

    The cheaper-Candidate test comes first on purpose. Checking the destination
    first would buy the whole remaining trip at this price whenever the finish
    is within Range, even with a cheaper pump on the way, and that is not
    optimal: three pumps at $4.00, $3.00 and $2.00 spread over a 300-mile trip
    cost $90 hand-to-mouth and $120 bought outright at the origin.
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

    # At most one decision per Candidate: every branch below either finishes
    # the trip or moves strictly forward, so this cannot spin.
    for _ in range(len(candidates)):
        current = candidates[index]
        remaining_miles = total_distance_miles - current.mile_marker
        reachable = _reachable_from(candidates, index, max_range_miles)

        cheaper = _first_strictly_cheaper(candidates, reachable, current.price_per_gallon)
        if cheaper is not None:
            next_index = cheaper
            gallons_needed = (
                candidates[next_index].mile_marker - current.mile_marker
            ) / mpg - gallons_in_tank
        elif remaining_miles <= max_range_miles:
            gallons_needed = remaining_miles / mpg - gallons_in_tank
            _record(stops, current, gallons_needed, gallons_in_tank)
            break
        else:
            next_index = _cheapest(candidates, reachable)
            gallons_needed = tank_capacity - gallons_in_tank

        gallons_in_tank = _record(stops, current, gallons_needed, gallons_in_tank)
        gallons_in_tank -= (candidates[next_index].mile_marker - current.mile_marker) / mpg
        index = next_index

    return FuelPlan(
        feasible=True,
        infeasible_stretch=None,
        stops=tuple(stops),
        total_gallons=gallons_burned,
        total_cost_usd=sum(stop.cost_usd for stop in stops),
        naive_cost_usd=naive_cost(candidates, total_distance_miles, max_range_miles, mpg),
    )


def naive_cost(
    candidates: Sequence[Purchasable],
    total_distance_miles: float,
    max_range_miles: float,
    mpg: float,
) -> float:
    """What the Naive Driver pays over the same route under the same Range.

    The Naive Driver runs the tank down and fills completely at whatever
    Candidate is nearest when it empties -- the furthest one still in Range --
    obeying Range but ignoring price. Only the final purchase is trimmed to
    what finishing the route needs, so the Naive Driver buys exactly the same
    total gallons as the plan and the two differ only in *where* fuel was
    bought. Assumes the route is feasible.
    """
    tank_capacity = max_range_miles / mpg
    cost = 0.0
    gallons_in_tank = 0.0
    index = 0

    for _ in range(len(candidates)):
        current = candidates[index]
        remaining_miles = total_distance_miles - current.mile_marker
        if remaining_miles <= max_range_miles:
            gallons = remaining_miles / mpg - gallons_in_tank
            if gallons > _GALLON_EPSILON:
                cost += gallons * current.price_per_gallon
            break

        gallons = tank_capacity - gallons_in_tank
        cost += gallons * current.price_per_gallon
        gallons_in_tank = tank_capacity
        furthest = _reachable_from(candidates, index, max_range_miles)[-1]
        gallons_in_tank -= (candidates[furthest].mile_marker - current.mile_marker) / mpg
        index = furthest

    return cost


def _record(
    stops: list[Stop],
    candidate: Purchasable,
    gallons: float,
    gallons_in_tank: float,
) -> float:
    """Append a Stop if the purchase is real, and return the new tank level."""
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


def _reachable_from(candidates: Sequence[Purchasable], index: int, max_range_miles: float) -> range:
    """Indices of the Candidates ahead of ``index`` and within Range of it."""
    limit = candidates[index].mile_marker + max_range_miles
    end = index + 1
    while end < len(candidates) and candidates[end].mile_marker <= limit:
        end += 1
    return range(index + 1, end)


def _first_strictly_cheaper(
    candidates: Sequence[Purchasable], reachable: range, price: float
) -> int | None:
    for index in reachable:
        if candidates[index].price_per_gallon < price:
            return index
    return None


def _cheapest(candidates: Sequence[Purchasable], reachable: range) -> int:
    """The cheapest reachable Candidate, ties broken by the furthest along.

    Tied prices cost the same either way, so taking the furthest makes more
    progress for the same money.
    """
    return min(
        reachable,
        key=lambda i: (candidates[i].price_per_gallon, -candidates[i].mile_marker),
    )
