# Fuel Route API

Given a start and a finish inside the USA, this service returns the driving
route, the cheapest legal sequence of diesel purchases for a truck with a
500-mile range at 10 mpg, and what that fuel costs. The routing provider
decides the path; this service decides **where to stop and how many gallons to
buy at each stop** — and makes exactly **one external API call** to do it.

*Built by Meet Patel for the Spotter backend assessment, September 2026.*

---

**New York → Miami — 1,280 miles · 128.01 gallons · $377.15 · 4 stops · 1 API call · 15.2% cheaper than not planning**

---

## Quick start

Nothing to provision: SQLite file, in-process cache, no API keys, no Docker.

```bash
uv sync
uv run manage.py migrate
uv run manage.py load_stations      # 6,625 truckstops from the committed CSV, 0 network calls
uv run manage.py runserver
```

```bash
curl -s -X POST localhost:8000/api/v1/route/ -H 'Content-Type: application/json' -d '{"start":"New York, NY","finish":"Miami, FL"}'
```

One line, no dependencies beyond `curl`. The `fuel` block of the reply:

```json
{
  "feasible": true,
  "infeasible_stretch": null,
  "total_gallons": 128.01,
  "total_cost_usd": 377.15,
  "average_price_per_gallon": 2.946,
  "stops_count": 4,
  "naive_cost_usd": 444.82,
  "savings_usd": 67.67,
  "savings_percent": 15.21
}
```

The full reply also carries the stops, the echoed request, the vehicle, `meta`
and the route geometry. If you have `jq`, append `| jq .fuel` to see just the
block above, or `| jq .meta` for the call count and timings — the first request
for a route reports `"external_api_calls": 1`, and a repeat reports `0`.


Swagger at <http://127.0.0.1:8000/api/docs/>. Every response carries
`meta.map_url` — open it for a Leaflet page with the route drawn and each stop
pinned in order, its price a click away.

Without `uv`: `pip install -r requirements.txt`, then the same three
`python manage.py` commands.

---

## What the brief asked for

