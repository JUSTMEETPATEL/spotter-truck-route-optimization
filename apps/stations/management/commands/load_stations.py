"""Load time: the committed geocoded CSV -> the Station table.

    uv run manage.py load_stations

This command makes **no network calls**, and neither does serving a request.
Keeping it separate from ``build_station_data`` and ``build_gazetteer``, which
may download their sources, is what guarantees the per-request external call
count.
"""

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.stations import registry
from apps.stations.models import Station

#: Rows per bulk insert. Large enough to be fast, small enough for SQLite's
#: parameter limit.
BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Load the committed geocoded Station CSV into the database. Makes no network calls."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--csv", type=Path, default=settings.STATIONS_GEOCODED_CSV)

    def handle(self, *args: Any, **options: Any) -> None:
        import csv

        path = Path(options["csv"])
        if not path.exists():
            raise CommandError(
                f"No geocoded Station CSV at {path}. "
                "Run `manage.py build_station_data` first (or check out the committed file)."
            )

        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        stations = [
            Station(
                opis_id=row["opis_id"],
                name=row["name"],
                address=row["address"],
                city=row["city"],
                state=row["state"],
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                price_mean=float(row["price_mean"]),
                price_min=float(row["price_min"]),
                price_max=float(row["price_max"]),
                price_sample_count=int(row["price_sample_count"]),
            )
            for row in rows
        ]

        # Replace wholesale rather than upsert: the committed CSV is the source
        # of truth, so a Station that left the file must leave the table.
        with transaction.atomic():
            Station.objects.all().delete()
            Station.objects.bulk_create(stations, batch_size=BATCH_SIZE)

        registry.reset()
        self.stdout.write(
            self.style.SUCCESS(f"Loaded {len(stations):,} Stations from {path} (0 network calls)")
        )
