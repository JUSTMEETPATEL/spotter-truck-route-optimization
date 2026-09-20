# Fuel Route API

Given a start and a finish inside the USA, this service returns the driving
route, the **cheapest legal sequence of diesel purchases** for a truck with a
500-mile range at 10 mpg, and what the fuel costs. A companion page draws the
result on a map.

The routing provider decides the path. This service decides **where to stop and
how many gallons to buy at each stop** — and it makes exactly **one external
API call** to do it.

```console
$ curl -s -X POST localhost:8000/api/v1/route/ \
    -H 'Content-Type: application/json' \
    -d '{"start": "New York, NY", "finish": "Miami, FL"}' | jq '.fuel, .meta.external_api_calls'
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
1
```

`naive_cost_usd` is not an average of corridor prices. It is a **simulated
driver** over the same route under the same range limit, who runs the tank down
and fills up at whatever truckstop is nearest, ignoring price. Its last
purchase is trimmed to what finishing the trip needs, so both drivers buy
exactly the same total gallons and the difference is purely *where* the fuel
was bought — which is the only thing this service controls. That trim makes the
reported saving **conservative**: a driver who really did fill the tank one
last time would pay more than the figure above.

---

## Run it

Nothing to provision: SQLite file, in-process cache, no API keys, no Docker.

```bash
uv sync
uv run manage.py migrate
uv run manage.py load_stations      # 6,625 Stations from the committed CSV, 0 network calls
uv run manage.py runserver
```

Then open <http://127.0.0.1:8000/api/docs/> for Swagger, or import
[`postman/fuel-route-api.postman_collection.json`](postman/fuel-route-api.postman_collection.json)
— 17 requests covering the happy path, the two "infeasible but correct" cases,
and every error code.

Without `uv`:

```bash
pip install -r requirements.txt            # the app: 20 packages, no scipy
pip install -r requirements-dev.txt        # adds the test suite: scipy + numpy, ~41 MB
python manage.py migrate && python manage.py load_stations && python manage.py runserver
```

