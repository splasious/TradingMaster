# TradingMaster

Institutional-grade multi-strategy trading platform. See [`TradingMaster_PRD.md`](TradingMaster_PRD.md)
for the full product specification and [`ARCHITECTURE.md`](ARCHITECTURE.md) for
how this codebase implements it.

**Current status: Phases 1–5.** Auth, RBAC, database schema, app
shell/navigation, design system, broker abstraction (mock adapter — no real
Zerodha/Delta order-placing credentials yet), a real market data engine
(NSE via the optional local `nse-yahoo-data` sidecar, Delta Exchange crypto
via its public API), real analytics (13 technical indicators, a
multi-timeframe engine, candlestick charting, a market scanner with saved
filters), a real strategy engine (visual rule-based strategies and
sandboxed Python strategy import — `import os`, `open()`, `eval()` etc. are
all verified-blocked, not just assumed), and a real backtesting engine:
bar-by-bar simulation with no look-ahead bias by construction, the full
standard metric set (Sharpe, Sortino, CAGR, drawdown, ...), out-of-sample
testing, trade-resampling Monte Carlo, and grid-search optimization. Every
later phase (paper/live trading) builds on this without reworking it.

## Prerequisites

- Python 3.11+ (a `py` launcher or `python3` on PATH)
- Node.js 20+
- PostgreSQL 16 + Redis, **or** Docker — for production-like local dev
- No Postgres/Docker available? The backend falls back to SQLite for local
  dev automatically (see `backend/.env.example`).
- Optional, for real NSE historical data: the sibling
  [`nse-yahoo-data`](../nse-yahoo-data) service running on `:8800`
  (`python app/main.py` in that repo). Without it, NSE backfills fail with a
  clear error; Delta Exchange market data works regardless (public API,
  no local service needed).

## Backend

```bash
cd backend
python -m venv .venv
./.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt

cp .env.example .env             # edit DATABASE_URL etc. as needed
alembic upgrade head             # create schema
python -m app.seed               # seed roles, admin user, broker catalog

uvicorn app.main:app --reload    # http://localhost:8000
pytest                           # run the test suite
```

Default seeded admin login (change immediately outside local dev):
`admin@tradingmaster.internal` / `ChangeMe123!` (from `.env` / `SEED_ADMIN_*`).

API docs: `http://localhost:8000/docs` (Swagger) once the server is running.

## Frontend

```bash
cd frontend
npm install
cp .env.local.example .env.local   # points at the backend API URL
npm run dev                        # http://localhost:3000
```

Log in with the seeded admin credentials above.

## Docker (Postgres + Redis, production-like)

```bash
docker compose up -d postgres redis
# then run the backend/frontend steps above against DATABASE_URL pointing at postgres
```

## Repository layout

```
backend/    FastAPI service: auth, RBAC, broker + market-data abstractions, DB models, migrations
frontend/   Next.js app: design system, app shell, dashboard, markets/market-data, broker/user settings
docker-compose.yml   Postgres + Redis for local dev
TradingMaster_PRD.md Master product specification
ARCHITECTURE.md      Architecture so far and how later phases plug in
```
