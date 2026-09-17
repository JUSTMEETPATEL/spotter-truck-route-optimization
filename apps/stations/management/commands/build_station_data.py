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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.stations.cleaning import clean_price_observations
from apps.stations.geocoding import (
    Gazetteer,
    GeocodingResult,
    geocode_stations,
    read_city_overrides,
)

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
        overrides = read_city_overrides(Path(options["overrides_csv"]))

        with Path(options["prices_csv"]).open(newline="", encoding="utf-8-sig") as handle:
            cleaning = clean_price_observations(csv.DictReader(handle))

        self.stdout.write(
            f"{cleaning.observations_read:,} Price Observations -> "
            f"{len(cleaning.stations):,} Stations "
            f"({cleaning.non_us_observations_dropped:,} non-US, "
            f"{cleaning.unusable_observations_dropped:,} unusable)"
        )

        placement = geocode_stations(cleaning.stations, gazetteer, overrides)
        _write_stations(Path(options["output"]), placement)
        report = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "gazetteer_entries": len(gazetteer),
            "observations_read": cleaning.observations_read,
            "non_us_observations_dropped": cleaning.non_us_observations_dropped,
            "unusable_observations_dropped": cleaning.unusable_observations_dropped,
            "stations_total": placement.stations_total,
            "stations_geocoded": len(placement.placed),
            "stations_excluded": sum(placement.excluded.values()),
            "stations_out_of_us_bounds": sum(placement.out_of_bounds.values()),
            "stations_from_overrides": placement.from_overrides,
            "gazetteer_coverage": round(placement.coverage, 6),
            "coverage_by_state": placement.coverage_by_state(),
            "excluded_cities": placement.excluded_cities(),
        }
        Path(options["report"]).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

        self.stdout.write(
            f"Geocoded {len(placement.placed):,}/{placement.stations_total:,} Stations "
            f"({placement.coverage:.2%}), {placement.from_overrides:,} from hand corrections"
        )
        worst = sorted(
            placement.coverage_by_state().items(),
            key=lambda item: (item[1]["coverage"], -item[1]["stations"]),
        )[:8]
        for state, tally in worst:
            self.stdout.write(
                f"  {state}: {tally['geocoded']}/{tally['stations']} ({tally['coverage']:.1%})"
            )

        floor = settings.GAZETTEER_COVERAGE_FLOOR
        if placement.coverage < floor:
            message = f"Coverage {placement.coverage:.2%} is below the {floor:.0%} floor."
            if options["strict"]:
                raise CommandError(message)
            self.stdout.write(self.style.WARNING(message))
        self.stdout.write(self.style.SUCCESS(f"Wrote {options['output']} and {options['report']}"))


def _write_stations(path: Path, placement: GeocodingResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(STATION_FIELDS)
        for placed in placement.placed:
            station = placed.station
            writer.writerow(
                [
                    station.opis_id,
                    station.name,
                    station.address,
                    station.city,
                    station.state,
                    f"{placed.point.lat:.6f}",
                    f"{placed.point.lon:.6f}",
                    f"{station.price_mean:.6f}",
                    f"{station.price_min:.6f}",
                    f"{station.price_max:.6f}",
                    station.price_sample_count,
                ]
            )
