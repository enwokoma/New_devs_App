# Debugging notes: Property Revenue Dashboard

Scope: debug only, no rebuild. All changes are confined to the revenue path
(`backend/app/core/database_pool.py`, `backend/app/services/{reservations,cache}.py`,
`backend/app/api/v1/dashboard.py`), the frontend Dockerfile, and a new test file.

## How I reproduced

```bash
docker compose up --build -d
# log in as each client, then:
curl "localhost:8000/api/v1/dashboard/summary?property_id=prop-001" -H "Authorization: Bearer $TOKEN"
docker compose exec redis redis-cli keys 'revenue:*'
docker compose exec db psql -U postgres propertyflow -c "SELECT tenant_id, property_id, SUM(total_amount), COUNT(*) FROM reservations GROUP BY 1,2"
```

Before the fix, **both** clients received the identical payload for `prop-001`:
`total_revenue: 1000.0, reservations_count: 3`. The database says Sunset's
`prop-001` has 4 reservations totalling 2250.000, and Ocean's `prop-001` has none.

## Root cause 0 (the one hiding the others): the API never reached Postgres

`DatabasePool.initialize()` built its URL from `settings.supabase_db_*`
attributes that do not exist on `Settings`, so it raised `AttributeError` on
every request. `calculate_total_revenue()` caught that and returned a
**hard-coded mock table** (`prop-001 -> 1000.00 / 3`), keyed by property ID only.
Two further latent bugs sat behind it: `poolclass=QueuePool` is invalid for an
async engine, and `get_session()` was `async def` so `async with db_pool.get_session()`
would have failed even with a working URL.

Fix: derive the asyncpg URL from `DATABASE_URL`, drop the invalid pool class,
make `get_session()` synchronous, reuse one global pool, and **remove the mock
fallback**. A DB outage now returns HTTP 503 instead of fabricated financials.

## Bug 1 — Sunset's March total is wrong (timezone)

Reservation `res-tz-1` checks in at `2024-02-29 23:30 UTC`. Its property is in
`Europe/Paris`, where that instant is already `2024-03-01 00:30`. The monthly
function built naive `datetime(year, month, 1)` boundaries, i.e. UTC, so the
booking was attributed to February. UTC March = 1000.000 / 3 (what the client
saw); Paris March = 2250.000 / 4 (what their books say).

Fix: `local_month_bounds_utc()` builds the month in the property's timezone
with `zoneinfo` and converts the `[start, end)` boundaries to UTC before the
`TIMESTAMPTZ` comparison. DST is handled automatically (March 2024 in Paris ends
at UTC+2). The endpoint accepts optional `year`/`month`; without them it is
all-time, keeping the shipped frontend working.

## Bug 2 — Ocean sees another company's numbers (cache isolation)

Cache key was `revenue:{property_id}`. `prop-001` belongs to both tenants, so
whoever warmed the cache first had their figure served to the other tenant for
300 s. That is a cross-tenant data leak, not just staleness. Also,
`dashboard.py` defaulted a missing tenant to the string `"default_tenant"`.

Fix: key is `revenue:{tenant_id}:{property_id}:{period}`; a cached entry is
re-validated against the caller's tenant before use; a request with no tenant
is rejected with 403; a property the caller does not own returns 404 (the SQL
already filters on `tenant_id`, and the property lookup makes it explicit).

## Bug 3 — totals off by a few cents (float precision)

Amounts are `NUMERIC(10,3)` and Postgres sums them exactly (333.333 + 333.333 +
333.334 = 1000.000). The endpoint then did `float(total)`, and the UI does
`Math.round(100 * x) / 100` on that float. Binary floats cannot represent most
decimal fractions (`1.005 * 100 === 100.49999999999999`), so half-cent values
round the wrong way and the UI shows a "Precision Mismatch" warning.

Fix: keep the value as `Decimal`, round **once**, half-up, to cents
(`to_money`), and return it as a string (`total_revenue_str`) alongside the
exact 3-dp value (`total_revenue_exact`). `total_revenue` is still emitted as a
number for the prebuilt frontend, but it is now a cent-exact value so the
client's rounding is a no-op. The frontend source is not in the repository
(only `dist/`), so the client-side rounding could not be changed here; in
production the UI should format the string and never do arithmetic on money.

## Environment fix

`frontend/Dockerfile` ran `npm install && npm run build`, but the repo contains
no `package.json` or `src/`, only the compiled `dist/`. The image now serves
`dist/` with nginx directly.

## Tests

```bash
docker compose exec backend python -m pytest -q     # 8 passed
```

`backend/tests/test_revenue.py` pins the Paris/New York month boundaries, the
seed reservation landing in Paris-March, tenant-scoped cache keys, and
half-up cent rounding versus float behaviour.

## What I would do next in production

- Composite key or UUID for `properties.id`; never rely on a per-tenant text ID.
- Enforce isolation at the DB with real RLS policies (the tables have RLS enabled
  but no policies), setting the tenant in the session per request.
- Carry money as integer minor units or strings end to end; format in the UI.
- Build the frontend from source in CI and drop `dist/` from git.
