# TradingMaster — Architecture (Phases 1–2)

This describes what's actually built. See [`TradingMaster_PRD.md`](TradingMaster_PRD.md)
for the full product vision and the phase roadmap (section 63).

## Stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | Next.js 16 (App Router), TypeScript, Tailwind v4, TanStack Query | Dense, high-frequency UI updates; first-class theming; the natural fit for the chart-heavy screens later phases add. |
| Backend | FastAPI (async), SQLAlchemy 2.0 (async), Alembic | Python is required anyway for the quant/strategy/backtesting engines and the Python strategy sandbox (PRD sections 14–20); FastAPI gives async WebSocket support later phases need, in the same language. |
| Database | PostgreSQL (production/docker) / SQLite (local dev without Docker) | Matches PRD section 42. The app is dialect-agnostic (see "SQLite dev fallback" below). |
| Auth | JWT access token (15 min) + revocable refresh session (httpOnly cookie) | Short-lived access tokens limit exposure; refresh sessions are stored hashed and revocable server-side, so a compromised token can be killed without waiting for expiry. |
| Secrets | Fernet-encrypted broker credentials at rest | PRD section 47: broker credentials never plaintext, never sent to the frontend. |

## Request flow (current)

```
Browser (Next.js, :3000)
   │  fetch, credentials: include
   ▼
FastAPI (:8000)
   │  Authorization: Bearer <access token>  (in-memory on the client, never localStorage)
   │  refresh_token cookie (httpOnly, path=/api/v1/auth)  →  sessions table
   ▼
SQLAlchemy async session → Postgres/SQLite
```

The access token lives only in memory on the client (a module-level variable
in `lib/api.ts`), not localStorage — it's short-lived and re-acquired via the
httpOnly refresh cookie on page load, which keeps it out of reach of XSS
reading `localStorage`. A 401 triggers exactly one silent refresh-and-retry
(`apiFetch` in `frontend/lib/api.ts`) before surfacing an error.

## RBAC

Four roles, seeded by `backend/app/seed.py`: `administrator`, `trader`,
`analyst`, `viewer` (PRD sections 5, 48). Enforced twice, independently:

- **Backend**: `require_role(*roles)` (`backend/app/core/deps.py`) as a
  FastAPI dependency on every endpoint that needs it — this is the actual
  security boundary.
- **Frontend**: `useAuth().hasRole(...)` (`frontend/lib/auth-context.tsx`)
  hides/disables actions the user can't perform — a UX convenience, not a
  security boundary on its own.

## Broker abstraction (PRD section 7)

```
BrokerInterface (ABC)          backend/app/services/broker/base.py
   ├── MockBroker              backend/app/services/broker/mock_broker.py   ← used now
   ├── ZerodhaKiteBroker       (later phase, real API, real credentials)
   └── DeltaExchangeBroker     (later phase, real API, real credentials)

registry.py maps a Broker.code ("zerodha_kite", "delta_exchange") to an
adapter class. Swapping MockBroker for a real adapter later is a one-line
change in registry.py — nothing above it (the `/brokers` endpoints, the
frontend) needs to change.
```

`MockBroker.place_order` always returns `REJECTED` — Phase 1 proves the
connection/authentication flow end-to-end without ever pretending to place a
real order.

## Database schema (Phase 1 subset of PRD section 42)

`users`, `roles`, `user_roles`, `sessions`, `brokers`, `broker_accounts`,
`broker_credentials`, `broker_connections`, `audit_logs`. Defined in
`backend/app/models/`, migrated via a single Alembic revision
(`backend/alembic/versions/`). Later phases add their own tables (market
data, strategies, orders, ...) as their own migrations — this schema doesn't
need to change for that.

## SQLite dev fallback

`DATABASE_URL` in `.env` decides the dialect; the app doesn't otherwise know
or care which one is active (`app/core/config.py: is_sqlite` only affects
`connect_args`). One real gotcha this caused and how it's handled: SQLite
doesn't preserve `tzinfo` on `DateTime(timezone=True)` columns — a value
round-tripped through it comes back naive even though it was always stored
as UTC. `app/core/time.py: as_aware_utc()` normalizes that at the one place
it mattered (refresh-token expiry comparison in `auth.py`). Postgres doesn't
have this problem; the helper is a no-op there.

## Frontend structure

