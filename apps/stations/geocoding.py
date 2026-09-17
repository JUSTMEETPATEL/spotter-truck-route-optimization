"""The offline Gazetteer: city and state to coordinates.

The only source of Station and endpoint coordinates. Built once from public
domain US Census Gazetteer files and committed to the repo, so nothing here
reaches the network -- which is what holds the per-request external call count
at one.

Matching is by normalized name and state. Census place names carry a legal
suffix ("Gila Bend town", "Abanda CDP") which is stripped, and consolidated
city-counties ("Nashville-Davidson metropolitan government (balance)") are
additionally indexed under the city half, because that is what the price file
calls them.
"""

import csv
import math
import re
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from apps.routing.geo import Point, haversine_miles

#: Legal suffixes the Census appends to a place name, longest first so that
#: "city and borough" wins over "city".
_SUFFIX = re.compile(
    r"\s+(?:"
    r"consolidated government|metropolitan government|metro government|"
    r"unified government|city and borough|charter township|census area|"
    r"urban county|municipality|comunidad|zona urbana|corporation|"
    r"township|borough|village|municipio|urbana|county|city|town|CDP|CCD|UT"
    r")$",
    re.IGNORECASE,
)
_BALANCE = re.compile(r"\s*\(balance\)\s*$", re.IGNORECASE)
#: Abbreviations the price file and the Census spell differently. Applied to
#: whole words only, so Stockton stays Stockton and Salem stays Salem.
_ABBREVIATIONS = {
    "ST": "SAINT",
    "STE": "SAINTE",
    "MT": "MOUNT",
    "FT": "FORT",
    "N": "NORTH",
    "S": "SOUTH",
    "E": "EAST",
    "W": "WEST",
}

#: USPS codes by the state name the USGS GNIS files spell out. The Census
#: files carry the code directly, so this is only needed for GNIS.
US_STATE_CODES = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
    "American Samoa": "AS",
    "Guam": "GU",
    "Commonwealth of the Northern Mariana Islands": "MP",
    "Puerto Rico": "PR",
    "United States Virgin Islands": "VI",
    "Virgin Islands": "VI",
}

#: Precedence when two places claim one name: a named Census place beats a
#: county subdivision, and an exact name beats an alias.
TIER_PLACE = 3
TIER_PLACE_ALIAS = 2
TIER_SUBDIVISION = 1
TIER_SUBDIVISION_ALIAS = 0


def normalize_city(name: str) -> str:
    """The matching key for a city name: upper case, punctuation-free, expanded.

    Accents are folded before punctuation is dropped, so the file's "Canon
    City" reaches the Census's "Cañon City" instead of becoming "CA ON CITY".
    """
    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", name)
        if not unicodedata.combining(character)
    )
    collapsed = re.sub(r"[^A-Z0-9]+", " ", folded.upper())
    return " ".join(_ABBREVIATIONS.get(word, word) for word in collapsed.split())


def canonical_place_name(census_name: str) -> str:
    """A Census place name with "(balance)" and one legal suffix removed.

    Exactly one suffix: "Oklahoma City city" is Oklahoma City, not Oklahoma.
    """
    without_balance = _BALANCE.sub("", census_name).strip()
    stripped = _SUFFIX.sub("", without_balance, count=1).strip()
    return stripped or without_balance


def place_aliases(canonical_name: str) -> list[str]:
    """Other names the price file might use for this place, normalized.

    Consolidated city-counties are indexed under the city half, and a place
    whose name ends in "City" is also indexed without it, because the file
    writes Boise City as Boise.
    """
    aliases: list[str] = []
    head = re.split(r"[-/]", canonical_name)[0].strip()
    if head and head != canonical_name:
        aliases.append(normalize_city(canonical_place_name(head)))
    for candidate in [canonical_name, *aliases]:
        normalized = normalize_city(candidate)
        if normalized.endswith(" CITY"):
            aliases.append(normalized.removesuffix(" CITY").strip())
    primary = normalize_city(canonical_name)
    return [alias for alias in dict.fromkeys(aliases) if alias and alias != primary]


@dataclass(frozen=True)
class GazetteerPlace:
    """One place: a normalized name, its state, and its centroid."""

    name: str
    state: str
    point: Point
    area_sqmi: float
    tier: int

    @property
    def label(self) -> str:
        return f"{self.name.title()}, {self.state}"


