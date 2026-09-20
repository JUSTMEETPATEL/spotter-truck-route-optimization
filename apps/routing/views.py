"""The HTTP surface. Views parse, delegate, and map exceptions to statuses.

Nothing here decides anything about fuel.
"""

import json
from typing import Any

from django.conf import settings
from django.db import connection
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.routing.exceptions import (
    FuelRouteError,
    NoRouteFound,
    OutsideUnitedStates,
    PlaceNotFound,
    RoutingProviderUnavailable,
)
from apps.routing.serializers import (
    ErrorSerializer,
    RoutePlanSerializer,
    RouteRequestSerializer,
    StationSerializer,
)
from apps.routing.services import INDEXED, SCANNING, plan_for_map, plan_route, tank_gallons
from apps.stations import registry
from apps.stations.models import Station

#: Every domain error, and the one status code it means.
ERROR_STATUSES: dict[type[FuelRouteError], int] = {
    PlaceNotFound: status.HTTP_400_BAD_REQUEST,
    OutsideUnitedStates: status.HTTP_400_BAD_REQUEST,
    NoRouteFound: status.HTTP_404_NOT_FOUND,
    RoutingProviderUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


class RouteView(APIView):
    """Plan the cheapest legal sequence of diesel purchases along a route."""

    optimizer = SCANNING

    @extend_schema(
        request=RouteRequestSerializer,
        responses={
            200: RoutePlanSerializer,
            400: OpenApiResponse(ErrorSerializer, "Unknown place, outside the USA, or a bad value"),
            404: OpenApiResponse(ErrorSerializer, "No road connects the two points"),
            503: OpenApiResponse(ErrorSerializer, "The routing provider is unreachable"),
        },
        summary="Plan a route and its fuel stops",
    )
    def post(self, request: Request) -> Response:
        return self._plan(request.data)

    @extend_schema(
        parameters=[
            OpenApiParameter("start", str, required=True),
            OpenApiParameter("finish", str, required=True),
            OpenApiParameter("mpg", float),
            OpenApiParameter("max_range_miles", float),
            OpenApiParameter("max_detour_miles", float),
        ],
        responses={200: RoutePlanSerializer, 400: ErrorSerializer},
        summary="The same thing, for a browser address bar",
    )
    def get(self, request: Request) -> Response:
        return self._plan(request.query_params)

    def _plan(self, data: Any) -> Response:
        serializer = RouteRequestSerializer(data=data)
        if not serializer.is_valid():
            return _error_response(
                "invalid_request",
                "The request could not be read.",
                status.HTTP_400_BAD_REQUEST,
                detail=serializer.errors,
            )
        try:
            payload = plan_route(serializer.to_route_request(), optimizer=self.optimizer)
        except FuelRouteError as error:
            return _error_response(
                _code(error),
                error.message,
                ERROR_STATUSES.get(type(error), status.HTTP_400_BAD_REQUEST),
                detail=error.detail,
            )
        return Response(payload)


class RouteV2View(RouteView):
    """The same plan, computed from precomputed indexes.

    Identical request and response; a monotonic stack and a sparse table
    replace the two linear scans inside the optimizer. ``meta.optimizer`` says
    which one answered.
    """

    optimizer = INDEXED


class HealthView(APIView):
    """Liveness, plus the dataset facts that are true of every request."""

    @extend_schema(
        responses={200: OpenApiResponse(description="Service and dataset health")},
        summary="Liveness and dataset coverage",
    )
    def get(self, request: Request) -> Response:
        report = _build_report()
        return Response(
            {
                "status": "ok",
                "stations_loaded": Station.objects.count(),
                "candidates": len(registry.candidates()),
                "dataset": {
                    "observations_read": report.get("observations_read"),
                    "non_us_observations_dropped": report.get("non_us_observations_dropped"),
                    "stations_total": report.get("stations_total"),
                    "stations_geocoded": report.get("stations_geocoded"),
                    "stations_excluded": report.get("stations_excluded"),
                    "stations_out_of_us_bounds": report.get("stations_out_of_us_bounds"),
                    "stations_from_overrides": report.get("stations_from_overrides"),
                    "gazetteer_entries": report.get("gazetteer_entries"),
                    "gazetteer_coverage": report.get("gazetteer_coverage"),
                    "excluded_cities": report.get("excluded_cities", []),
                    "coverage_by_state": report.get("coverage_by_state", {}),
                    "built_at": report.get("generated_at"),
                },
                "backends": {
                    "database": connection.vendor,
                    "cache": settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1],
                    "routing_provider": settings.OSRM_BASE_URL,
                },
                "vehicle_defaults": {
                    "max_range_miles": settings.FUEL_MAX_RANGE_MILES,
                    "mpg": settings.FUEL_MPG,
                    "tank_gallons": tank_gallons(settings.FUEL_MAX_RANGE_MILES, settings.FUEL_MPG),
                    "corridor_miles": settings.CORRIDOR_MILES,
                },
            }
        )


class StationListView(ListAPIView):
    """Browse the price file as loaded: all Stations, before the collapse."""

    serializer_class = StationSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter("state", str, description="Two-letter state code."),
            OpenApiParameter("search", str, description="Substring of the truckstop name."),
        ],
        summary="Browse Stations and their prices",
    )
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        stations = Station.objects.all()
        state = self.request.query_params.get("state")
        if state:
            stations = stations.filter(state__iexact=state.strip())
        search = self.request.query_params.get("search")
        if search:
            stations = stations.filter(name__icontains=search.strip())
        return stations


def route_map(request: HttpRequest, route_token: str) -> HttpResponse:
    """The Leaflet page for a Route Token."""
    serializer = RouteRequestSerializer(data=request.GET)
    recompute_with = serializer.to_route_request() if serializer.is_valid() else None
    try:
        payload = plan_for_map(route_token, recompute_with)
    except FuelRouteError as error:
        raise Http404(error.message) from error

    return render(
        request,
        "routing/map.html",
        {"payload": payload, "payload_json": json.dumps(payload)},
    )


def _error_response(code: str, message: str, status_code: int, detail: Any = None) -> Response:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if detail:
        body["error"]["detail"] = detail
    return Response(body, status=status_code)


def _code(error: FuelRouteError) -> str:
    """A stable, snake_case code for a domain error."""
    name = type(error).__name__
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


def _build_report() -> dict[str, Any]:
    """The build-time coverage report, if it was committed."""
    path = settings.BUILD_REPORT_JSON
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
