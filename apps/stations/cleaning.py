"""Turning the supplied Price Observations into Stations.

Three jobs, in order: drop the rows the brief does not scope, drop the rows
that cannot be used, and collapse the repeats of one OPIS Truckstop ID into a
single Station carrying the mean of its prices and the spread behind it.

The file carries no date column, so repeats are read as observations over time
and averaged. Taking the minimum would quote a total the trip cannot achieve.
Evidence for that reading: every duplicated OPIS ID in the supplied file shares
a single Rack ID, so the repeats are not rack-market variants.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from apps.routing.corridor import opis_sort_key

#: Province codes in the supplied file. The brief scopes to the USA.
CANADIAN_PROVINCES = frozenset(
    {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
)


@dataclass(frozen=True)
class Station:
    """A distinct truckstop, with the spread of its Price Observations kept."""

    opis_id: str
    name: str
    address: str
    city: str
    state: str
    rack_id: str
    price_mean: float
    price_min: float
    price_max: float
    price_sample_count: int


@dataclass
class CleaningResult:
    """The Stations, and an account of every row that did not become one."""

    stations: list[Station] = field(default_factory=list)
    observations_read: int = 0
    non_us_observations_dropped: int = 0
    unusable_observations_dropped: int = 0


@dataclass
class _Accumulator:
    names: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    city: str = ""
    state: str = ""
    rack_id: str = ""
    prices: list[float] = field(default_factory=list)


def clean_price_observations(rows: Iterable[Mapping[str, str]]) -> CleaningResult:
    """Collapse Price Observation rows into Stations, in OPIS ID order."""
    result = CleaningResult()
    accumulators: dict[str, _Accumulator] = {}

    for row in rows:
        result.observations_read += 1
        state = _text(row.get("State"))
        if state in CANADIAN_PROVINCES:
            result.non_us_observations_dropped += 1
            continue

        opis_id = _text(row.get("OPIS Truckstop ID"))
        city = _text(row.get("City"))
        price = _price(row.get("Retail Price"))
        # A Station with no ID, no city or no price cannot be geocoded, priced
        # or identified, so it is not a usable observation.
        if not opis_id or not city or not state or price is None:
            result.unusable_observations_dropped += 1
            continue

        accumulator = accumulators.setdefault(opis_id, _Accumulator())
        accumulator.names.append(_text(row.get("Truckstop Name")))
        accumulator.addresses.append(_text(row.get("Address")))
        accumulator.city = city
        accumulator.state = state
        accumulator.rack_id = _text(row.get("Rack ID"))
        accumulator.prices.append(price)

    for opis_id in sorted(accumulators, key=opis_sort_key):
        accumulator = accumulators[opis_id]
        result.stations.append(
            Station(
                opis_id=opis_id,
                name=_most_descriptive(accumulator.names),
                address=_most_descriptive(accumulator.addresses),
                city=accumulator.city,
                state=accumulator.state,
                rack_id=accumulator.rack_id,
                price_mean=sum(accumulator.prices) / len(accumulator.prices),
                price_min=min(accumulator.prices),
                price_max=max(accumulator.prices),
                price_sample_count=len(accumulator.prices),
            )
        )
    return result


def _text(value: str | None) -> str:
    return (value or "").strip()


def _price(value: str | None) -> float | None:
    try:
        price = float(_text(value))
    except ValueError:
        return None
    return price if price > 0.0 else None


def _most_descriptive(values: list[str]) -> str:
    """The longest of the repeated spellings, ties broken alphabetically.

    "PILOT TRAVEL CENTER #1243" and "PILOT #1243" are the same truckstop; the
    longer spelling says more, and picking by length rather than by file order
    keeps the choice independent of how the rows happen to be sorted.
    """
    return min(values, key=lambda value: (-len(value), value)) if values else ""
