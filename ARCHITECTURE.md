# TradingMaster — Architecture (Phases 1–8)

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

## Indicator engine, multi-timeframe, and scanner (Phase 3, PRD sections 12, 13, 33)

`backend/app/services/indicators/` -- 13 indicators across all 5 PRD
categories (trend, momentum, volume, volatility, structure), each a pure
pandas function of past-and-current bars only (`base.py`'s module docstring
explains the one deliberate exception: `structure.swing_high_low` is a
retrospective chart annotation, not safe to feed into live signals). Every
one has a correctness test against a known reference value or invariant
(RSI/MFI/Stochastic bounded [0,100], Bollinger upper≥middle≥lower, ATR≥0,
pivot points provably use only the *previous* bar, etc.) in
`tests/test_indicators.py`.

`registry.py` maps a code ("rsi", "macd", ...) to its spec — same pattern as
the broker and market-data registries. `GET /indicators/calculate` computes
one on demand against stored candles; nothing is precomputed or cached, so
there's no staleness to manage.

**Multi-timeframe** (`services/market_data/resample.py`): aggregates stored
daily candles up to weekly/monthly. The PRD's explicit requirement --
"higher-timeframe information becomes available only after the
corresponding candle has actually closed" -- is enforced by dropping the
last resampled bar whenever the underlying period (the current week/month)
hasn't fully elapsed yet (`only_closed=True`, the default); tested directly
in `test_resample_drops_still_forming_period_by_default`. Indicator
calculation itself is only wired up against the stored base timeframe today
("1d") — the Charts page disables overlay checkboxes when viewing a
resampled timeframe rather than silently computing something misleading.

**Scanner** (`services/scanner.py`, PRD section 33): filters are a
structured `{field, operator, value}` triple, not a user-supplied
expression -- `field` is either a raw OHLCV column or `"indicator.output"`
resolved through the same indicator registry above, and evaluation only
ever calls known Python operators (`operator.gt`, etc.). No `eval()`, no
arbitrary code path, consistent with PRD Rule 3 even though this isn't the
Python-strategy-sandbox that rule was written for.

## Strategy engine and Python sandbox (Phase 4, PRD sections 14, 15, 16, 25, 32)

Two ways to define a strategy, sharing one schema (`strategies` +
`strategy_versions`, `backend/app/models/strategy.py`):

- **Visual mode**: `entry_rules` / `exit_rules` are the *same* rule-tree
  shape the scanner already evaluates (`{"all"/"any": [...]}` of
  `{field, operator, value}` leaves) -- `services/strategy/rules.py` just
  calls the scanner's `evaluate_condition` per leaf. One condition
  evaluator, two UIs (Scanner, Strategy Builder), no duplicated logic.
- **Python mode**: arbitrary code, but never executed directly in the API
  process (PRD Rule 3). `services/strategy/sandbox_worker.py` runs as a
  **separate subprocess**, in a **RestrictedPython**-compiled environment
  whose `safe_builtins` has no `__import__`, `open`, `exec`, or `eval`.
  Verified both ways in `tests/test_strategy_sandbox.py`: `import os`,
  `open(...)`, `__import__(...)`, `eval(...)`, and `exec(...)` are all
  rejected (some at compile time, some at runtime when the missing builtin
  is referenced) -- and a real infinite loop is killed by the orchestrator's
  hard timeout (`sandbox.py`, default 5s) with the subprocess actually
  terminated, not just abandoned. This is two independent isolation layers
  (AST restriction + OS process boundary); a production deployment should
  add a third (container/network-namespace isolation) on top -- not built
  here since this dev environment has no Docker, but nothing above assumes
  its absence.

The strategy contract is deliberately minimal: Python code defines
`generate_signal(candles, params) -> "BUY" | "SELL" | "HOLD"`. Backtesting
(Phase 5) calls this same function bar-by-bar; nothing about the sandbox
changes when that phase lands.

**Deployment state machine** (`services/strategy/state_machine.py`, PRD
section 25): all 8 states and their legal transitions are modeled and
tested now (`DRAFT → BACKTESTED → OPTIMIZED → ... → LIVE`, and any state
can fall back to `DRAFT`), but only `DRAFT` is reachable through the API
today. Nothing fabricates a "backtested" strategy without a real backtest
behind it -- Phase 5 will call `can_transition()` when a backtest actually
completes, using the same validator these tests already cover.

