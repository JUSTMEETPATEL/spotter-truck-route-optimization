# Future work

Everything the 17 build issues asked for is done and merged. This file is what
came *after* them: work that was identified, deliberately deferred, and is
worth doing next. Each item says why it was not done, so the deferral is a
decision on the record rather than a gap.

Ordered roughly by value for effort.

## 1. Drop the plan cache and keep one cache at the network boundary

There are two caches. The plan cache stores the finished response, keyed on the
endpoints **and** the vehicle. The road cache stores just the route, keyed on
the endpoints alone. Now that the second one exists, the first buys much less
than it costs:

| | Plan cache | Road cache |
|---|---|---|
| Entry size (median) | 368 KB | 59 KB |
| Copies per city pair | one **per vehicle variant** | **one, shared** |
| Repeat request | ~1 ms | ~63 ms |

So the plan cache buys the last ~62 ms at roughly 6× the memory, multiplied by
every `mpg`/range/detour combination anyone tries. It can also serve a plan
priced from a `Station` table that has since been reloaded, where the road
cache cannot: the road is the only part that genuinely does not change.

**Why it was not done:** `route_token` is a one-way hash, and `plan_for_map()`
resolves a map link by finding the plan in that cache. Removing it means moving
the request parameters into `map_url` first — a change to the demo path, which
was not worth the risk close to a deadline.

**Worth noting:** doing so would make shared map links survive a restart and a
cache flush, which today they do not.

## 2. Make `meta.cached` say which cache

`cached: false` alongside `external_api_calls: 0` and `provider_ms: 0.0` is a
real and meaningful combination — a freshly computed plan off a cached road —
but it reads like a contradiction. Either add a `road_cached` boolean, or turn
`cached` into `"plan"` / `"road"` / `false`. The latter is a breaking change to
a published boolean field, so it needs a version bump or a parallel field.

## 3. `ROAD_CACHE_TTL_SECONDS` is missing from `.env.example`

The setting exists, is env-readable, and is in the README's configuration
table, but the example env file was not updated when it was added. One line.

## 4. Persist the cache across restarts

Both layers are `LocMemCache` by default, so they die with the process. Every
`runserver` restart or deploy pays the full routing call again for the first
request to each city pair. `REDIS_URL` is already wired through
`config/settings.py` and needs no code change — it just needs to be set, and
the README should say plainly that this is what makes the caching survive a
restart.

A management command that pre-fetches a list of demo routes would also make a
cold start cheap, though it has to run **against the server over HTTP**: a
management command runs in its own process and would warm the wrong LocMem.

## 5. Self-host OSRM with an HGV profile

The public demo server is a development service on a **car** profile: no truck
weight, height, length or hazmat restrictions, and no SLA. It is also the
single largest source of latency and of variance — a wobble costs 10 s of
timeout plus a retry, which `meta.provider_ms` now makes visible.

Self-hosting fixes correctness and latency at once. Only the geometry changes;
the optimizer is untouched.

## 6. Replace proximity containment with a border polygon

US containment is decided by distance to the nearest gazetteer place, which
cannot separate Detroit from Windsor, two miles apart across a border. A point
in Windsor is accepted as inside the USA. A real border polygon would fix it.
The current mitigation is that the error message names the matched place.

## 7. Recover the one excluded Station

Gazetteer coverage is 99.98%: 6,626 Stations resolve to 6,625, with one
excluded — a single truckstop in Etters, PA. It is counted and disclosed on
`/api/v1/health/` rather than hidden. Adding a row to `data/city_overrides.csv`
would recover it.

This is also why the build issues said 6,626 Stations and 3,808 Candidates
while the running service reports 6,625 and 3,807. The difference is that one
exclusion, working as designed.

## 8. Decide whether `/api/v2/route/` stays

The indexed optimizer is correct and returns byte-identical plans, and on
adversarial price profiles it is 3–4× faster. On realistic ones it is ~5×
slower, and on a real corridor of 86–142 candidates both finish in under a
millisecond, so it has no effect on response time. It is kept and documented
honestly rather than deleted.

It is worth keeping only as long as the README keeps explaining why it is not
the default. If that explanation ever goes, the endpoint should go with it.

## 9. Constraints that would need a different optimizer

Minimum purchase quantities and driver-hours limits are both realistic and both
**break the exchange argument** that makes the greedy rule provably optimal.
They are explicit non-goals rather than omissions. Adding either means moving to
a dynamic program or an LP, and re-doing the optimality tests against it.

## 10. Prices are a static snapshot

The supplied file has no timestamps, so nothing here refreshes prices and
averaging repeated rows is a reading of the file rather than a fact about the
world. A real deployment needs a price feed with timestamps, and a cache policy
that expires plans when prices move — which is another argument for item 1,
since the road cache would be unaffected by a price refresh.

## 11. No authentication or rate limiting

Out of scope for the brief. Worth noting that the route token is derivable from
its inputs, so a map link leaks the request that made it — fine for a route
between two cities, not fine if this ever had users. Item 1 would make that
worse by putting the parameters in the URL outright, so the two decisions are
linked.
