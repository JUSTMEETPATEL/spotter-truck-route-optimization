"""Build time: the supplied price file -> the committed geocoded Station CSV.

    uv run manage.py build_station_data

Cleans the supplied Price Observations into Stations, resolves each one's city
against the committed Gazetteer, applies the hand corrections in
``data/city_overrides.csv``, and writes two committed files:

* ``data/stations_geocoded.csv`` -- what ``load_stations`` reads.
* ``data/build_report.json`` -- the coverage figures ``/api/v1/health/``
  publishes, so the count of excluded Stations is never hidden.

This command may read the network only through ``build_gazetteer``; by itself
it reads committed files and writes committed files.
"""

import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.stations.cleaning import Station, clean_price_observations
from apps.stations.geocoding import Gazetteer

STATION_FIELDS = (
    "opis_id",
    "name",
    "address",
    "city",
    "state",
    "latitude",
    "longitude",
    "price_mean",
    "price_min",
    "price_max",
    "price_sample_count",
)


class Command(BaseCommand):
    help = "Clean, dedupe and geocode the supplied price file into a committed CSV."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--prices-csv", type=Path, default=settings.SUPPLIED_PRICES_CSV)
        parser.add_argument("--gazetteer-csv", type=Path, default=settings.GAZETTEER_CSV)
        parser.add_argument("--overrides-csv", type=Path, default=settings.CITY_OVERRIDES_CSV)
        parser.add_argument("--output", type=Path, default=settings.STATIONS_GEOCODED_CSV)
        parser.add_argument("--report", type=Path, default=settings.BUILD_REPORT_JSON)
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Fail if coverage is below GAZETTEER_COVERAGE_FLOOR.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        gazetteer_path = Path(options["gazetteer_csv"])
        if not gazetteer_path.exists():
            raise CommandError(
                f"No Gazetteer at {gazetteer_path}. Run `manage.py build_gazetteer` first."
            )
        gazetteer = Gazetteer.from_csv(gazetteer_path)
        overrides = _read_overrides(Path(options["overrides_csv"]))

        with Path(options["prices_csv"]).open(newline="", encoding="utf-8-sig") as handle:
            cleaning = clean_price_observations(csv.DictReader(handle))

        self.stdout.write(
            f"{cleaning.observations_read:,} Price Observations -> "
            f"{len(cleaning.stations):,} Stations "
            f"({cleaning.non_us_observations_dropped:,} non-US, "
            f"{cleaning.unusable_observations_dropped:,} unusable)"
        )

        geocoded: list[tuple[Station, float, float]] = []
        excluded: Counter[tuple[str, str]] = Counter()
        per_state: dict[str, Counter[str]] = {}
        override_hits = 0

        for station in cleaning.stations:
            counts = per_state.setdefault(station.state, Counter())
            counts["stations"] += 1
            point = overrides.get((station.city.strip().upper(), station.state))
            if point is not None:
                override_hits += 1
            else:
                place = gazetteer.resolve(station.city, station.state)
                point = (place.point.lat, place.point.lon) if place else None
            if point is None:
                excluded[(station.city, station.state)] += 1
                continue
            counts["geocoded"] += 1
            geocoded.append((station, point[0], point[1]))

        coverage = len(geocoded) / len(cleaning.stations) if cleaning.stations else 0.0
        _write_stations(Path(options["output"]), geocoded)
        coverage_by_state = {
            state: {
                "stations": counts["stations"],
                "geocoded": counts["geocoded"],
                "coverage": round(counts["geocoded"] / counts["stations"], 4),
            }
            for state, counts in sorted(per_state.items())
        }
        report: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "gazetteer_entries": len(gazetteer),
            "observations_read": cleaning.observations_read,
            "non_us_observations_dropped": cleaning.non_us_observations_dropped,
            "unusable_observations_dropped": cleaning.unusable_observations_dropped,
            "stations_total": len(cleaning.stations),
            "stations_geocoded": len(geocoded),
            "stations_excluded": sum(excluded.values()),
            "stations_from_overrides": override_hits,
            "gazetteer_coverage": round(coverage, 6),
            "coverage_by_state": coverage_by_state,
            "excluded_cities": [
                {"city": city, "state": state, "stations": count}
                for (city, state), count in sorted(excluded.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        }
        Path(options["report"]).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

        self.stdout.write(
            f"Geocoded {len(geocoded):,}/{len(cleaning.stations):,} Stations "
            f"({coverage:.2%}), {override_hits:,} from hand corrections"
        )
        worst = sorted(
            coverage_by_state.items(),
            key=lambda item: (item[1]["coverage"], -item[1]["stations"]),
        )[:8]
        for state, tally in worst:
            self.stdout.write(
                f"  {state}: {tally['geocoded']}/{tally['stations']} ({tally['coverage']:.1%})"
            )

        floor = settings.GAZETTEER_COVERAGE_FLOOR
        if coverage < floor:
            message = f"Coverage {coverage:.2%} is below the {floor:.0%} floor."
            if options["strict"]:
                raise CommandError(message)
            self.stdout.write(self.style.WARNING(message))
        self.stdout.write(self.style.SUCCESS(f"Wrote {options['output']} and {options['report']}"))


def _read_overrides(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    """Hand corrections for cities the Gazetteer cannot resolve."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            (row["city"].strip().upper(), row["state"].strip().upper()): (
                float(row["latitude"]),
                float(row["longitude"]),
            )
            for row in csv.DictReader(handle)
            if row.get("city") and not row["city"].startswith("#")
        }


def _write_stations(path: Path, geocoded: list[tuple[Station, float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(STATION_FIELDS)
        for station, latitude, longitude in geocoded:
            writer.writerow(
                [
                    station.opis_id,
                    station.name,
                    station.address,
                    station.city,
                    station.state,
                    f"{latitude:.6f}",
                    f"{longitude:.6f}",
                    f"{station.price_mean:.6f}",
                    f"{station.price_min:.6f}",
                    f"{station.price_max:.6f}",
                    station.price_sample_count,
                ]
            )