## Backtesting engine (Phase 5, PRD sections 17-20)

The look-ahead-bias guarantee is structural, not a rule someone has to
remember: `services/backtest/signals.py` computes a strategy's signal at
bar `i` from `candles[: i + 1]` only -- true for both the visual rule-tree
evaluator and the Python sandbox's batch mode (`sandbox_worker.py`'s
`run_backtest_signals`, one subprocess call for the whole run rather than
one per bar, so a multi-year backtest doesn't spawn hundreds of processes).
`services/backtest/engine.py` then turns those per-bar signals into trades
with a specific, realistic fill convention: a signal computed from bar i's
close fills at bar **i+1's open** (never the same bar), while stop-loss/
take-profit are standing orders allowed to trigger intrabar off the
*current* bar's high/low. Every formula in `metrics.py` (Sharpe, Sortino,
CAGR, max drawdown, profit factor, ...) runs against the actual simulated
trades/equity curve -- nothing is estimated.

Also real, not stubbed:
- **Out-of-sample testing**: `out_of_sample_split_pct` on a backtest job
  splits the candle range and runs two independent simulations, returned
  as separate metric sets.
- **Monte Carlo** (`monte_carlo.py`): bootstraps the *realized* trade P&Ls
  (never invents new trades) into many alternate equity paths to show how
  much of a result could be sequencing luck vs. edge.
- **Grid search optimization** (`optimization.py`, PRD section 20):
  Python-strategy only, since `generate_signal(candles, params)` already
  takes a params dict — a visual rule's conditions use literal values, not
  named parameters, so extending that DSL to support optimizable
  thresholds is a real feature this phase didn't build. Capped at 60
  combinations (each one re-runs a full backtest).

A completed backtest is the actual, earned trigger for the strategy's
`DRAFT -> BACKTESTED` transition (`runner.py` calls the same
`can_transition()` from Phase 4) — the first time anything in the codebase
sets a status other than `DRAFT`.

## Risk engine and paper trading (Phase 6, PRD sections 21, 24)

```
Live Market Data -> Strategy -> Signal -> Risk Engine -> Paper Execution -> Paper Portfolio
```

