"""The in-memory Candidate registry.

Stations are grouped by coordinate and each group collapses to one Candidate at
the minimum price, carrying the count of Stations it stands for. Co-located
Stations share a Mile Marker and a Detour on every route, so only the cheapest
of them can ever be worth choosing -- which makes the collapse valid once, at
load time, rather than per request.

Loading is lazy and happens behind :func:`candidates`. Not at import time and
not in ``AppConfig.ready()``: both run during ``migrate`` on a fresh clone,
before the table exists.
"""

from typing import TYPE_CHECKING

from django.conf import settings

from apps.routing.corridor import Candidate, opis_sort_key

if TYPE_CHECKING:
    from apps.stations.models import Station


class _Registry:
    def __init__(self) -> None:
        self._candidates: list[Candidate] | None = None

    def candidates(self) -> list[Candidate]:
        if self._candidates is None:
            self._candidates = _build()
        return self._candidates

    def reset(self) -> None:
        self._candidates = None


_REGISTRY = _Registry()


def candidates() -> list[Candidate]:
    """Every Candidate in the country, built on first access and then cached."""
    return _REGISTRY.candidates()


def reset() -> None:
    """Drop the cached Candidates, so the next access rebuilds them."""
    _REGISTRY.reset()


def _build() -> list[Candidate]:
    from apps.stations.models import Station

    decimals = settings.COORD_ROUNDING_DECIMALS
    groups: dict[tuple[float, float], list[Station]] = {}
    fields = ("opis_id", "name", "city", "state", "latitude", "longitude", "price_mean")
    for station in Station.objects.only(*fields).iterator():
        key = (round(station.latitude, decimals), round(station.longitude, decimals))
        groups.setdefault(key, []).append(station)

    return [collapse(stations) for stations in groups.values()]


def collapse(stations: list["Station"]) -> Candidate:
    """One Candidate for a coordinate: its cheapest Station and how many share it.

    Ties on price are broken by OPIS ID ascending, so a repeated request names
    the same Station and returns a byte-identical payload.
    """
    cheapest = min(stations, key=lambda s: (s.price_mean, opis_sort_key(s.opis_id)))
    return Candidate(
        point=cheapest.point,
        opis_id=cheapest.opis_id,
        name=cheapest.name,
        city=cheapest.city,
        state=cheapest.state,
        price_per_gallon=cheapest.price_mean,
        stations_at_location=len(stations),
    )
