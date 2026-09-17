"""Distance arithmetic, checked against independently published figures."""

import math

import pytest

from apps.routing.geo import (
    MILES_PER_DEGREE_LATITUDE,
    Point,
    haversine_miles,
    planar_distance_miles,
    planar_offsets_miles,
)

# Great-circle distances published independently of this code.
LAX = Point(33.9425, -118.4081)
JFK = Point(40.6398, -73.7789)
DALLAS = Point(32.7767, -96.7970)
CHICAGO = Point(41.8781, -87.6298)


class TestHaversine:
    def test_matches_published_transcontinental_distance(self):
        # LAX -> JFK is published as 3,983 km = 2,475 statute miles.
        assert haversine_miles(LAX, JFK) == pytest.approx(2475, rel=0.005)

    def test_matches_published_dallas_chicago_distance(self):
        # Straight-line Dallas -> Chicago is published as 803 miles.
        assert haversine_miles(DALLAS, CHICAGO) == pytest.approx(803, rel=0.005)

    def test_quarter_of_the_equator_is_a_quarter_of_the_circumference(self):
        # The equatorial circumference is 24,901 miles.
        assert haversine_miles(Point(0.0, 0.0), Point(0.0, 90.0)) == pytest.approx(
            24901 / 4, rel=0.005
        )

    def test_one_degree_of_latitude_is_sixty_nine_miles(self):
        assert haversine_miles(Point(40.0, -100.0), Point(41.0, -100.0)) == pytest.approx(
            69.0, rel=0.005
        )
        assert pytest.approx(69.0, rel=0.005) == MILES_PER_DEGREE_LATITUDE

    def test_a_point_is_no_distance_from_itself(self):
        assert haversine_miles(DALLAS, DALLAS) == 0.0

    def test_is_symmetric(self):
        assert haversine_miles(DALLAS, CHICAGO) == pytest.approx(haversine_miles(CHICAGO, DALLAS))


class TestPlanarApproximation:
    def test_tracks_haversine_over_corridor_scale_distances(self):
        near = Point(DALLAS.lat + 0.1, DALLAS.lon + 0.1)
        assert planar_distance_miles(DALLAS, near) == pytest.approx(
            haversine_miles(DALLAS, near), rel=0.002
        )

    def test_longitude_shrinks_with_latitude(self):
        one_degree_east_of_dallas = planar_distance_miles(DALLAS, Point(DALLAS.lat, DALLAS.lon + 1))
        assert one_degree_east_of_dallas == pytest.approx(
            MILES_PER_DEGREE_LATITUDE * math.cos(math.radians(DALLAS.lat)), rel=1e-9
        )

    def test_offsets_are_signed_east_and_north(self):
        east, north = planar_offsets_miles(DALLAS, Point(DALLAS.lat + 0.5, DALLAS.lon + 0.5))
        assert east > 0
        assert north > 0
        west, south = planar_offsets_miles(DALLAS, Point(DALLAS.lat - 0.5, DALLAS.lon - 0.5))
        assert west < 0
        assert south < 0
