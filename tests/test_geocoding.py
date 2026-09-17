"""City-to-coordinate matching against the committed Gazetteer."""

import pytest

from apps.routing.geo import Point
from apps.stations.geocoding import (
    Gazetteer,
    GazetteerPlace,
    canonical_place_name,
    geocode_stations,
    inside_us_bounds,
    normalize_city,
    place_aliases,
    read_city_overrides,
)


class TestNormalization:
    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [
            ("Gila Bend", "GILA BEND"),
            ("  big cabin  ", "BIG CABIN"),
            ("St. Cloud", "SAINT CLOUD"),
            ("Ste. Genevieve", "SAINTE GENEVIEVE"),
            ("Mt. Vernon", "MOUNT VERNON"),
            ("Ft. Worth", "FORT WORTH"),
            ("Winston-Salem", "WINSTON SALEM"),
            ("O'Fallon", "O FALLON"),
            # Accented spellings: the price file writes Canon City, the
            # Census writes Cañon City.
            ("Ca\u00f1on City", "CANON CITY"),
            ("Espa\u00f1ola", "ESPANOLA"),
            # Compass abbreviations, as whole words only.
            ("S Coffeyville", "SOUTH COFFEYVILLE"),
            ("N Las Vegas", "NORTH LAS VEGAS"),
            ("W Memphis", "WEST MEMPHIS"),
            ("E Saint Louis", "EAST SAINT LOUIS"),
        ],
    )
    def test_spellings_that_mean_the_same_city_normalize_together(self, supplied, expected):
        assert normalize_city(supplied) == expected

    def test_saint_is_only_expanded_as_a_whole_word(self):
        assert normalize_city("Stockton") == "STOCKTON"
        assert normalize_city("Sterling") == "STERLING"

    def test_compass_letters_are_only_expanded_as_whole_words(self):
        assert normalize_city("Salem") == "SALEM"
        assert normalize_city("Waco") == "WACO"


class TestCanonicalPlaceNames:
    @pytest.mark.parametrize(
        ("census_name", "expected"),
        [
            ("Gila Bend town", "Gila Bend"),
            ("Abanda CDP", "Abanda"),
            ("Chicago city", "Chicago"),
            # One suffix only: "Oklahoma City city" must not lose both.
            ("Oklahoma City city", "Oklahoma City"),
            ("Salt Lake City city", "Salt Lake City"),
            # "(balance)" is stripped before the suffix, so both go.
            ("Indianapolis city (balance)", "Indianapolis"),
            ("Nashville-Davidson metropolitan government (balance)", "Nashville-Davidson"),
            ("Autaugaville CCD", "Autaugaville"),
            ("Mahwah township", "Mahwah"),
        ],
    )
    def test_the_census_suffix_is_stripped_exactly_once(self, census_name, expected):
        assert canonical_place_name(census_name) == expected

    def test_a_name_that_is_only_a_suffix_survives(self):
        assert canonical_place_name("Town of Pines town") == "Town of Pines"


class TestPlaceAliases:
    def test_consolidated_governments_alias_to_their_city_half(self):
        assert "NASHVILLE" in place_aliases("Nashville-Davidson")
        assert "AUGUSTA" in place_aliases("Augusta-Richmond County")
        assert "LOUISVILLE" in place_aliases("Louisville/Jefferson County metro")

    def test_a_trailing_city_is_aliased_away(self):
        assert "BOISE" in place_aliases("Boise City")

    def test_a_plain_name_has_no_aliases(self):
        assert place_aliases("Gila Bend") == []


def place(name: str, state: str, lat: float, lon: float, **kwargs) -> GazetteerPlace:
    return GazetteerPlace(
        name=name,
        state=state,
        point=Point(lat, lon),
        area_sqmi=kwargs.get("area", 1.0),
        tier=kwargs.get("tier", 3),
    )


