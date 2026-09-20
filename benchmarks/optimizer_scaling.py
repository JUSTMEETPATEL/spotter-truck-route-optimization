"""Where the indexed optimizer overtakes the scanning one.

    uv run python benchmarks/optimizer_scaling.py

Offline and dependency-free: it generates Candidate layouts rather than
touching the database, the network or the Gazetteer.

The point is honest about scale. On a real route the Corridor holds 86-142
Candidates and both optimizers finish in well under a millisecond, so the
indexed one buys nothing a caller can feel -- 93% of a request is the routing
call. It wins when the Candidate list grows: the scanning optimizer answers
"cheaper Candidate ahead?" and "cheapest within Range?" by walking the
reachable window, which widens as Candidates get denser, while the indexed one
answers both in constant time after an O(n log n) build.
"""

import os
import pathlib
import random
import statistics
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from apps.routing import optimizer as scanning  # noqa: E402
from apps.routing import optimizer_v2 as indexed  # noqa: E402

RANGE = 500.0
MPG = 10.0
#: Candidate counts, from a real Corridor up to a hypothetical national one.
SIZES = (100, 400, 1_600, 6_400, 25_600)
REPEATS = 3
#: Price profiles decide the winner, so all three are measured. "rising" is
#: the scanning optimizer's worst case: no Candidate ahead is ever cheaper, so
#: every step scans the whole reachable window.
PROFILES = ("random", "rising", "falling")


class Opportunity:
    """A Candidate as the optimizer sees it."""

    __slots__ = ("mile_marker", "price_per_gallon")

    def __init__(self, mile_marker: float, price_per_gallon: float):
        self.mile_marker = mile_marker
        self.price_per_gallon = price_per_gallon


def layout(count: int, profile: str, seed: int = 4242) -> tuple[list[Opportunity], float]:
    """``count`` Candidates spread so that Range always covers several."""
    rng = random.Random(seed)
    spacing = 8.0
    if profile == "random":
        prices = [round(rng.uniform(2.1, 4.9), 4) for _ in range(count)]
    elif profile == "rising":
        prices = [round(2.0 + i * 0.001, 4) for i in range(count)]
    else:
        prices = [round(2.0 + (count - i) * 0.001, 4) for i in range(count)]
    candidates = [Opportunity(round(i * spacing, 3), prices[i]) for i in range(count)]
    return candidates, candidates[-1].mile_marker + 120.0


def median_ms(plan, candidates, distance) -> float:
    timings = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        plan(candidates, distance, RANGE, MPG)
        timings.append((time.perf_counter() - started) * 1000)
    return statistics.median(timings)


def main() -> int:
    print(f"Range {RANGE:g} mi at {MPG:g} mpg, Candidates every 8 miles")
    print(
        f"{'prices':<9}{'candidates':>11} {'scan (ms)':>11} {'indexed (ms)':>13} "
        f"{'speed-up':>9}  same answer"
    )
    for profile in PROFILES:
        for size in SIZES:
            candidates, distance = layout(size, profile)
            scan_ms = median_ms(scanning.plan_purchases, candidates, distance)
            indexed_ms = median_ms(indexed.plan_purchases, candidates, distance)
            first = scanning.plan_purchases(candidates, distance, RANGE, MPG)
            second = indexed.plan_purchases(candidates, distance, RANGE, MPG)
            agree = (
                first.stops_count == second.stops_count
                and abs(first.total_cost_usd - second.total_cost_usd) < 1e-9
            )
            print(
                f"{profile:<9}{size:>11,} {scan_ms:>11.2f} {indexed_ms:>13.2f} "
                f"{scan_ms / indexed_ms:>8.1f}x  {'yes' if agree else 'NO'}"
            )
            if not agree:
                return 1
        print()
    print("A real Corridor holds 86-142 Candidates, where both finish in well under a")
    print("millisecond and 93% of a request is the routing call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
