"""Tests that hit the real routing provider.

Excluded by default (``addopts = -m 'not live'``). Run them immediately before
a demo, when the question is whether the public OSRM demo server is up:

    uv run pytest -m live
"""

import pytest

from apps.routing.exceptions import NoRouteFound
from apps.routing.geo import Point
from apps.routing.services import RouteRequest, default_provider, plan_route

pytestmark = pytest.mark.live

DALLAS = Point(32.7767, -96.7970)
CHICAGO = Point(41.8781, -87.6298)
HONOLULU = Point(21.3069, -157.8583)


def test_the_provider_answers_with_a_plausible_route():
    route = default_provider().route(DALLAS, CHICAGO)
    assert 900 < route.distance_miles < 1100
    assert 12 < route.duration_hours < 24
    assert route.shape_points > 1000
    assert route.provider_calls == 1


def test_an_island_has_no_road_route():
    with pytest.raises(NoRouteFound):
        default_provider().route(HONOLULU, DALLAS)


@pytest.mark.django_db
def test_the_whole_flow_works_against_the_real_provider():
    """The committed data, the committed Gazetteer, and the live provider."""
    from django.core.management import call_command

    from apps.stations import registry

    call_command("load_stations", verbosity=0)
    registry.reset()

    payload = plan_route(RouteRequest("Dallas, TX", "Chicago, IL", 10.0, 500.0, 10.0))
    assert payload["meta"]["external_api_calls"] == 1
    assert payload["fuel"]["feasible"] is True
    assert payload["fuel"]["total_cost_usd"] > 0
    assert payload["fuel"]["stops_count"] >= 2
    assert payload["fuel"]["naive_cost_usd"] >= payload["fuel"]["total_cost_usd"]