class TestGazetteerLookup:
    @pytest.fixture
    def gazetteer(self) -> Gazetteer:
        return Gazetteer(
            [
                place("GILA BEND", "AZ", 32.9479, -112.7166),
                place("SPRINGFIELD", "IL", 39.7817, -89.6501, area=65.0),
                place("SPRINGFIELD", "MO", 37.1943, -93.2925, area=82.0),
                place("SPRINGFIELD", "MA", 42.1155, -72.5400, area=31.0),
                place("BREEZEWOOD", "PA", 39.9948, -78.2453, tier=0, area=0.0),
            ]
        )

    def test_a_city_and_state_resolve_to_a_coordinate(self, gazetteer):
        found = gazetteer.resolve("Gila Bend", "AZ")
        assert found is not None
        assert found.point == pytest.approx((32.9479, -112.7166))

    def test_the_lookup_is_insensitive_to_spelling_noise(self, gazetteer):
        assert gazetteer.resolve("  gila bend ", "az") is not None

    def test_a_city_absent_from_the_gazetteer_does_not_resolve(self, gazetteer):
        assert gazetteer.resolve("Atlantis", "AZ") is None

    def test_a_city_in_the_wrong_state_does_not_resolve(self, gazetteer):
        assert gazetteer.resolve("Gila Bend", "TX") is None

    def test_a_legal_suffix_on_the_query_is_stripped_too(self):
        # The price file writes "Monroe Township, NJ"; the Census subdivision
        # is "Monroe township", which canonicalises to "Monroe".
        gazetteer = Gazetteer([place("MONROE", "NJ", 40.3204, -74.4271)])
        assert gazetteer.resolve("Monroe Township", "NJ") is not None

    def test_one_place_is_kept_per_name_and_state(self):
        gazetteer = Gazetteer(
            [
                place("SPRINGFIELD", "VA", 38.7893, -77.1872, tier=3, area=6.0),
                place("SPRINGFIELD", "VA", 37.0000, -79.0000, tier=-1, area=0.0),
            ]
        )
        assert len(gazetteer) == 1
        found = gazetteer.resolve("Springfield", "VA")
        assert found is not None and found.tier == 3

    def test_a_bare_city_name_resolves_to_the_largest_match(self, gazetteer):
        found = gazetteer.resolve_name("Springfield")
        assert found is not None
        assert found.state == "MO"

    def test_a_census_place_outranks_a_lesser_tier_on_a_bare_name(self):
        gazetteer = Gazetteer(
            [
                place("FRANKLIN", "TN", 35.9251, -86.8689, tier=3, area=41.0),
                place("FRANKLIN", "VA", 36.6777, -76.9225, tier=0, area=0.0),
            ]
        )
        assert gazetteer.resolve_name("Franklin").state == "TN"

    def test_the_count_is_the_number_of_places(self, gazetteer):
        assert len(gazetteer) == 5


class TestUsContainment:
    @pytest.fixture
    def gazetteer(self) -> Gazetteer:
        return Gazetteer(
            [
                place("DETROIT", "MI", 42.3314, -83.0458),
                place("BUFFALO", "NY", 42.8864, -78.8784),
                place("SAN DIEGO", "CA", 32.7157, -117.1611),
            ]
        )

    def test_a_point_on_a_us_city_finds_it(self, gazetteer):
        found = gazetteer.nearest(Point(42.33, -83.05), radius_miles=50.0)
        assert found is not None and found.name == "DETROIT"

    def test_windsor_ontario_is_outside_a_thirty_mile_radius_of_nothing(self, gazetteer):
        # Windsor sits two miles from Detroit, so a radius test alone cannot
        # reject it. That is a documented limit of containment-by-proximity,
        # and the reason the error names the place it found.
        found = gazetteer.nearest(Point(42.3149, -83.0364), radius_miles=50.0)
        assert found is not None and found.name == "DETROIT"

    def test_mid_pacific_is_nowhere_near_a_us_place(self, gazetteer):
        assert gazetteer.nearest(Point(20.0, -150.0), radius_miles=50.0) is None

    def test_mexico_city_is_beyond_the_radius(self, gazetteer):
        assert gazetteer.nearest(Point(19.4326, -99.1332), radius_miles=50.0) is None

    def test_without_a_radius_the_closest_place_anywhere_comes_back(self, gazetteer):
        found = gazetteer.nearest(Point(19.4326, -99.1332))
        assert found is not None and found.name == "SAN DIEGO"

    def test_the_nearest_place_is_the_one_returned(self, gazetteer):
        found = gazetteer.nearest(Point(42.87, -78.87), radius_miles=50.0)
        assert found is not None and found.name == "BUFFALO"


class TestCsvRoundTrip:
    def test_a_gazetteer_survives_a_write_and_a_read(self, tmp_path):
        original = Gazetteer(
            [
                place("GILA BEND", "AZ", 32.9479, -112.7166, area=23.5, tier=3),
                place("BREEZEWOOD", "PA", 39.9948, -78.2453, area=0.0, tier=0),
            ]
        )
        path = tmp_path / "gazetteer.csv"
        original.write_csv(path)
        reloaded = Gazetteer.from_csv(path)
        assert len(reloaded) == 2
        found = reloaded.resolve("Gila Bend", "AZ")
        assert found is not None
        assert found.point == pytest.approx((32.9479, -112.7166))
        assert found.area_sqmi == pytest.approx(23.5)
        assert found.tier == 3


class TestUsBounds:
    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [
            (32.7767, -96.7970),  # Dallas
            (61.2181, -149.9003),  # Anchorage
            (21.3069, -157.8583),  # Honolulu
            (52.9403, 172.9118),  # Attu, Alaska, across the antimeridian
            (18.4655, -66.1057),  # San Juan, Puerto Rico
        ],
    )
    def test_real_us_coordinates_are_inside(self, latitude, longitude):
        assert inside_us_bounds(Point(latitude, longitude))

    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [
            (0.0, 0.0),  # The unmapped-feature coordinate GNIS uses
            (51.5074, -0.1278),  # London
            (-33.8688, 151.2093),  # Sydney
            (-15.7939, -47.8828),  # Brasilia: right longitude, wrong hemisphere
        ],
    )
    def test_coordinates_that_cannot_be_in_the_usa_are_outside(self, latitude, longitude):
        assert not inside_us_bounds(Point(latitude, longitude))

    def test_the_box_is_a_garbage_filter_not_a_containment_test(self):
        # Mexico City sits inside this crude box, and so would Windsor. That
        # is the division of labour: the box rejects coordinates that cannot
        # be a US place at all, and the resolver's nearest-place-within-50-
        # miles test is what decides whether a point is in the USA.
        assert inside_us_bounds(Point(19.4326, -99.1332))