`services/risk/engine.py` is the literal gate between a signal and an order,
for both paper trading now and live trading later (Phase 7 reuses it
unchanged). Three checks, each with a specific auditable reason on
rejection (PRD section 24's explicit requirement, not a bare "REJECTED"):
sufficient cash, max open positions, max daily loss. Exits are always
approved -- risk controls gate new exposure, never trap an existing
position open.

`services/paper_trading/engine.py`'s `evaluate_deployment()` is the whole
pipeline for one deployment, one tick -- a plain async function, not baked
into the background loop, so tests call it directly and deterministically
(the same pattern already used for backfill/backtest/optimization jobs).
`services/paper_trading/scheduler.py` calls it for every active deployment
every ~10s; the "Evaluate Now" button in the UI calls the exact same
function on demand. Price comes from the tick engine's simulated feed
(Phase 2) — a paper deployment `subscribe()`s to it independent of any
browser watching, so it keeps generating prices even with no UI open.

**A real bug this phase surfaced and fixed**: the live evaluation loop
combines DB-loaded candles with a freshly built "current bar" from the live
tick price, in the same list, then hands it to the same
`evaluate_rule_node`/indicator pipeline scanner.py and strategies use.
SQLite-loaded timestamps come back tzinfo-naive (the recurring gotcha
documented in `core/time.py`) while the synthetic bar's timestamp is
`datetime.now(timezone.utc)` — aware. Mixing the two in one pandas column
crashed `sort_values` with "can't compare offset-naive and offset-aware
datetimes" the first time a paper deployment used an *indicator-based*
rule (a raw `close` rule never touches `candles_to_frame`, which is why
this phase's first pass tests didn't catch it). Fixed once, centrally, in
`indicators/base.py: candles_to_frame()` — every caller (scanner,
strategies, paper trading, and Phase 5's backtester) benefits, and
`test_paper_trading_engine.py` has a named regression test for it.

## Live trading (Phase 7, PRD sections 22-24, 28, 49)

The real thing, not a mock: `services/broker/delta_broker.py` is a genuine
Delta Exchange India adapter -- HMAC-SHA256 request signing verified
directly against their live API before writing a line of adapter code
(Rule 1: never invent broker APIs), including reproducing their own
documented signing example byte-for-byte as a regression test
(`test_delta_broker.py::test_signature_matches_delta_documented_example`).
`services/broker/zerodha_broker.py` is a second real adapter, for Zerodha
Kite Connect v3 -- both replace `MockBroker` in the registry (see "Zerodha
Kite adapter" below for what "real" means here given no live account was
available to test against).

**What actually gates a real order from being placed**, in order:

1. **Kill switch** (`services/live_trading/kill_switch.py`) -- a single-row
   DB flag checked first, before anything else. Active means every live
   deployment is blocked, full stop.
2. **Safety checklist** (`services/live_trading/safety.py`, PRD section 49)
   -- broker actually connected, strategy status is `approved` (the full
   backtest -> paper-trade -> validate -> approve pipeline from Phase 4/5/6
   has to have actually happened), risk limits configured, position sizing
   configured. Checked server-side on deployment start, not trusted from a
   client checkbox.
3. **Explicit confirmation** -- `POST /live-trading/deployments` requires
   `confirmed: true` in the body; the UI's checkbox sets it, but the API
   itself refuses without it regardless of what the client sends.
4. **Risk engine** (the same `services/risk/engine.py` from Phase 6,
   unchanged) -- cash, max positions, daily loss, evaluated fresh against
   the broker's real balance before every entry.

**Order confirmation** (`services/live_trading/oms.py`, PRD Rule 5): after
`place_order()` returns, the OMS immediately calls `get_order_status()` and
only trusts *that* response for the order's recorded state -- a broker's
initial placement response isn't assumed to be the final word. The order
lifecycle (`services/live_trading/order_state_machine.py`, PRD section 23)
models all 9 states with real transition rules; both Delta's 4 documented
order states and Kite's 11 map onto it explicitly (`STATE_MAPS`, keyed by
broker code), not by guessing.

**Live price**, deliberately, is never the simulated tick engine.
`oms._get_live_price_and_context()` is the one place that knows either
broker's pricing vocabulary: for Delta, `DeltaExchangeDataSource.get_ticker()`
calls their real public `/v2/tickers/{symbol}` endpoint (verified live) for
price plus the numeric `product_id` orders need; for Kite, the already-
authenticated adapter's `get_ltp()` calls the real `/quote/ltp` endpoint
(Kite's LTP requires auth, unlike Delta's public ticker) for price plus
the `tradingsymbol`/`exchange`/`product` context Kite's order endpoint
needs. Everything above that one function is broker-agnostic -- neither
broker's field names leak into the risk engine, the signal evaluators, or
`LiveOrder`/`LivePosition`.

**Reconciliation** (`services/live_trading/reconciliation.py`, PRD section
28) is detection-only: it diffs local `LivePosition` rows against the
broker's real `get_positions()` and reports matches/mismatches/orphans on
both sides. It never auto-corrects anything -- "never silently overwrite
state" is enforced by the function simply not having a write path.

**Deliberately not automatic**: unlike paper trading's ~10s background
scheduler, there's no live-trading equivalent. A live deployment only
evaluates when explicitly triggered (the "Evaluate Now" button, or a future
scheduled job someone deliberately wires up) -- one more human-in-the-loop
safety margin given real money is at stake, not an oversight.

**Credentials**: never written to a file or logged by this codebase. They're
entered through Settings -> Brokers -> Connect Delta Exchange (the exact
mechanism built in Phase 1), Fernet-encrypted at rest, decrypted only in
memory immediately before an authenticated call.

## Zerodha Kite adapter (`services/broker/zerodha_broker.py`)

Auth here is fundamentally different from Delta's per-request HMAC
signing, which is why it gets its own section. Kite Connect uses an
interactive, OAuth-like flow with no key/secret-only path:

1. `POST /brokers/accounts` stores the api_key/api_secret (encrypted, as
   always) but deliberately does *not* call `authenticate()` -- there's no
   request_token yet, so it can't succeed. The connection is left
   `disconnected` with an explanatory `last_error`, not `error` -- this
   isn't a failure, it's mid-setup (`registry.requires_interactive_auth()`
   is the generalization point `connect_broker_account` branches on).
2. `GET /brokers/accounts/{id}/kite/login-url` returns
   `ZerodhaKiteBroker.build_login_url(api_key)` -- Kite's real documented
   login URL shape.
3. The user logs in on Zerodha's own site (real 2FA, real account) and
   Kite redirects to whatever URL is registered against that Kite Connect
   app, appending a one-time `request_token`. Kite Connect v3 does **not**
   support a `state` parameter to round-trip which account this was for
   -- so the frontend remembers the pending `account_id` in `localStorage`
   before opening the login tab and reads it back on
   `/settings/brokers/kite/callback`, rather than inventing a capability
   Kite doesn't have.
4. `POST /brokers/accounts/{id}/kite/callback` exchanges the request_token
   for a session `access_token` (`POST /session/token`, SHA-256 checksum
   of `api_key + request_token + api_secret` -- Kite's documented
   formula), then re-encrypts the credentials to include it. This
   endpoint returns HTTP 200 even when the broker itself rejects the
   exchange (`connection_status: "error"` in the body, same pattern as
   the original connect endpoint) -- the frontend has to check that field
   rather than treating any 200 as success (a real bug caught and fixed
   during Playwright verification of this exact flow).

**What "real" means here without a live account**: no Kite Connect
developer subscription was available while building this, so a genuine
authenticated session was never exercised end-to-end. What *was* verified
live: every endpoint this adapter calls (`/session/token`, `/user/profile`,
`/quote/ltp`) was hit with placeholder credentials and returned Kite's
real, correctly-shaped error envelope (`{"status":"error","error_type":
"TokenException","message":"..."}`) rather than a malformed-request
rejection -- confirming the request format and `_request()`'s
success/error parsing against Kite's actual servers, not just their docs.
The remaining gap is a real login; that's the first thing to try once
real credentials exist (`zerodha_broker.py`'s module docstring has the
exact verification log).

**Kite session expiry**: unlike Delta's API key (long-lived), a Kite
`access_token` expires daily (~6am IST) and there is no refresh token in
the public API. `oms.evaluate_live_deployment()` re-authenticates at the
top of every evaluation (real cost: one extra call per broker per poll,
accepted for correctness), so an expired session surfaces as a clean
`action="error"` outcome with Kite's real message -- not a crash -- and
the fix is the same "Login with Zerodha" button, not a code change.

## Broker credentials note

Real Delta Exchange API credentials were provided during development.
They're not in this codebase, this repo's git history, or any file on
disk — they were used exactly once, from an ephemeral scratch script
outside the repo (deleted immediately after), to verify the real signing
scheme in `delta_broker.py` against Delta's live API before writing the
adapter (that single verification hit an IP-whitelist restriction on the
key, confirming the signing logic was correct without ever placing an
order). From here on, credentials only ever enter the system through
Settings -> Brokers -> Connect Delta Exchange, Fernet-encrypted at rest —
the same mechanism built in Phase 1, never touched directly -- **or**
through the optional `.env`-based seed bootstrap described in the Phase 8
section below, which ultimately writes to that exact same encrypted table.

Note for running this locally: Delta Exchange requires the calling
machine's IP to be whitelisted per API key. Whitelist your own machine's
real outbound IP in Delta Exchange > Account > API Management before
expecting `authenticate()` to succeed.

## Monitoring, alerts, reports, and backups (Phase 8, PRD sections 37, 38, 52-54)

**Alerts** (`models/alert.py`, `services/alerts/service.py`, PRD section
38): a real DB-backed feed, not a UI-only toast. `create_alert()` is called
directly from inside the paper trading engine and the live trading OMS at
the moments that actually matter -- order rejected, position entered/exited
(stop-loss/take-profit/signal each get their own alert type), and from
`activate_kill_switch()` for every affected deployment owner plus the
activating admin. Severity is INFO for routine paper-trading events,
WARNING for paper-trading risk events, and CRITICAL for anything in live
trading or the kill switch -- reflecting that live money is at stake.
`GET /alerts`, `/alerts/unread-count`, `POST /alerts/{id}/read`, and
`POST /alerts/read-all` are scoped to `user_id`, never returning another
user's alerts. The topbar's bell icon polls unread-count every 15s.

**System monitor** (`services/monitoring/service.py`, `GET
/system/monitor`, PRD section 37): every number is real, not a
placeholder -- `psutil` for CPU/memory/disk, `tick_engine`/
`paper_trading_scheduler` internal state for whether the background loops
are actually running, and live DB counts of active paper/live deployments.

**Reports** (`services/reports/service.py`, `GET /reports/trades.csv`,
`GET /reports/summary`, PRD section 54): `get_trade_rows()` merges
`PaperTrade` and `LiveTrade` -- joined through deployment ownership so a
user only ever sees their own trades -- into one sorted list, filterable by
environment and date range. This is also why `LiveTrade` exists
(`models/live_trading.py`): `LiveOrder` alone only records individual order
legs with no entry/exit pairing, so `oms._exit_position()` now computes
`pnl`/`pnl_pct` and writes a `LiveTrade` row on every live exit, mirroring
what paper trading already had. `PaperTrade` gained a real `exit_reason`
column in this phase too (it was previously hardcoded to `"signal"` in the
report layer even though the paper engine already knew the true reason --
fixed at the source instead of papering over it in the report).

**Backup** (`services/backup/service.py`, `POST`/`GET /backup`, `GET
/backup/{filename}/download`, PRD section 53, admin-only): for SQLite (the
default local-dev database), a real consistent snapshot taken through
`sqlite3`'s own backup API -- safe to run against a live database, unlike a
plain file copy, which risks reading a half-written page. For PostgreSQL,
this deliberately does *not* attempt an automated file-copy backup (that
isn't a valid strategy for a live Postgres server) -- `create_backup()`
raises `NotImplementedError` naming the exact `pg_dump`/`pg_restore`
commands an operator should run instead, rather than automating a
subprocess shell-out with connection credentials. `resolve_backup_path()`
only ever matches this service's own generated filename format, so a
`filename` from the API can't path-traverse outside the backup directory.

**Delta Exchange credential bootstrap** (`app/seed.py:_provision_delta_account`,
PRD section 47): an optional path for local dev -- if `DELTA_API_KEY`/
`DELTA_API_SECRET` are set in the gitignored, OS-ACL-restricted `.env`
file, `python -m app.seed` encrypts them into the same `broker_credentials`
table the Settings -> Brokers UI writes to (never plaintext in the
database) and attempts one real authenticated call so the initial
`BrokerConnection.status` reflects reality (CONNECTED, or ERROR with
Delta's actual error message) instead of defaulting to an untested
CONNECTING. Idempotent -- running seed again detects the existing account
and skips.

## What's deliberately not here yet

A real Zerodha Kite session that's actually been logged into -- the
adapter, endpoints, and UI flow are all real and built (see "Zerodha Kite
adapter" above), but no Kite Connect developer subscription was available
to complete one live end-to-end. No options/derivatives data, no drawing
tools or multi-panel charting, no market-breadth indicators (no data source
for OI/PCR yet), no strategy edit UI beyond create (the API supports
versioning -- tested -- but the Strategy Builder page only wires up
creation), no walk-forward *optimization* specifically (out-of-sample
testing and grid search both exist independently; combining them into one
workflow is a further step), no automatic live-evaluation scheduler
(deliberate -- see the live trading section above), no consolidated
cross-environment Portfolio/Orders/Positions/Risk Management screens (paper
and live each have their own view; a unified one is a later polish pass --
these four nav items are still scaffolded placeholders), no reconciliation
*remediation* (detection is real and tested; fixing a detected mismatch is
still a manual, out-of-band action), no PDF/Excel report export (CSV only
-- no new heavy dependency pulled in just for this), no automated backup
scheduling (backups are triggered manually from Settings -> Backup, not on
a cron). PRD section 63 phases the rest in deliberately so building on a
shaky foundation doesn't mean redoing it later.
