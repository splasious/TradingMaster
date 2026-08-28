# TradingMaster — Architecture (Phase 1: Foundation)

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

## What's deliberately not here yet

No market data, no strategies, no backtesting, no real orders/positions/risk
logic, no WebSocket streaming. Building those against a shaky foundation
(auth, RBAC, a fake broker abstraction) would mean redoing them later — PRD
section 63 phases them in specifically so that doesn't happen. See the nav
sidebar's "P2".."P8" badges for which phase each screen belongs to.
