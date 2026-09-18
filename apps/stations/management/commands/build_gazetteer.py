"""Build time: public domain Census and USGS files -> the committed Gazetteer.

This command reaches the network. It runs once, its output
(``data/gazetteer.csv``) is committed, and nothing else in the project
downloads anything -- which is what keeps serving a request to a single
external call.

    uv run manage.py build_gazetteer

Sources, all public domain:

* US Census Gazetteer **Places** -- incorporated places and CDPs.
* US Census Gazetteer **County Subdivisions** -- the townships that the
  Northeast uses as its city names and that the Places file does not carry.
* USGS GNIS **Domestic Names**, populated places only -- the unincorporated
  communities ("Ruther Glen, VA", "Breezewood, PA") that truckstops sit in and
  that neither Census file lists.

Each source indexes at a lower precedence than the one above it, so a named
Census place always wins a contested name.
"""

import csv
import io
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.routing.geo import Point
from apps.stations.geocoding import (
    TIER_PLACE,
    TIER_PLACE_ALIAS,
    TIER_SUBDIVISION,
    TIER_SUBDIVISION_ALIAS,
    US_STATE_CODES,
    Gazetteer,
    GazetteerPlace,
    inside_us_bounds,
    normalize_city,
    places_from_census_rows,
)

#: GNIS entries are settlements without a published land area, so they index
#: below every Census row and can never win a contested name.
TIER_GNIS = -1
#: The one GNIS feature class that names a place people live in.
GNIS_POPULATED_PLACE = "Populated Place"


class Command(BaseCommand):
    help = "Build the committed offline Gazetteer from Census and USGS files."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--output",
            type=Path,
            default=settings.GAZETTEER_CSV,
            help="Where to write the Gazetteer CSV.",
        )
        parser.add_argument(
            "--places-zip", type=Path, help="A local copy of the Census Places zip."
        )
        parser.add_argument(
            "--cousubs-zip", type=Path, help="A local copy of the Census County Subdivisions zip."
        )
        parser.add_argument(
            "--gnis-zip", type=Path, help="A local copy of the USGS GNIS Domestic Names zip."
        )
        parser.add_argument(
            "--skip-gnis",
            action="store_true",
            help="Build from the two Census files only. Coverage drops to about 97%%.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        places: list[GazetteerPlace] = []

        self.stdout.write("Reading Census Places…")
        places.extend(
            places_from_census_rows(
                self._census_rows(options["places_zip"], settings.GAZETTEER_PLACES_URL),
                name_field="NAME",
                tier=TIER_PLACE,
                alias_tier=TIER_PLACE_ALIAS,
            )
        )
        self.stdout.write(f"  {len(places):,} entries")

        self.stdout.write("Reading Census County Subdivisions…")
        before = len(places)
        places.extend(
            places_from_census_rows(
                self._census_rows(options["cousubs_zip"], settings.GAZETTEER_COUSUBS_URL),
                name_field="NAME",
                tier=TIER_SUBDIVISION,
                alias_tier=TIER_SUBDIVISION_ALIAS,
            )
        )
        self.stdout.write(f"  {len(places) - before:,} entries")

        if not options["skip_gnis"]:
            self.stdout.write("Reading USGS GNIS populated places…")
            before = len(places)
            places.extend(self._gnis_places(options["gnis_zip"], settings.GAZETTEER_GNIS_URL))
            self.stdout.write(f"  {len(places) - before:,} entries")

        gazetteer = Gazetteer(places)
        output = Path(options["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        gazetteer.write_csv(output)
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(gazetteer):,} Gazetteer entries to {output} "
                f"({output.stat().st_size / 1_048_576:.1f} MB)"
            )
        )

    # --- sources ------------------------------------------------------------

    def _census_rows(self, local: Path | None, url: str) -> list[dict[str, str]]:
        with self._zipped_text(local, url) as text:
            return list(csv.DictReader(text, delimiter="\t"))

    def _gnis_places(self, local: Path | None, url: str) -> list[GazetteerPlace]:
        """Populated places from the GNIS national file.

        GNIS spells the state out and carries no land area, so entries index
        below every Census row and can never win a contested name.
        """
        places: list[GazetteerPlace] = []
        with self._zipped_text(local, url) as text:
            for row in csv.DictReader(text, delimiter="|"):
                if row.get("feature_class") != GNIS_POPULATED_PLACE:
                    continue
                latitude, longitude = row.get("prim_lat_dec"), row.get("prim_long_dec")
                name = row.get("feature_name")
                state = US_STATE_CODES.get((row.get("state_name") or "").strip())
                if not (latitude and longitude and name and state):
                    continue
                point = Point(float(latitude), float(longitude))
                # 5,603 GNIS populated places carry 0.0, 0.0 for an unmapped
                # feature. Left in, one of them put three Atlanta truckstops
                # in the Gulf of Guinea.
                if not inside_us_bounds(point):
                    continue
                places.append(
                    GazetteerPlace(
                        name=normalize_city(name),
                        state=state,
                        point=point,
                        area_sqmi=0.0,
                        tier=TIER_GNIS,
                    )
                )
        return places

    @contextmanager
    def _zipped_text(self, local: Path | None, url: str) -> Iterator[Iterator[str]]:
        """Line-by-line access to the one text file inside a zip.

        Streamed rather than read whole: the GNIS member is 141 MB.
        """
        path = Path(local) if local is not None else self._download(url)
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".txt")]
            if len(members) != 1:
                raise CommandError(f"Expected one .txt inside {path.name}, found {members}")
            with archive.open(members[0]) as member:
                yield io.TextIOWrapper(member, encoding="utf-8-sig", errors="replace")

    def _download(self, url: str) -> Path:
        """Fetch a source archive to a temporary file and return its path."""
        self.stdout.write(f"  downloading {url}")
        try:
            response = requests.get(
                url, timeout=settings.GAZETTEER_DOWNLOAD_TIMEOUT_SECONDS, stream=True
            )
            response.raise_for_status()
            handle = NamedTemporaryFile(suffix=".zip", delete=False)  # noqa: SIM115
            with handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    handle.write(chunk)
        except requests.RequestException as error:
            raise CommandError(f"Could not download {url}: {error}") from error
        return Path(handle.name)