class TestCityOverrides:
    def test_overrides_are_read_and_keyed_the_way_cities_are_matched(self, tmp_path):
        path = tmp_path / "city_overrides.csv"
        path.write_text(
            'city,state,latitude,longitude,source\nSt. Elsewhere,ga,33.5,-84.3,"GNIS: somewhere"\n',
            encoding="utf-8",
        )
        overrides = read_city_overrides(path)
        assert overrides[("SAINT ELSEWHERE", "GA")] == Point(33.5, -84.3)

    def test_a_missing_overrides_file_is_simply_no_overrides(self, tmp_path):
        assert read_city_overrides(tmp_path / "absent.csv") == {}

    def test_the_committed_overrides_are_all_inside_the_usa(self):
        from django.conf import settings

        overrides = read_city_overrides(settings.CITY_OVERRIDES_CSV)
        assert overrides, "the committed overrides file should not be empty"
        for key, point in overrides.items():
            assert inside_us_bounds(point), key


def cleaned_station(opis_id: str, city: str, state: str):
    from apps.stations.cleaning import Station as CleanedStation

    return CleanedStation(
        opis_id=opis_id,
        name=f"TRUCKSTOP {opis_id}",
        address="EXIT 1",
        city=city,
        state=state,
        rack_id="1",
        price_mean=3.0,
        price_min=3.0,
        price_max=3.0,
        price_sample_count=1,
    )


class TestGeocodingStations:
    @pytest.fixture
    def gazetteer(self) -> Gazetteer:
        return Gazetteer(
            [
                place("GILA BEND", "AZ", 32.9479, -112.7166),
                place("BIG CABIN", "OK", 36.5390, -95.2210),
                # A bad source row: the coordinate GNIS uses for an unmapped
                # feature.
                place("NULL ISLAND", "GA", 0.0, 0.0, tier=-1),
            ]
        )

    def test_stations_are_placed_at_their_city(self, gazetteer):
        result = geocode_stations([cleaned_station("1", "Gila Bend", "AZ")], gazetteer)
        assert len(result.placed) == 1
        assert result.placed[0].point == pytest.approx((32.9479, -112.7166))
        assert result.placed[0].from_override is False
        assert result.coverage == 1.0

    def test_an_override_beats_the_gazetteer(self, gazetteer):
        overrides = {("GILA BEND", "AZ"): Point(33.0, -112.0)}
        result = geocode_stations([cleaned_station("1", "Gila Bend", "AZ")], gazetteer, overrides)
        assert result.placed[0].point == Point(33.0, -112.0)
        assert result.placed[0].from_override is True
        assert result.from_overrides == 1

    def test_an_unresolved_city_is_excluded_and_counted(self, gazetteer):
        result = geocode_stations(
            [cleaned_station("1", "Atlantis", "AZ"), cleaned_station("2", "Gila Bend", "AZ")],
            gazetteer,
        )
        assert len(result.placed) == 1
        assert result.excluded == {("Atlantis", "AZ"): 1}
        assert result.excluded_cities() == [{"city": "Atlantis", "state": "AZ", "stations": 1}]
        assert result.coverage == pytest.approx(0.5)

    def test_a_coordinate_outside_the_usa_is_excluded_rather_than_used(self, gazetteer):
        result = geocode_stations([cleaned_station("1", "Null Island", "GA")], gazetteer)
        assert result.placed == []
        assert sum(result.out_of_bounds.values()) == 1
        assert sum(result.excluded.values()) == 1

    def test_coverage_is_reported_per_state(self, gazetteer):
        result = geocode_stations(
            [
                cleaned_station("1", "Gila Bend", "AZ"),
                cleaned_station("2", "Atlantis", "AZ"),
                cleaned_station("3", "Big Cabin", "OK"),
            ],
            gazetteer,
        )
        assert result.coverage_by_state() == {
            "AZ": {"stations": 2, "geocoded": 1, "coverage": 0.5},
            "OK": {"stations": 1, "geocoded": 1, "coverage": 1.0},
        }

    def test_the_worst_cities_come_first_so_the_report_is_actionable(self, gazetteer):
        stations = [cleaned_station(str(i), "Atlantis", "AZ") for i in range(3)]
        stations.append(cleaned_station("9", "Avalon", "OK"))
        result = geocode_stations(stations, gazetteer)
        assert [row["city"] for row in result.excluded_cities()] == ["Atlantis", "Avalon"]

    def test_no_stations_is_zero_coverage_rather_than_a_crash(self, gazetteer):
        result = geocode_stations([], gazetteer)
        assert result.coverage == 0.0
        assert result.coverage_by_state() == {}