| Requirement | Where it lives |
|---|---|
| Start and finish, both inside the USA | `POST /api/v1/route/`; containment tested against the gazetteer in `resolver.py`, non-US rejected with 400 |
| Return a map of the route | GeoJSON in the response, plus the Leaflet page at `meta.map_url` (`views.route_map`) |
| Optimal, cost-effective fuel stops | `optimizer.py` — greedy minimum-cost refuelling, checked against a scipy linear program |
| 500-mile range, several fuel-ups | Hard constraint in `optimizer.py`; stretches it cannot cover are named, not hidden |
| Total money spent at 10 mpg | `fuel.total_cost_usd` |
| Use the supplied price file | `cleaning.py` → `geocoding.py` → `load_stations`; 8,151 rows in, 6,625 truckstops loaded |
| Free routing API | OSRM public demo, no key (`providers.py`) |
| Latest stable Django | 6.1.1 on Python 3.13 |
| Fast responses | Under 2 s cold, ~1 ms warm — see [Performance](#performance) |
| One external call ideal | **One.** Asserted by `tests/test_services.py::TestCallBudget` |

---

## The problem the CSV poses

The supplied file has 8,151 price rows and **no coordinates**. Its `Address`
column holds highway-exit descriptors — `"I-44, EXIT 283 & US-69"` — not street
addresses. Nothing can be matched to a route until every truckstop has a
position, so this is the first problem, not a detail.

Geocoding 8,000 addresses through a paid API would need a key, cost money, and
leave a fresh clone unable to run. So placement happens **offline, at build
time**, against a committed gazetteer assembled from three public-domain
sources:

| Source | Entries used | Why it is needed |
|---|---|---|
| US Census — Places | 33,140 | Incorporated places and CDPs |
| US Census — County Subdivisions | 37,447 | The Northeast names cities after townships ("Mahwah, NJ") |
| USGS GNIS — populated places | 184,987 | The unincorporated communities truckstops actually sit in ("Breezewood, PA") |

182,946 place keys after deduplication, 8.2 MB, committed to the repo.

Matching is the part the data actually forced. Strip exactly **one** Census
suffix ("Oklahoma City city" is Oklahoma City, not Oklahoma — stripping
repeatedly cost 4 points of coverage). Strip `(balance)` first. Fold accents
before dropping punctuation, because the file writes "Canon City" for "Cañon
City". Expand `St`, `Mt`, `Ft` as whole words only, so "St. Cloud" reaches
"Saint Cloud" while Stockton stays Stockton.

Every coordinate is checked against a US bounding box on the way in, which
drops **5,603 GNIS entries carrying `0.0, 0.0`** for an unmapped feature. Not
theoretical: "Ellenwood, GA" is one of them, and without the filter three
Atlanta truckstops were being placed in the Gulf of Guinea.

**Result: 6,625 of 6,626 truckstops placed — 99.98%.** Seven cities are
hand-corrected in [`data/city_overrides.csv`](data/city_overrides.csv), each
row citing the GNIS feature its coordinate came from. The last one, Etters, PA,
is left **excluded rather than given an invented coordinate**, and
`/api/v1/health/` publishes that. Coverage is measured per state, because a
miss among Texas's 776 is noise and a miss among Nevada's 71 can flip a real
route to infeasible.

One more thing to know before reading any cost figure: **California has eight
truckstops** in this file, Oregon 29, Alaska and Hawaii none. The West Coast is
effectively unserved by the data, which is why Los Angeles → Phoenix comes back
honestly infeasible.

---

## How a request is served

```
POST /api/v1/route/
  ├─ 1. Resolve both endpoints      committed gazetteer      0 calls
  ├─ 2. Check the plan cache        LocMem or Redis          0 calls
  ├─ 3. Check the road cache        LocMem or Redis          0 calls
  ├─ 4. Fetch the route             OSRM                     1 call   ← the only egress
  ├─ 5. Match the corridor          in-memory candidates     0 calls
  ├─ 6. Choose the stops            greedy optimizer         0 calls
  ├─ 7. Price the naive driver      same route, no prices    0 calls
  └─ 8. Serialise, cache, respond
```

### Corridor matching, and thinning

OSRM returns the road as a huge list of points — 9,144 for Dallas → Chicago,
35,523 for Seattle → Miami. Testing 3,807 candidates against every one is
hundreds of millions of operations, so it runs in two passes:

- **Coarse.** Thin the route to one point per mile, bucket those into a 0.5°
  grid, and reject most candidates with flat-earth arithmetic. The ring radius
  is computed rather than fixed, because `max_detour_miles` goes to 50.
- **Exact.** Project the survivors onto the *original, unthinned* segments,
  which measures the detour properly and yields the mile marker for free.

Because survivors are always re-measured against the original road, **thinning
is a speed knob that cannot change the answer** — and the benchmark asserts
exactly that. One mile was chosen by measurement, not taste:

| Spacing | Time | Stops found |
|---|---|---|
| 0.25 mi | 36.9 ms | 85 |
| **1 mi** | **27.1 ms** | **85** |
| 5 mi | 37.8 ms | 85 |
| 25 mi | 141.1 ms | 85 |

Too fine and there are too many points to bucket; too coarse and the coarse
pass stops rejecting anything, so the exact pass does all the work. Re-checked
across three routes and two corridor widths, anything from 0.75 to 2 miles
lands within a few percent, so 1 mile is a safe middle rather than a lucky
number.

### The optimizer

At each candidate in mile order: if a cheaper one is reachable within range,
buy just enough to reach it; otherwise fill the tank. Purchases always net off
fuel already aboard. The truck departs empty, so an **origin fill** is
synthesised at mile 0 at the cheapest truckstop within 30 miles of the start —
without it, nothing is reachable and every route would be infeasible.

This is provably the cheapest plan, and the claim is tested rather than
asserted: total cost is compared against a scipy linear program over 86
adversarial layouts — strictly rising prices, strictly falling, runs of
identical prices, gaps exactly equal to range, and gaps a mile either side of
it.

`naive_cost_usd` is not an average of corridor prices. It is a **simulated
driver** over the same route under the same range limit who runs the tank down
and fills at whatever truckstop is nearest, ignoring price. Its last purchase
is trimmed so both drivers buy identical total gallons — the difference is
purely *where* the fuel was bought, which is the only thing this service
controls. That trim makes the reported saving conservative.

---

## API

`start` and `finish` each accept `"City, ST"`, `"City, State"`, a bare city
name (the largest match in the country), or a `"lat,lon"` pair.

```json
{ "start": "Dallas, TX", "finish": "Chicago, IL" }
```

| Parameter | Default | Bounds | Why bounded |
|---|---|---|---|
| `mpg` | 10.0 | 1–20 | `mpg=0` is a division by zero |
| `max_range_miles` | 500.0 | 50–1000 | Below ~50 miles nothing is reachable |
| `max_detour_miles` | 10.0 | 0–50 | At 500 every truckstop in the country matches |

`tank_gallons` is derived per request as range ÷ mpg, never stored — a stored
constant would report 50 gallons while the two fields beside it said otherwise.

The response puts the answer first and the geometry last, because the geometry
runs to thousands of points:

```jsonc
{
  "fuel":       { "feasible": true, "total_cost_usd": 377.15, "savings_usd": 67.67, … },
  "fuel_stops": [ { "sequence": 1, "name": …, "mile_marker": 0.0, "gallons": …, "price_per_gallon": … } ],
  "request":    { "start": {…}, "finish": {…} },
  "vehicle":    { "max_range_miles": 500.0, "mpg": 10.0, "tank_gallons": 50.0 },
  "meta":       { "external_api_calls": 1, "cached": false, "provider_ms": 847.0, "compute_ms": 912.4, … },
  "route":      { "total_distance_miles": …, "geometry": { "type": "LineString", … } }
}
```

| Endpoint | What it does |
|---|---|
| `POST /api/v1/route/` | Plan a route and its fuel stops. `GET` with query parameters does the same |
| `POST /api/v2/route/` | The identical plan from a monotonic stack and a sparse table |
| `GET /api/v1/route/map/<route_token>/` | Leaflet page: route, numbered pins, price popups |
| `GET /api/v1/stations/` | Browse the price file as loaded. Filter by `state`, search by `search` |
| `GET /api/v1/health/` | Liveness, counts, the excluded count, per-state coverage, backends |
| `GET /api/docs/` | Swagger UI, including the parameter bounds |

| Status | When |
|---|---|
| 400 | Place not found, outside the USA, malformed, or a parameter out of range |
| 404 | No road connects the two points (Honolulu → Dallas) |
| 503 | Routing provider unreachable |

**Infeasible fuelling is not an error.** It is a 200 with `feasible: false` and
a named stretch, because it is an answer about the fuelling, not a failure of
the route. Los Angeles → Phoenix returns one: no California truckstop in the
file is within 30 miles of Los Angeles, so the service says so instead of
inventing a price.

---

## Performance

Cold, against the live demo server. The routing call is most of it; everything
this project controls is the remainder.

| Route | Miles | Cold | Stops | Fuel cost | Naive | Saving |
|---|---|---|---|---|---|---|
| Atlanta → Nashville | 248 | 0.96 s | 3 | $70.02 | $71.22 | 1.7% |
| Denver → Kansas City | 599 | 0.87 s | 5 | $182.21 | $189.50 | 3.9% |
| Dallas → Chicago | 961 | 1.06 s | 5 | $272.22 | $276.04 | 1.4% |
| New York → Miami | 1,280 | 1.13 s | 4 | $377.15 | $444.82 | 15.2% |
| Portland ME → San Diego | 3,130 | 2.06 s | 21 | $961.46 | $1,109.67 | 13.4% |

**93% of a cold request is the network call**, so that is the only thing worth
removing. Two caches do it. The plan cache is keyed on the endpoints *and* the
vehicle; the road cache is keyed on the endpoints alone, because the road does
not depend on the truck driving it:

| Second request for a pair already fetched | Time | External calls |
|---|---|---|
| Same vehicle — plan cache hit | ~1 ms | 0 |
| Different `mpg`, range or detour — road cache hit | ~99 ms | 0 |
| The other optimizer (`/api/v2/`) — road cache hit | ~99 ms | 0 |

Every response reports both `meta.compute_ms` (what *this* caller waited for)
and `meta.provider_ms` (how much of that was the provider, retries included),
so a slow answer can be attributed rather than guessed at. `provider_ms: 0.0`
with `external_api_calls: 0` means the road came from cache.

Savings vary honestly with the corridor. Where truckstops are dense and prices
tight, a driver who simply stops when low does nearly as well; where prices
spread across regions, the optimizer earns 13–15%. Reporting 1.4% where it is
1.4% is the point of measuring against a simulated driver rather than a claim.

---

## Assumptions

**These can move the cost you see:**

1. **The truck departs empty and pays for every gallon it burns**, so total
   gallons are always distance ÷ mpg and a 240-mile trip reports $84, not $0.
2. **It buys at the origin fill before it can move** — the cheapest truckstop
   within 30 miles of the start, at mile 0. That radius is deliberately
   separate from `max_detour_miles`: the corridor is about detouring off a
   route, the origin fill about reaching the first purchase at all.
3. **Repeated price rows are averaged.** 568 OPIS IDs appear more than once
   with no date column, so recency is unknowable and the mean is the only
   defensible summary; the minimum would quote a total the trip cannot achieve.
   All 568 share one Rack ID, so they are not rack-market variants.
   `price_min`, `price_max` and `price_sample_count` stay visible.
4. **Canadian rows are dropped** — 620 of them; the brief scopes to the USA.
5. **Truckstops the gazetteer cannot place are excluded and counted.**
   Exactly one is.

**These affect precision, not the answer:** coordinates are city centroids
(1–3 miles against a 10-mile corridor, and co-located truckstops collapse to
the cheapest anyway); detour is reported but not added to fuel burn (well under
1%); the OSRM demo serves a **car profile**, so the path is not guaranteed
truck-legal; prices are diesel retail as supplied, mean $3.417.

---

## Tests

```bash
uv run pytest                 # 388 tests, no network
uv run pytest -m live         # 3 more, against the real OSRM
uv run ruff check . && uv run mypy apps config
```

One test file per source module, so a failure names the component that broke.
The ones worth knowing about:

| What | How |
|---|---|
| Optimality | Greedy total cost vs a scipy linear program, 86 adversarial layouts |
| Corridor | Two-pass matcher vs brute force over 9,144 real shape points, at 3 widths |
| Call budget | Exactly one external call uncached, zero cached |
| No network at load | `load_stations` runs with `socket.socket` patched to raise |
| Distance arithmetic | Haversine against published figures, not against itself |
| Cache policy | Identical payload on repeat, infeasible cached, 503 never cached |
| Road cache | Reused across vehicles and both optimizers, namespaced per provider |
| v1 vs v2 | Identical payloads apart from `meta.optimizer`, on 386 layouts |
| Geocoding | Overrides beat the gazetteer; unresolved excluded *and counted* |

---

## Regenerating the data

Both commands are build-time only, and are the only code in the project allowed
to touch the network. `load_stations` is forbidden from it, and a test proves
that by patching `socket.socket` to raise.

```bash
uv run manage.py build_gazetteer      # downloads 3 public-domain sources → data/gazetteer.csv
uv run manage.py build_station_data   # cleans, geocodes → committed CSV + build_report.json
```

Keeping them separate from request serving is what holds the per-request call
count at one. Offline benchmarks, reproducible with no network:

```bash
uv run python benchmarks/corridor_spacing.py    # the thinning sweep above
uv run python benchmarks/optimizer_scaling.py   # v1 vs v2
```

---

## Layout

```
config/settings.py        every tunable, read from env
apps/stations/
  cleaning.py             drop Canada, collapse duplicates, average prices
  geocoding.py            the offline gazetteer and its matching rules
  registry.py             in-memory candidates, collapsed by coordinate, lazy
  models.py               Station, price spread kept visible
apps/routing/
  geo.py                  haversine, planar approximation
  corridor.py             thinning, grid buckets, exact segment projection
  optimizer.py            minimum-cost refuelling, feasibility, naive driver
  optimizer_v2.py         the same plan from a monotonic stack + sparse table
  providers.py            OSRM: timeout, retry, geometry, the road cache
  resolver.py             offline endpoint resolution, US containment
  services.py             orchestration + the plan cache; the only module that
                          knows the whole request flow
  serializers.py          validation, bounds, published response shape
  views.py                HTTP surface only
benchmarks/               offline sweeps over committed geometry
data/                     supplied CSV, gazetteer, geocoded output, corrections
tests/                    one file per module above
```

`geo.py`, `corridor.py` and both optimizers import nothing from Django. They
take plain data and return plain data, which is what lets the linear-program
check and the brute-force comparison run as ordinary unit tests with no
database, no network and no settings.

---

## Choices worth defending

**Greedy over dynamic programming.** The exchange argument makes it provably
optimal for this problem, it is O(n) and readable, and a scipy linear program
checks it on every adversarial layout I could construct. A DP would be slower,
longer, and no more correct.

**An offline gazetteer over a geocoding API.** 8,000 addresses would mean 8,000
calls, a key and a bill — and a fresh clone that cannot run. Committing 8.2 MB
of public-domain data instead holds the per-request budget at one call and
makes the repo work with no signup.

**Caching the road separately from the plan.** The plan depends on the truck;
the road does not. Keying them separately means changing `mpg` costs ~99 ms
instead of a full ~945 ms routing call for a road that is byte-identical.

**Excluding one truckstop rather than guessing its coordinate.** An invented
position can manufacture a range-exceeding gap and report a drivable route as
infeasible. Excluding it and publishing the count at `/api/v1/health/` is
honest; a plausible-looking guess is not.

**Keeping `/api/v2/` even though it is not faster.** The indexed optimizer is
3–4× quicker on adversarial price profiles and ~5× *slower* on realistic ones,
where a real corridor of 86–142 candidates finishes in under a millisecond
either way. It is documented as not helping rather than quietly deleted,
because the measurement is the useful part.

Deferred work, with the reason for each deferral, is in
[`Future.md`](Future.md).

---

## Contact

**Meet Patel** — [justmeetpatel@gmail.com](mailto:justmeetpatel@gmail.com) ·
[github.com/JUSTMEETPATEL](https://github.com/JUSTMEETPATEL)
