"""Offline sweep of the Corridor thinning spacing.

    uv run python benchmarks/corridor_spacing.py

Runs against the committed Dallas->Chicago geometry fixture, so it costs zero
external calls and a reviewer with no network can reproduce it. Thinning
spacing is a pure speed knob: survivors of the coarse pass are re-measured
against the original geometry regardless, so the matched set must not change
as the spacing does -- and the sweep asserts exactly that.
"""

import json
import os
import pathlib
import statistics
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402  -- Django has to be configured before the apps import

django.setup()

from apps.routing.corridor import match_corridor  # noqa: E402
from apps.routing.geo import Point  # noqa: E402
from apps.stations.registry import candidates  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "dallas_chicago_route.json"
SPACINGS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0)
CELL_DEGREES = 0.5
CORRIDOR_MILES = 10.0
REPEATS = 3


def load_geometry() -> list[Point]:
    payload = json.loads(FIXTURE.read_text())
    return [Point(lat, lon) for lon, lat in payload["geometry"]["coordinates"]]


def main() -> int:
    geometry = load_geometry()
    all_candidates = candidates()
    if not all_candidates:
        print("No Candidates loaded. Run `manage.py load_stations` first.")
        return 1

    print(
        f"Dallas->Chicago fixture: {len(geometry):,} shape points, "
        f"{len(all_candidates):,} Candidates, {CORRIDOR_MILES:g}-mile Corridor"
    )
    print(f"{'spacing (mi)':>12} {'median ms':>10} {'matched':>8} {'agrees':>7}")

    baseline: list[str] | None = None
    exit_code = 0
    for spacing in SPACINGS:
        timings = []
        matched = []
        for _ in range(REPEATS):
            started = time.perf_counter()
            matched = match_corridor(
                geometry, all_candidates, CORRIDOR_MILES, spacing, CELL_DEGREES
            )
            timings.append((time.perf_counter() - started) * 1000)
        identities = [entry.candidate.opis_id for entry in matched]
        baseline = baseline if baseline is not None else identities
        agrees = identities == baseline
        exit_code = exit_code or (0 if agrees else 1)
        print(
            f"{spacing:>12g} {statistics.median(timings):>10.1f} "
            f"{len(matched):>8} {'yes' if agrees else 'NO':>7}"
        )

    if exit_code:
        print("\nA spacing changed the matched set. That is a correctness bug, not a trade-off.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
