"""Distance arithmetic on the sphere and its flat-earth approximation.

Imports nothing from Django: plain data in, plain data out.
"""

import math
from typing import NamedTuple

#: Mean Earth radius in statute miles (IUGG mean radius, 6,371.0088 km).
EARTH_RADIUS_MILES = 3958.7613

#: Miles spanned by one degree of latitude anywhere on the sphere.
MILES_PER_DEGREE_LATITUDE = math.pi * EARTH_RADIUS_MILES / 180.0


class Point(NamedTuple):
    """A coordinate in decimal degrees, latitude first."""

    lat: float
    lon: float


def haversine_miles(a: Point, b: Point) -> float:
    """Great-circle distance between two coordinates, in miles."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    d_lat = lat2 - lat1
    d_lon = math.radians(b.lon - a.lon)
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def planar_offsets_miles(origin: Point, point: Point) -> tuple[float, float]:
    """East and north offsets of ``point`` from ``origin``, in miles.

    The longitude scale is taken at ``origin``'s latitude. Signed, so callers
    can do segment projection in a local plane.
    """
    east = (point.lon - origin.lon) * MILES_PER_DEGREE_LATITUDE * math.cos(math.radians(origin.lat))
    north = (point.lat - origin.lat) * MILES_PER_DEGREE_LATITUDE
    return east, north


def planar_distance_miles(a: Point, b: Point) -> float:
    """Flat-earth distance between two nearby coordinates, in miles.

    Cheap enough for the coarse Corridor pass, where it runs millions of times;
    accurate to a fraction of a percent at Corridor scale.
    """
    east, north = planar_offsets_miles(a, b)
    return math.hypot(east, north)
