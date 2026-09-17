"""Domain errors, each mapped to one HTTP status by the view layer.

Infeasible fuelling is deliberately absent: it is an answer about the
fuelling, not an error about the route, and comes back 200 with
``fuel.feasible: false``.
"""


class FuelRouteError(Exception):
    """Base class for everything the view layer knows how to map."""

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class PlaceNotFound(FuelRouteError):
    """The Gazetteer holds no place by that name. HTTP 400."""


class OutsideUnitedStates(FuelRouteError):
    """The point resolved, but no US place lies near it. HTTP 400."""


class NoRouteFound(FuelRouteError):
    """No road connects the two endpoints. HTTP 404."""


class RoutingProviderUnavailable(FuelRouteError):
    """The routing provider could not be reached. HTTP 503."""


class MapLinkUnavailable(FuelRouteError):
    """A map link whose plan has expired and cannot be recomputed. HTTP 404.

    The Route Token is a hash, so it cannot be turned back into a request. A
    link that outlives its cache entry can only be honoured if the request
    parameters came along with it.
    """
