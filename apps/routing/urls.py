from collections.abc import Callable
from typing import Any

from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.routing import views

# Each API route answers with and without its trailing slash. Django would
# otherwise try to redirect a slashless request to the slashed URL, which it
# cannot do for a POST without dropping the body, so `POST /api/v1/route` was a
# 500 -- and Postman, curl and most HTTP clients drop the slash freely. The
# slashed form stays the canonical one: it carries the name, so every URL the
# service generates (map_url included) keeps its slash.
_API_ROUTES: list[tuple[str, Callable[..., Any], str]] = [
    ("v1/route/", views.RouteView.as_view(), "route"),
    ("v2/route/", views.RouteV2View.as_view(), "route-v2"),
    ("v1/route/map/<str:route_token>/", views.route_map, "route-map"),
    ("v1/stations/", views.StationListView.as_view(), "station-list"),
    ("v1/health/", views.HealthView.as_view(), "health"),
    ("schema/", SpectacularAPIView.as_view(), "schema"),
]

urlpatterns = [
    *(path(route, view, name=name) for route, view, name in _API_ROUTES),
    *(path(route.rstrip("/"), view) for route, view, _ in _API_ROUTES),
    path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
