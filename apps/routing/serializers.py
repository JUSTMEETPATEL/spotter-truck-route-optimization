"""Request validation and response shape. No business logic lives here.

The parameter bounds are load-bearing rather than decoration: ``mpg=0`` is a
division by zero, and ``max_detour_miles=500`` matches every Candidate in the
country on a long route and disproves this project's performance claims. Being
serializer fields, drf-spectacular publishes them into Swagger.
"""

from typing import Any

from django.conf import settings
from rest_framework import serializers

from apps.routing.services import RouteRequest

MPG_RANGE = (1.0, 20.0)
RANGE_MILES_RANGE = (50.0, 1000.0)
DETOUR_MILES_RANGE = (0.0, 50.0)


class RouteRequestSerializer(serializers.Serializer):
    """A start, a finish, and optionally what the truck is."""

    start = serializers.CharField(
        max_length=120,
        help_text='"City, ST", "City, State", a bare city name, or "lat,lon".',
    )
    finish = serializers.CharField(
        max_length=120,
        help_text='"City, ST", "City, State", a bare city name, or "lat,lon".',
    )
    mpg = serializers.FloatField(
        required=False,
        min_value=MPG_RANGE[0],
        max_value=MPG_RANGE[1],
        help_text=f"Miles per gallon, {MPG_RANGE[0]:g}-{MPG_RANGE[1]:g}. Defaults to FUEL_MPG.",
    )
    max_range_miles = serializers.FloatField(
        required=False,
        min_value=RANGE_MILES_RANGE[0],
        max_value=RANGE_MILES_RANGE[1],
        help_text=(
            f"Miles on a full tank, {RANGE_MILES_RANGE[0]:g}-{RANGE_MILES_RANGE[1]:g}. "
            "Defaults to FUEL_MAX_RANGE_MILES."
        ),
    )
    max_detour_miles = serializers.FloatField(
        required=False,
        min_value=DETOUR_MILES_RANGE[0],
        max_value=DETOUR_MILES_RANGE[1],
        help_text=(
            f"Corridor half-width, {DETOUR_MILES_RANGE[0]:g}-{DETOUR_MILES_RANGE[1]:g}. "
            "Defaults to CORRIDOR_MILES."
        ),
    )

    def to_route_request(self) -> RouteRequest:
        """The validated request, with settings filling anything omitted."""
        data: dict[str, Any] = self.validated_data
        return RouteRequest(
            start=data["start"].strip(),
            finish=data["finish"].strip(),
            mpg=data.get("mpg") or settings.FUEL_MPG,
            max_range_miles=data.get("max_range_miles") or settings.FUEL_MAX_RANGE_MILES,
            max_detour_miles=(
                settings.CORRIDOR_MILES
                if data.get("max_detour_miles") is None
                else data["max_detour_miles"]
            ),
        )


# --- response shape, for the published schema -------------------------------


class EndpointSerializer(serializers.Serializer):
    query = serializers.CharField(help_text="What the caller asked for.")
    name = serializers.CharField(help_text="What the Gazetteer resolved it to.")
    city = serializers.CharField(allow_null=True)
    state = serializers.CharField(allow_null=True)
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()


class RequestEchoSerializer(serializers.Serializer):
    start = EndpointSerializer()
    finish = EndpointSerializer()


class VehicleSerializer(serializers.Serializer):
    max_range_miles = serializers.FloatField()
    mpg = serializers.FloatField()
    tank_gallons = serializers.FloatField(help_text="Derived: Range / mpg. Never configured.")
    max_detour_miles = serializers.FloatField()


class GeometrySerializer(serializers.Serializer):
    type = serializers.CharField(default="LineString")
    coordinates = serializers.ListField(child=serializers.ListField(child=serializers.FloatField()))


class RouteSerializer(serializers.Serializer):
    total_distance_miles = serializers.FloatField()
    total_duration_hours = serializers.FloatField()
    shape_points = serializers.IntegerField()
    geometry = GeometrySerializer()


# "from" is a Python keyword, so this one serializer is assembled from a dict
# rather than declared with class syntax. The response key has to be "from".
InfeasibleStretchSerializer = type(
    "InfeasibleStretchSerializer",
    (serializers.Serializer,),
    {
        "from": serializers.CharField(),
        "to": serializers.CharField(),
        "gap_miles": serializers.FloatField(),
    },
)


class FuelSerializer(serializers.Serializer):
    feasible = serializers.BooleanField()
    infeasible_stretch = InfeasibleStretchSerializer(allow_null=True)
    total_gallons = serializers.FloatField()
    total_cost_usd = serializers.FloatField(allow_null=True)
    average_price_per_gallon = serializers.FloatField(allow_null=True)
    stops_count = serializers.IntegerField()
    naive_cost_usd = serializers.FloatField(
        allow_null=True, help_text="A simulated driver who ignores price. Not an average."
    )
    savings_usd = serializers.FloatField(allow_null=True)
    savings_percent = serializers.FloatField(allow_null=True)


class FuelStopSerializer(serializers.Serializer):
    sequence = serializers.IntegerField()
    opis_id = serializers.CharField()
    name = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    mile_marker = serializers.FloatField()
    detour_miles_to_city = serializers.FloatField(
        help_text="Distance from the route to the city centroid, not to a forecourt."
    )
    stations_at_location = serializers.IntegerField()
    price_per_gallon = serializers.FloatField()
    gallons = serializers.FloatField()
    cost_usd = serializers.FloatField()
    gallons_remaining_after = serializers.FloatField()


class MetaSerializer(serializers.Serializer):
    external_api_calls = serializers.IntegerField()
    cached = serializers.BooleanField()
    provider_ms = serializers.FloatField(
        help_text=(
            "Of compute_ms, the part spent waiting on the routing provider, "
            "retries included. Zero when the road came from a cache."
        )
    )
    compute_ms = serializers.FloatField(
        help_text="What this caller waited for, cache lookup included."
    )
    stations_in_corridor = serializers.IntegerField()
    candidates_considered = serializers.IntegerField()
    map_url = serializers.CharField()
    route_token = serializers.CharField()
    optimizer = serializers.CharField(
        help_text="Which implementation of the greedy rule computed this plan."
    )


class RoutePlanSerializer(serializers.Serializer):
    """The full response. Built in ``services.py``; described here.

    Answer first, geometry last, in the same order the service emits.
    """

    fuel = FuelSerializer()
    fuel_stops = FuelStopSerializer(many=True)
    request = RequestEchoSerializer()
    vehicle = VehicleSerializer()
    meta = MetaSerializer()
    route = RouteSerializer()


class ErrorBodySerializer(serializers.Serializer):
    """What an error actually carries."""

    code = serializers.CharField(help_text="Stable machine-readable code, e.g. place_not_found.")
    message = serializers.CharField(help_text="A sentence naming what went wrong.")
    detail = serializers.DictField(
        required=False, help_text="Field errors, or the nearest place found."
    )


class ErrorSerializer(serializers.Serializer):
    """Errors are nested under one key, so a client can branch on its presence."""

    error = ErrorBodySerializer()


class StationSerializer(serializers.Serializer):
    opis_id = serializers.CharField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    price_mean = serializers.FloatField()
    price_min = serializers.FloatField()
    price_max = serializers.FloatField()
    price_sample_count = serializers.IntegerField()