class Gazetteer:
    """An in-memory lookup from city and state to a coordinate."""

    def __init__(self, places: Iterable[GazetteerPlace]) -> None:
        # One place per name and state: the sources overlap heavily, and a
        # duplicate of a name already held adds nothing a city-granularity
        # lookup can use.
        self._by_key: dict[tuple[str, str], GazetteerPlace] = {}
        for place in places:
            _keep_best(self._by_key, (place.name, place.state), place)
        self._places = list(self._by_key.values())

        self._by_name: dict[str, GazetteerPlace] = {}
        # A second index with spaces removed catches "Mc Calla" against
        # "McCalla" and "De Forest" against "DeForest".
        self._by_squashed: dict[tuple[str, str], GazetteerPlace] = {}
        for place in self._places:
            _keep_best(self._by_name, place.name, place)
            _keep_best(self._by_squashed, (_squash(place.name), place.state), place)

    def __len__(self) -> int:
        return len(self._places)

    def __iter__(self) -> Iterator[GazetteerPlace]:
        return iter(self._places)

    def resolve(self, city: str, state: str) -> GazetteerPlace | None:
        """The place this city and state name, or None.

        Tries the name as written, then with a legal suffix stripped -- the
        file writes "Monroe Township, NJ" for the subdivision the Census calls
        "Monroe township" -- and finally with spaces squashed out.
        """
        state_code = state.strip().upper()
        for name in _query_spellings(city):
            found = self._by_key.get((name, state_code))
            if found is not None:
                return found
        for name in _query_spellings(city):
            found = self._by_squashed.get((_squash(name), state_code))
            if found is not None:
                return found
        return None

    def resolve_name(self, city: str) -> GazetteerPlace | None:
        """The largest place with this name in any state, or None.

        "Largest" is by land area, which is the size measure the Census
        Gazetteer files carry.
        """
        return self._by_name.get(normalize_city(city))

    def nearest(self, point: Point, radius_miles: float | None = None) -> GazetteerPlace | None:
        """The closest place within ``radius_miles``, or None if there is none.

        With no radius, the closest place in the country however far away --
        which is what makes an out-of-country rejection actionable without
        inventing a second radius to search.

        A radius is the US containment test: a point with no US place near it
        is not in the USA. It degrades better than a bounding box, which
        swallows Ontario, Windsor and much of northern Mexico, though it cannot
        separate Detroit from Windsor either -- so the caller names the place
        it found.
        """
        best: GazetteerPlace | None = None
        best_distance = math.inf
        for place in self._places:
            # Reject on latitude first: it is one subtraction against a
            # haversine, and it discards almost everything.
            if (
                radius_miles is not None
                and abs(place.point.lat - point.lat) * _MILES_PER_DEGREE_LAT > radius_miles
            ):
                continue
            distance = haversine_miles(point, place.point)
            if distance < best_distance:
                best, best_distance = place, distance
        if radius_miles is not None and best_distance > radius_miles:
            return None
        return best

    # --- persistence --------------------------------------------------------

    FIELDS = ("name", "state", "latitude", "longitude", "area_sqmi", "tier")

    def write_csv(self, path: Path) -> None:
        with Path(path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.FIELDS)
            for place in sorted(self._places, key=lambda p: (p.state, p.name, -p.tier)):
                writer.writerow(
                    [
                        place.name,
                        place.state,
                        f"{place.point.lat:.6f}",
                        f"{place.point.lon:.6f}",
                        f"{place.area_sqmi:.4f}",
                        place.tier,
                    ]
                )

    @classmethod
    def from_csv(cls, path: Path) -> "Gazetteer":
        with Path(path).open(newline="", encoding="utf-8") as handle:
            return cls(
                [
                    GazetteerPlace(
                        name=row["name"],
                        state=row["state"],
                        point=Point(float(row["latitude"]), float(row["longitude"])),
                        area_sqmi=float(row["area_sqmi"]),
                        tier=int(row["tier"]),
                    )
                    for row in csv.DictReader(handle)
                ]
            )


def _query_spellings(city: str) -> list[str]:
    """The normalized keys a supplied city name could be indexed under."""
    normalized = normalize_city(city)
    canonical = normalize_city(canonical_place_name(city))
    return [normalized] if canonical == normalized else [normalized, canonical]


_LOADED: dict[str, "Gazetteer"] = {}


def load_gazetteer() -> Gazetteer:
    """The committed Gazetteer, read from disk on first use and then kept.

    Lazy for the same reason the Candidate registry is: nothing may touch a
    data file or a table at import time, or ``migrate`` breaks on a fresh
    clone.
    """
    from django.conf import settings

    if "gazetteer" not in _LOADED:
        _LOADED["gazetteer"] = Gazetteer.from_csv(settings.GAZETTEER_CSV)
    return _LOADED["gazetteer"]


def reset_gazetteer() -> None:
    """Drop the loaded Gazetteer, so the next access reads the file again."""
    _LOADED.clear()


def _keep_best(index: dict, key, place: GazetteerPlace) -> None:
    """Index ``place`` under ``key`` unless something better is already there.

    Strictly better, so the first of two equals wins. The sources are read in
    a fixed order, which is what makes the result reproducible.
    """
    current = index.get(key)
    if current is None or (place.tier, place.area_sqmi) > (current.tier, current.area_sqmi):
        index[key] = place


def _squash(name: str) -> str:
    return name.replace(" ", "")


_MILES_PER_DEGREE_LAT = 69.1


def places_from_census_rows(
    rows: Iterable[dict[str, str]],
    name_field: str,
    tier: int,
    alias_tier: int,
) -> Sequence[GazetteerPlace]:
    """Every place a Census Gazetteer file describes, plus its aliases."""
    places: list[GazetteerPlace] = []
    for row in rows:
        row = {(key or "").strip(): (value or "").strip() for key, value in row.items()}
        if not row.get("INTPTLAT") or not row.get("INTPTLONG"):
            continue
        canonical = canonical_place_name(row[name_field])
        point = Point(float(row["INTPTLAT"]), float(row["INTPTLONG"]))
        area = float(row.get("ALAND_SQMI") or 0.0)
        state = row["USPS"]
        places.append(
            GazetteerPlace(
                name=normalize_city(canonical),
                state=state,
                point=point,
                area_sqmi=area,
                tier=tier,
            )
        )
        places.extend(
            GazetteerPlace(name=alias, state=state, point=point, area_sqmi=area, tier=alias_tier)
            for alias in place_aliases(canonical)
        )
    return places