```
app/
  login/                 public
  (app)/                 gated by AuthProvider; redirects to /login if unauthenticated
    layout.tsx            Sidebar + Topbar shell
    dashboard/             real system-health panel; other panels marked "Phase N"
    settings/brokers/      real: connect/disconnect broker accounts (against MockBroker)
    settings/users/        real, admin-only: user list + create + role assignment
    <17 other modules>/    routed, each a labeled "scaffolded — Phase N" placeholder
components/ui/            Button, Card, Badge, StatusBadge, Table, Modal, Tabs, Tooltip, Input, Select
lib/
  api.ts                  fetch wrapper: attaches token, retries once on 401 via refresh
  auth-context.tsx         AuthProvider: login/logout/me, silent refresh on mount
  hooks.ts                 TanStack Query hooks (system health, brokers, users)
```

Design tokens (PRD section 39) live in `app/globals.css` as CSS variables,
exposed to Tailwind via `@theme inline` — `bg-positive`, `text-critical`,
`bg-active-soft`, etc. are real Tailwind utilities, not one-off hex codes.
Every status indicator pairs a color with an icon and a text label
(`components/ui/status-badge.tsx`) — PRD section 39.3 explicitly rules out
color-only signaling. Dark is the default theme (a trading terminal is a
long-session, low-glare tool per section 40); the toggle in the topbar
switches to light and persists via `next-themes`.

## Market data engine (Phase 2, PRD sections 8–11)

Same pluggable-adapter shape as the broker layer, but for historical OHLCV:

```
MarketDataSource (ABC)          backend/app/services/market_data/base.py
   ├── YahooNSEDataSource         real NSE equities/indices via the local
   │                              nse-yahoo-data sidecar service (its own
   │                              repo, HTTP API on :8800) -- 750+ symbols,
   │                              daily history back to listing. Optional:
   │                              not running → backfills fail with a clear
   │                              MarketDataSourceError, not a raw traceback.
   └── DeltaExchangeDataSource     real crypto perpetuals via Delta Exchange
                                   India's public REST API -- no API key
                                   needed for market data (candles/products
                                   are public endpoints).
```

`registry.py` maps `Instrument.data_source` ("yahoo_nse" / "delta_exchange")
to an adapter, same pattern as the broker registry. A real Zerodha- or
Delta-order-flow-sourced data adapter plugs in the same way in a later phase.

**Backfill** (`services/market_data/backfill.py`): `POST /market-data/backfill`
creates a `BackfillJob` row and runs the actual fetch as a FastAPI
`BackgroundTask` (opens its own DB session — the request's session is
already closed by the time it runs). Dedupes against existing candles by
timestamp before inserting, so re-running a backfill is a no-op rather than
a constraint violation. "Download completed" and "backfill completed" are
deliberately different things (PRD section 10) — the job only reaches
`completed` after real duplicate/insert accounting, not just a successful
HTTP call.

**Data quality** (`services/market_data/validation.py`): computed on demand
from stored candles, not persisted — OHLC-relationship sanity, non-positive
prices, and (for daily bars) a weekday-gap heuristic. That gap check is
explicitly *not* exchange-holiday-aware yet (no calendar data source wired
up), so a legitimate NSE holiday shows up as a "possible missing candle" —
documented in the code and in the Market Data UI rather than hidden.

**Real-time streaming** (PRD section 9): `services/market_data/tick_engine.py`
is a simulated price engine (random walk seeded from last close) driving a
real WebSocket (`/api/v1/ws/market-data`, token auth via query param since
browsers can't set headers on WS) with the actual protocol a live feed will
use later — subscribe/unsubscribe per connection, heartbeat, connection
lifecycle (connecting/connected/reconnecting/error). Every tick is tagged
`"source": "simulated"` end to end, including in the Markets page UI (PRD
Rule 11: never present synthetic data as real without saying so).

## Broker credentials note

The Delta Exchange API key/secret provided during development are **not**
in this codebase or its config. They're not needed yet: Phase 2 only uses
Delta's public market-data endpoints. Authenticated endpoints (balances,
order placement) are Phase 7 territory, gated by the risk engine and
paper-trading approval workflow — wiring real order authority in earlier
would violate the platform's own safety rules (PRD Rule 8, section 49).

## What's deliberately not here yet

No strategies, no backtesting, no real orders/positions/risk logic, no
options/derivatives data. PRD section 63 phases them in deliberately so
building on a shaky foundation doesn't mean redoing it later. See the nav
sidebar's "P3".."P8" badges for which phase each remaining screen belongs to.
