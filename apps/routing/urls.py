from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.routing import views

urlpatterns = [
    path("v1/route/", views.RouteView.as_view(), name="route"),
    path("v1/route/map/<str:route_token>/", views.route_map, name="route-map"),
    path("v1/stations/", views.StationListView.as_view(), name="station-list"),
    path("v1/health/", views.HealthView.as_view(), name="health"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