| Endpoint | What it does |
|---|---|
| `POST /api/v1/route/` | Plan a route and its fuel stops. `GET` with query parameters does the same, for a browser |
| `POST /api/v2/route/` | The identical plan, computed with a monotonic stack and a sparse table instead of two linear scans. See [Two optimizers](#two-optimizers-apiv1route-and-apiv2route) |
| `GET /api/v1/route/map/<route_token>/` | Leaflet page: the route, numbered stop pins, price popups |
| `GET /api/v1/stations/` | Browse the price file as loaded. Filter by `state`, search by `search` |
| `GET /api/v1/health/` | Liveness, station counts, **the excluded count**, per-state coverage, active backends |
| `GET /api/docs/` | Swagger UI, including the parameter bounds |

### Request

```json
{ "start": "Dallas, TX", "finish": "Chicago, IL" }
```

`start` and `finish` each accept `"City, ST"`, `"City, State"`, a bare city name
(the largest match in the country), or a `"lat,lon"` pair. Three optional
parameters, all bounded by the serializer and published in Swagger:

| Parameter | Default | Bounds | Why bounded |
|---|---|---|---|
| `mpg` | 10.0 | 1–20 | `mpg=0` is a division by zero |
| `max_range_miles` | 500.0 | 50–1000 | Below ~50 miles nothing is reachable |
| `max_detour_miles` | 10.0 | 0–50 | At 500 every truckstop in the country matches, which would disprove the performance figures below |

`tank_gallons` is **derived per request** as range ÷ mpg, never stored. A stored
constant would report 50 gallons while its two neighbouring response fields
said otherwise.

### Errors

| Status | When |
|---|---|
| 400 | Place not found, outside the USA, malformed request, or a parameter out of range |
| 404 | No road connects the two points (Honolulu → Dallas) |
| 503 | Routing provider unreachable |

**Infeasible fuelling is not an error.** It is a 200 with `feasible: false` and
a named stretch, because it is an answer about the fuelling, not a failure of
the route:

```json
{
  "feasible": false,
  "infeasible_stretch": {
    "from": "start (mile 0.0)",
    "to": "Coachella, CA (mile 143.5)",
    "gap_miles": 143.5
  },
  "total_gallons": 39.08,
  "total_cost_usd": null,
  "stops_count": 0
}
```

That is Los Angeles → Phoenix. The supplied file contains **eight California
truckstops** and none is within 30 miles of Los Angeles, so the service says so
instead of inventing a price. Phoenix → Los Angeles, on the other hand, is
feasible in one stop — the asymmetry is real, and it is in the data.

---

## Assumptions

Split deliberately. The first group can move the cost figure you read; the
second cannot.

### These affect the cost you see

1. **The truck departs empty and pays for every gallon it burns.** Total
   gallons are always distance ÷ mpg, so a 240-mile trip reports $84, not
   $0.00.
2. **Before it can move it buys at the Origin Fill** — the cheapest truckstop
   within `ORIGIN_FILL_RADIUS_MILES` (30) of the start, treated as a stop at
   mile 0. It buys *just enough* there, like anywhere else. This radius is
   deliberately separate from `max_detour_miles`: the corridor is about
   detouring off a route, while the origin fill is about reaching the first
   purchase at all, on an empty tank. A stop at mile 0 may therefore sit
   further off-route than `max_detour_miles`, and its
   `detour_miles_to_city` says so. If that same truckstop also lies in the
   corridor further along, its later position is dropped: it is one truckstop,
   and mile 0 is the only place an empty tank can reach it.
3. **Repeated price rows for one truckstop are averaged.** 568 OPIS IDs appear
   more than once. The file has no date column, so recency is unknowable and
   the mean is the only defensible summary; the minimum would quote a total the
   trip cannot achieve. `price_min`, `price_max` and `price_sample_count` stay
   visible on every station. Evidence for reading repeats as observations over
   time: all 568 share a single Rack ID, so they are not rack-market variants.
4. **Canadian rows are dropped** — 620 of them, the brief scopes to the USA.
   7,531 US rows remain, collapsing to 6,626 distinct truckstops.
5. **Truckstops the Gazetteer cannot place are excluded, and the count is
   published** at `/api/v1/health/`. Exactly one is: Etters, PA. See
   [Coverage](#coverage).

### These affect precision, not the answer

6. **Station coordinates are city centroids.** The CSV has no coordinates and
   its `Address` column holds highway-exit descriptors ("I-44, EXIT 283 &
   US-69"), not street addresses. The error is 1–3 miles against a 10-mile
   corridor and a 500-mile range, and it cannot change *which* stop is chosen:
   co-located truckstops collapse into one candidate resolved on price alone.
7. **Detour is reported but not added to fuel burn.** Stops sit at interstate
   exits; the error stays well under 1%. It is a property of the city, not of
   an individual forecourt, and the field name (`detour_miles_to_city`) says
   so.
8. **The routing provider serves a car profile.** The public OSRM demo has no
   HGV weight, height or length restrictions, so the returned path is not
   guaranteed truck-legal. A real deployment self-hosts OSRM with an HGV
   profile; the optimizer is unchanged, only the geometry improves.
9. **Prices are diesel retail per gallon**, as supplied. Mean $3.417, range
   $2.687–$6.399.
10. **Tank capacity is derived, never configured.** It is range ÷ mpg — 50
    gallons at the defaults — computed wherever it is shown. There is
    deliberately no `TANK_GALLONS` setting, because both of its inputs are
    request-overridable and a stored constant would report 50 while the two
    response fields beside it said otherwise.

---

## How it works

Everything expensive happens before a request arrives.

### Phase A — build time, run once, output committed

```
fuel-prices-for-be-assessment.csv  (8,151 rows)
  ├─ drop 620 Canadian rows                            → 7,531
  ├─ collapse duplicate OPIS IDs, average the prices   → 6,626 Stations
  ├─ resolve city+state against the committed Gazetteer
  ├─ apply data/city_overrides.csv to the remainder
  └─ write data/stations_geocoded.csv + data/build_report.json   (both committed)
```

```bash
uv run manage.py build_gazetteer      # downloads 3 public-domain sources → data/gazetteer.csv
uv run manage.py build_station_data   # cleans, geocodes, writes the committed CSV + report
```

Those two commands are the only code in the project allowed to touch the
network at build time. `load_stations` is forbidden from it — there is a test
that patches `socket.socket` to prove it. **Keeping them separate is what holds
the per-request call count at one.**

Loading also builds the candidate registry: stations are grouped by coordinate
and each group collapses to the cheapest one, carrying a count of how many it
stands for. **6,625 stations occupy 3,807 distinct coordinates**, so the
corridor scan runs against 3,807 entries rather than 6,625. The collapse is
valid at build time because co-located stations share a mile marker and a
detour on *every* route, so only the cheapest can ever be worth choosing. All
6,625 stay in the database for `/api/v1/stations/`.

### Phase B — per request

```
POST /api/v1/route/
  ├─ 1. Resolve both endpoints      committed Gazetteer      0 calls
  ├─ 2. Check the plan cache        LocMem or Redis          0 calls
  ├─ 3. Check the road cache        LocMem or Redis          0 calls
  ├─ 4. Fetch the route             OSRM                     1 call   ← the only egress
  ├─ 5. Match the corridor          in-memory candidates     0 calls
  ├─ 6. Choose the stops            greedy optimizer         0 calls
  ├─ 7. Price the Naive Driver      same route, no prices    0 calls
  └─ 8. Serialise, cache, respond
```

A test asserts the call count is exactly one for an uncached request and zero
for a cached one. It is also what keeps this project inside the OSRM demo
server's usage policy.

### The optimizer

Classic minimum-cost refuelling over the candidates in the corridor, ordered by
mile marker, with the Origin Fill prepended at mile 0. At each candidate the
plan visits:

```
cheaper candidate within range?  → buy just enough to reach the nearest one
else destination within range?   → buy just enough to finish
else                             → fill the tank, drive to the cheapest in range
```

"Just enough" is always net of the fuel already in the tank. Buying a full leg
while holding 8 gallons inflates every total.

**The order of those three tests matters.** Checking the destination first —
which is how I first wrote it down — buys the whole remaining trip at the
current price whenever the finish is within range, even with cheaper fuel on
the way. Three pumps at $4.00, $3.00 and $2.00 over a 300-mile trip cost $90
hand-to-mouth and $120 bought outright at the origin. A test caught it.

**Why greedy and not dynamic programming.** A gallon does the same work
wherever it was bought, so a cheap purchase can never become a liability later.
The exchange argument: any plan that buys surplus at a high price while a
cheaper pump is reachable improves by moving a gallon from the expensive
purchase to the cheap one, and repeating that transformation yields exactly the
greedy plan. DP would cost more to run and return the same answer. Recorded as
an architecture decision, because choosing greedy deliberately reads very
differently from defaulting to it.

**Verified, not asserted.** Testing a greedy against another greedy proves
nothing: if the reasoning is wrong, both are wrong identically. So the test
suite restates the problem as a linear program — minimise Σ pᵢgᵢ subject to the
tank level after every leg staying within [0, tank capacity], starting empty —
solves it with scipy, and compares **total cost only** at relative tolerance
1e-6 across **86 adversarial layouts**: prices strictly rising, strictly
falling, runs of identical prices, a gap of exactly 500 miles, gaps at 499 and
501, and a single-candidate route. Ties in price admit many optimal purchase
vectors, so asserting on gallons would flake against scipy's choice of pivot.

### Two optimizers: `/api/v1/route/` and `/api/v2/route/`

Same request, same response, same answer to the cent. They differ in how the
greedy rule answers its two questions:

| | v1 `scan` | v2 `monotonic-stack+sparse-table` |
|---|---|---|
| Cheaper candidate ahead? | walk forward until one is found | **monotonic stack**, one right-to-left pass, O(n) for all candidates |
| Cheapest within range? | minimum over the reachable slice | **sparse table**, O(1) per query after an O(n log n) build, built lazily on first use |

`meta.optimizer` says which one answered. A test asserts the two endpoints
return byte-identical payloads apart from that field, and the indexed plan is
checked against the same scipy linear program.

**It is not faster on a real request, and the measurements say why.** 93% of a
request is the routing call (859 ms of 922 ms before the polyline change), and
a real corridor holds 86–142 candidates, where both optimizers finish in under
a millisecond. `uv run python benchmarks/optimizer_scaling.py`, offline:

| Prices | Candidates | v1 scan | v2 indexed | Speed-up |
|---|---|---|---|---|
| random | 25,600 | 6.6 ms | 45.6 ms | **0.1×** |
| rising | 25,600 | 194.9 ms | 65.6 ms | **3.0×** |
| falling | 25,600 | 81.9 ms | 20.1 ms | **4.1×** |

The price profile decides the winner. Rising prices are the scan's worst case:
nothing ahead is ever cheaper, so every step walks the whole reachable window
and the walk visits every candidate — that's where precomputation repays
itself. On random prices, which is what a real corridor looks like, the walk
takes few steps and the O(n log n) build is never repaid, so the scan wins by
5×. That is why **v1 remains the default** and v2 is offered alongside it
rather than replacing it.

**Feasibility** runs over the whole sequence `[start, candidates…,
destination]`, including the final leg to the destination. The first gap is
special: the truck departs empty, so it cannot cover *any* distance before its
first purchase, which makes an empty candidate list on a non-zero route
infeasible rather than free.

### Corridor matching

The provider returns tens of thousands of shape points (9,144 for
Dallas → Chicago; 26,159 for Seattle → Anchorage). Comparing 3,807 candidates
against all of them is hundreds of millions of operations, so there are two
passes:

- **Coarse.** Resample the route to one point per mile, bucket the samples into
  0.5° grid cells, and test each candidate against its own cell and the
  surrounding ring with flat-earth arithmetic. The ring radius is *computed*
  rather than fixed at one cell, because `max_detour_miles` goes to 50 and
  longitude cells narrow towards the poles. This rejects the large majority of
  candidates.
- **Exact.** For survivors, project onto the original geometry's line segments
  — segment projection, not nearest-shape-point, which handles a truckstop
  lying between two distant points and yields the mile marker from the
  along-segment fraction for free. The coarse pass also reports *where along
  the route* each survivor is near, so this measures a few dozen segments
  instead of all 9,143.

"Exact" means exact against the route geometry, not against the real forecourt,
which is a city centroid (assumption 6). A test asserts the two-pass matcher
agrees with a brute-force reference — every candidate against every segment —
at 0.5, 10 and 50 mile corridors.

### Caching

Two layers, because they answer two different questions.

**The plan cache** is keyed on the **route token**: a truncated SHA-256 of the
resolved endpoints *and the vehicle parameters*, with coordinates rounded to 4
decimal places (~11 m) so that `"Dallas"`, `"dallas, tx"` and
`"32.7767,-96.7970"` collapse onto one entry. The same request therefore always
yields the same map URL, with no token store to maintain and no link that dies
on a process restart.

**The road cache** sits one layer down, inside the provider, and is keyed on
the **rounded endpoints alone**. The vehicle belongs in the plan key because
the plan depends on it; the road does not. Change `mpg` from 10 to 8 and the
road between the same two places is byte-identical, yet the plan key changes —
so before this layer existed, that request paid the full routing call again.
That call is **93% of an uncached request**, so it is the only part worth
removing:

| Second request for a pair already fetched | Before | After |
|---|---|---|
| Same vehicle (plan cache hit) | ~1 ms | ~1 ms |
| Different `mpg`, range or detour | ~945 ms, 1 call | **~99 ms, 0 calls** |
| The other optimizer (`/api/v2/`) | ~945 ms, 1 call | **~99 ms, 0 calls** |

Measured across Atlanta → Denver, Phoenix → Seattle and Boston → Miami, warm
process, median. The remaining ~99 ms is corridor matching and serialising,
which genuinely *do* depend on the parameters and so must re-run.

The road cache stores the **encoded polyline**, not the decoded points: the
same geometry in 33 KB of string rather than ~9,000 pickled `Point`s, and about
a millisecond to decode again. Its key carries the provider's base URL, so a
self-hosted HGV profile never serves a route cached from the public car
profile.

Successes cache for 24 hours in both layers. **Infeasible results cache too** —
deterministic and expensive to recompute. **Failures never cache** in either
layer: not a 503, or one provider wobble poisons a route for a day; and not a
`NoRoute`, which the provider may answer differently once it is healthy. The
map page recomputes through the same service when its cache entry has expired
and the request parameters are on the query string, so a shared link costs one
routing call rather than 404-ing in the middle of a demo.

Both layers are LocMem by default and Redis when `REDIS_URL` is set. LocMem
dies with the process, so a restart pays full price for the first request to
each pair again; Redis is what makes the saving survive a deploy.

---

## The data

### Coverage

The supplied file has no coordinates, so every truckstop is placed by matching
its city and state against a **committed offline gazetteer** built from three
public-domain sources:

| Source | Entries used | Why it is needed |
|---|---|---|
| US Census Gazetteer — Places | 33,140 | Incorporated places and CDPs |
| US Census Gazetteer — County Subdivisions | 37,447 | The Northeast names its cities after townships ("Mahwah, NJ"), which Places omits |
| USGS GNIS — populated places | 184,987 | The unincorporated communities truckstops actually sit in ("Ruther Glen, VA", "Breezewood, PA") |

182,946 distinct place keys after deduplication, 8.2 MB, committed. Each source
indexes below the one above it, so a named Census place always wins a contested
name.

Every entry is checked against a crude US bounding box on the way in, and
**5,603 GNIS populated places are dropped because they carry 0.0, 0.0** for an
unmapped feature. That filter is not theoretical: "Ellenwood, GA" is one of
them, and without it three Atlanta truckstops were being placed in the Gulf of
Guinea. The same check runs again when stations are placed, and the count of
anything it rejects is published at `/api/v1/health/` as
`stations_out_of_us_bounds`. The box is a garbage filter, not a containment
test — Mexico City is inside it — which is why deciding whether an *endpoint*
is in the USA is a separate, finer test.

Matching is the part the data actually forced:

- Strip exactly **one** Census suffix. "Oklahoma City city" is Oklahoma City,
  not Oklahoma — stripping repeatedly cost 4 points of coverage on its own.
- Strip `(balance)` first, so "Indianapolis city (balance)" resolves.
- Fold accents before dropping punctuation: the file writes "Canon City" for
  "Cañon City".
- Expand `St`, `Ste`, `Mt`, `Ft`, `N`, `S`, `E`, `W` as **whole words only**,
  so "St. Cloud" matches "Saint Cloud" while Stockton stays Stockton.
- Index consolidated city-counties under their city half:
  "Nashville-Davidson metropolitan government (balance)" is also Nashville.
- Try the query with a legal suffix stripped too: the file writes "Monroe
  Township, NJ" for the subdivision the Census calls "Monroe township".
- Keep a space-squashed index, so "Mc Calla" reaches "McCalla".

**Result: 6,625 of 6,626 truckstops placed — 99.98%.** Seven cities absent from
all three sources, or misplaced by them, are hand-corrected in
[`data/city_overrides.csv`](data/city_overrides.csv) — 12 truckstops between
them, every row citing the GNIS feature its coordinate came from. The last one,
**Etters, PA (1 truckstop)**, is left excluded rather than given an invented
coordinate, and `/api/v1/health/` publishes that.

This matters more than a percentage suggests: an excluded truckstop can
manufacture a range-exceeding gap and report a perfectly drivable route as
infeasible. Coverage is therefore measured **per state** and published per
state, because a miss in Texas is noise among 776 and a miss in Nevada among
71 can flip a real route.

### Where the truckstops are

48 states. The distribution is the single most important thing to know before
reading any cost figure:

| Dense | | Sparse | |
|---|---|---|---|
| TX | 776 | RI | 2 |
| IL | 329 | **CA** | **8** |
| WI | 295 | DE | 15 |
| GA | 284 | VT | 16 |
| OH | 262 | NH | 23 |
| MO | 249 | CT | 25 |
| IA | 220 | OR | 29 |
| PA | 216 | ME | 30 |
| IN | 212 | WV | 38 |
| VA | 198 | MT | 44 |

**California has eight**, Oregon 29, Washington 52, and Alaska and Hawaii none
at all. The West Coast is effectively unserved by this dataset. That is why
Los Angeles → Phoenix comes back infeasible: it is the correct answer to a
question the data cannot answer, and it is a better demonstration than a
hardcoded rejection would be.

---

## Performance

Measured on the committed dataset, warm process, real OSRM calls included.
The routing call is 600–800 ms of it; everything this project controls is the
remainder.

| Route | Miles | Cold | Stops | Fuel cost | Naive | Saving |
|---|---|---|---|---|---|---|
| Atlanta → Nashville | 248 | 0.96 s | 3 | $70.02 | $71.22 | 1.7% |
| Phoenix → Los Angeles | 390 | 0.70 s | 1 | $114.02 | $114.02 | 0.0% |
| Denver → Kansas City | 599 | 0.87 s | 5 | $182.21 | $189.50 | 3.9% |
| Dallas → Chicago | 961 | 1.06 s | 5 | $272.22 | $276.04 | 1.4% |
| New York → Miami | 1,280 | 1.13 s | 4 | $377.15 | $444.82 | 15.2% |
| Portland ME → San Diego | 3,130 | 2.06 s | 21 | $961.46 | $1,109.67 | 13.4% |

A cached repeat of any of them is **under 2 ms and zero external calls**, and
re-running one with a different truck is **~99 ms and zero external calls** —
see the road cache above. Every response carries `meta.compute_ms`, which is
what *that* caller waited for — the cached path reports its own figure rather
than replaying the first caller's — so none of the above has to be taken on
trust.

Beside it, **`meta.provider_ms` is the part of that wait which was the routing
provider**, retries included. The two together say where a slow response went
without anyone having to guess:

| `provider_ms` | `external_api_calls` | What happened |
|---|---|---|
| `0.0` | `0` | Served from a cache; the rest of `compute_ms` is this service |
| ~`850` | `1` | A normal cold call to the OSRM demo server |
| ~`15000` | `2` | The demo server hung to the 10 s timeout and the retry succeeded |

That last row is not hypothetical — the public demo server has no SLA, and a
15-second response with `external_api_calls: 2` is it wobbling rather than
anything in this codebase. It is the clearest argument for the road cache, and
for self-hosting OSRM in production.

Savings vary honestly with the corridor. Where truckstops are dense and prices
tight (Dallas → Chicago), a driver who just stops when the tank runs low does
nearly as well as the optimizer. Where prices spread (New York → Miami, or any
long haul crossing several price regions), the optimizer earns 13–15%.
Reporting 1.4% where it is 1.4% is the point of measuring against a simulated
driver rather than a corridor average, which would have flattered the result.

### Corridor thinning is a speed knob, not an accuracy one

`uv run python benchmarks/corridor_spacing.py` — offline, against the committed
Dallas → Chicago geometry, so it costs no external calls and needs no network:

| Spacing (mi) | Median | Matched | Same set? |
|---|---|---|---|
| 0.25 | 36.7 ms | 85 | yes |
| 0.5 | 29.7 ms | 85 | yes |
| **1.0** | **26.7 ms** | 85 | yes |
| 2.0 | 27.5 ms | 85 | yes |
| 5.0 | 37.2 ms | 85 | yes |
| 10.0 | 56.4 ms | 85 | yes |
| 25.0 | 142.0 ms | 85 | yes |

The matched set is identical at every spacing, because survivors of the coarse
pass are re-measured against the original geometry regardless. Coarser spacing
is slower, not less accurate: it widens the slack radius, so more candidates
survive to the exact pass. 1.0 mile is the default. Narrowing the exact pass to
a neighbourhood of segments took Dallas → Chicago from 646 ms to 27 ms.

---

## Configuration

Every tunable lives in `config/settings.py` and is readable from the
environment. There are no magic numbers elsewhere in the source tree. See
[`.env.example`](.env.example).

| Setting | Default | Request-overridable |
|---|---|---|
| `FUEL_MAX_RANGE_MILES` | 500.0 | `max_range_miles`, 50–1000 |
| `FUEL_MPG` | 10.0 | `mpg`, 1–20 |
| `CORRIDOR_MILES` | 10.0 | `max_detour_miles`, 0–50 |
| `CORRIDOR_THINNING_MILES` | 1.0 | no |
| `CORRIDOR_GRID_CELL_DEGREES` | 0.5 | no |
| `ORIGIN_FILL_RADIUS_MILES` | 30.0 | no |
| `US_CONTAINMENT_RADIUS_MILES` | 50.0 | no |
| `COORD_ROUNDING_DECIMALS` | 4 | no |
| `GAZETTEER_COVERAGE_FLOOR` | 0.98 | build-time gate |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | no |
| `OSRM_TIMEOUT_SECONDS` | 10.0 | no |
| `OSRM_RETRIES` | 1 | no |
| `ROUTE_CACHE_TTL_SECONDS` | 86400 | no |
| `ROAD_CACHE_TTL_SECONDS` | 86400 | no |
| `ROUTE_TOKEN_LENGTH` | 16 | no |

Four more are read only by `build_gazetteer`, the one command that downloads
anything:

| Setting | Default |
|---|---|
| `GAZETTEER_PLACES_URL` | Census 2023 Gazetteer Places |
| `GAZETTEER_COUSUBS_URL` | Census 2023 Gazetteer County Subdivisions |
| `GAZETTEER_GNIS_URL` | USGS GNIS Domestic Names |
| `GAZETTEER_DOWNLOAD_TIMEOUT_SECONDS` | 300.0 |

Env-readable is not the same as request-overridable. The internal knobs —
thinning spacing, grid cell size, origin fill radius — are deliberately not
exposed to callers.

**Postgres and Redis** are one environment variable each
(`DATABASE_URL`, `REDIS_URL`); SQLite and an in-process cache are the defaults
so that a fresh clone runs.

---

## Tests

```bash
uv run pytest                 # 363 tests, no network
uv run pytest -m live         # 3 more, against the real OSRM. Run before a demo
uv run ruff check . && uv run mypy apps config
```

One test file per source module, so a failure names the component that broke.
External providers are mocked throughout; the default `pytest` run makes no
network calls at all.

The ones worth knowing about:

| What | How |
|---|---|
| Optimality | Greedy total cost vs a scipy linear program, 86 adversarial layouts |
| Corridor | Two-pass matcher vs brute force over 9,144 real shape points, at 3 corridor widths |
| Call budget | Exactly one external call uncached, zero cached |
| No network at load | `load_stations` runs with `socket.socket` patched to raise |
| Lazy registry | Importing `registry` in a subprocess with an unusable `DATABASE_URL` still succeeds, so `migrate` works on a fresh clone |
| Distance arithmetic | Haversine against published figures (LAX→JFK 2,475 mi; the equator) rather than against itself |
| Feasibility | Gaps between candidates, the final leg to the destination, and the empty-tank first gap |
| Cache policy | Identical payload on repeat, shared token across input forms, infeasible cached, 503 not cached |
| Road cache | Reused across vehicles and across both optimizers, namespaced per provider, failures never cached, geometry stored encoded |
| Bounds | Every parameter at and beyond each limit |
| v1 vs v2 | Both optimizers agree on 86 adversarial layouts, 300 random ones and every worked case; the endpoints return identical payloads apart from `meta.optimizer` |
| Geocoding | Overrides beat the Gazetteer, per-state coverage arithmetic, unresolved excluded *and counted*, and coordinates that cannot be in the USA rejected rather than used |
| Build gate | Low coverage warns by default and fails under `--strict` |
| Endpoints | Hawaii accepted as the USA, then 404 from the provider's real `NoRoute` body |

---

## Repo map

```
config/settings.py        every tunable, read from env
apps/stations/
  cleaning.py             drop Canada, collapse duplicates, average prices
  geocoding.py            the offline Gazetteer and its matching rules
  registry.py             in-memory candidates, collapsed by coordinate, lazy
  models.py               Station, price spread kept visible
  management/commands/    build_gazetteer, build_station_data (network allowed)
                          load_stations (network forbidden)
apps/routing/
  geo.py                  haversine, planar approximation, unit constants
  corridor.py             thinning, grid buckets, exact segment projection
  optimizer.py            minimum-cost refuelling, feasibility, Naive Driver
  optimizer_v2.py         the same plan from a monotonic stack and a sparse
                          table; serves /api/v2/route/
  providers.py            OSRM: timeout, retry, geometry
  resolver.py             offline endpoint resolution, US containment
  services.py             orchestration + caching; the only module that knows
                          the whole request flow
  serializers.py          validation, bounds, published response shape
  views.py                HTTP surface only
  exceptions.py           domain errors mapped to status codes
benchmarks/               offline spacing sweep over a committed geometry,
                          and the v1-vs-v2 optimizer scaling sweep
data/                     supplied CSV, committed Gazetteer, geocoded output,
                          hand corrections, build report
tests/                    one file per module above
```

`geo.py`, `corridor.py` and `optimizer.py` import nothing from Django. They
take plain data and return plain data, which is what lets the linear-program
check and the brute-force comparison run as ordinary unit tests with no
database, no network and no settings.

---

## Known limitations

Stated here rather than discovered later.

- **The OSRM demo server is a development service on a car profile.** No HGV
  weight, height, length or hazmat restrictions, and no SLA. Production
  self-hosts OSRM with an HGV profile or contracts a provider. Only the
  geometry changes; the optimizer does not.
- **Prices are a static snapshot with no timestamps.** Nothing here refreshes
  them, and averaging repeats is a reading of the file, not a fact about it.
- **City-centroid coordinates.** Good to 1–3 miles, which the corridor and
  range absorb, but it means `detour_miles_to_city` is honest about measuring
  to a city rather than to a forecourt.
- **Containment by proximity cannot separate Detroit from Windsor**, two miles
  apart across a border. A point in Windsor is accepted as inside the USA. A
  border polygon would fix it; the error message naming the matched place is
  the mitigation.
- **No minimum-purchase or driver-hours constraints.** Either would break the
  exchange argument that makes the greedy provably optimal, so both are
  explicit non-goals rather than omissions.
- **No authentication or rate limiting.** Out of scope for the brief, and the
  route token being derivable means a map link leaks its own inputs — which is
  fine for a route between two cities and would not be if this had users.
