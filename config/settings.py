"""Settings for the Fuel Route API.

Every tunable in the project lives in this file and is readable from the
environment. There are no magic numbers elsewhere in the source tree.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

env = environ.Env(
    DEBUG=(bool, True),
    SECRET_KEY=(str, "insecure-development-key-override-in-production"),
    ALLOWED_HOSTS=(list, ["*"]),
    FUEL_MAX_RANGE_MILES=(float, 500.0),
    FUEL_MPG=(float, 10.0),
    CORRIDOR_MILES=(float, 10.0),
    CORRIDOR_THINNING_MILES=(float, 1.0),
    CORRIDOR_GRID_CELL_DEGREES=(float, 0.5),
    ORIGIN_FILL_RADIUS_MILES=(float, 30.0),
    US_CONTAINMENT_RADIUS_MILES=(float, 50.0),
    COORD_ROUNDING_DECIMALS=(int, 4),
    GAZETTEER_COVERAGE_FLOOR=(float, 0.98),
    OSRM_BASE_URL=(str, "https://router.project-osrm.org"),
    OSRM_TIMEOUT_SECONDS=(float, 10.0),
    OSRM_RETRIES=(int, 1),
    ROUTE_CACHE_TTL_SECONDS=(int, 86400),
    ROUTE_TOKEN_LENGTH=(int, 16),
)
environ.Env.read_env(BASE_DIR / ".env")

# --- Django core ------------------------------------------------------------

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "apps.stations",
    "apps.routing",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": env.db_url("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

CACHES = {
    "default": env.cache_url("REDIS_URL", default="locmemcache://fuel-route", backend=None),
}

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

# --- DRF --------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fuel Route API",
    "DESCRIPTION": (
        "Given a start and finish inside the USA, returns the driving route, the "
        "cheapest legal sequence of diesel purchases for a truck with a fixed "
        "range, and the total fuel cost."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- Domain -----------------------------------------------------------------

#: The furthest the truck can travel on a full tank. Request-overridable.
FUEL_MAX_RANGE_MILES = env("FUEL_MAX_RANGE_MILES")
#: Miles per gallon. Request-overridable. Tank capacity is Range / mpg.
FUEL_MPG = env("FUEL_MPG")
#: Half-width of the Corridor either side of the route. Request-overridable.
CORRIDOR_MILES = env("CORRIDOR_MILES")
#: Spacing of the thinned route used by the coarse Corridor pass. Speed knob.
CORRIDOR_THINNING_MILES = env("CORRIDOR_THINNING_MILES")
#: Side of the grid cell the coarse Corridor pass buckets route points into.
CORRIDOR_GRID_CELL_DEGREES = env("CORRIDOR_GRID_CELL_DEGREES")
#: How far from the start the Origin Fill may be priced.
ORIGIN_FILL_RADIUS_MILES = env("ORIGIN_FILL_RADIUS_MILES")
#: How near a US Gazetteer place an endpoint must be to count as inside the USA.
US_CONTAINMENT_RADIUS_MILES = env("US_CONTAINMENT_RADIUS_MILES")
#: Decimal places coordinates are rounded to before hashing into a Route Token.
COORD_ROUNDING_DECIMALS = env("COORD_ROUNDING_DECIMALS")
#: Build-time gate: the fraction of Stations the Gazetteer must resolve.
GAZETTEER_COVERAGE_FLOOR = env("GAZETTEER_COVERAGE_FLOOR")

OSRM_BASE_URL = env("OSRM_BASE_URL")
OSRM_TIMEOUT_SECONDS = env("OSRM_TIMEOUT_SECONDS")
OSRM_RETRIES = env("OSRM_RETRIES")

ROUTE_CACHE_TTL_SECONDS = env("ROUTE_CACHE_TTL_SECONDS")
ROUTE_TOKEN_LENGTH = env("ROUTE_TOKEN_LENGTH")

# --- Committed data files ---------------------------------------------------

SUPPLIED_PRICES_CSV = DATA_DIR / "fuel-prices-for-be-assessment.csv"
GAZETTEER_CSV = DATA_DIR / "gazetteer.csv"
CITY_OVERRIDES_CSV = DATA_DIR / "city_overrides.csv"
STATIONS_GEOCODED_CSV = DATA_DIR / "stations_geocoded.csv"
