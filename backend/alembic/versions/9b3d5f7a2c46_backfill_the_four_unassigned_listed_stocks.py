"""Download the 4 listed stocks that never got any data, and give them Zerodha as their source

CUPID, MTARTECH, STLTECH and TDPOWERSYS were added to the 15-minute lists
(MACD - RSI - 15 MIN, RS Rotation 15 MIN) on 1 Oct, but their chart-catalog
rows are the old seeded ones with data_source "unassigned" and they were
never put on the Zerodha backfill list -- so no candles at all, the strategies
never ranked them, and the live candle sync failed on them every minute
("No market data source registered for 'unassigned'", 6 Oct). Agreed 6 Oct.

For each still "unassigned": puts it on the Zerodha backfill list, queues its
history downloads (daily from 2021, 15-minute from April 2026, 5-minute from
July 2026 -- what the other listed stocks hold), and sets the catalog row's
source to zerodha_kite (what catalog sync would set once the bars are copied)
so the live sync fetches it from now on. The backfill worker runs the jobs;
catalog sync then copies the bars into the chart table. A stock already on
Zerodha is left alone; running again changes nothing.

Revision ID: 9b3d5f7a2c46
Revises: 8f2b4d6a1c35
Create Date: 2026-10-06 11:00:00.000000

"""
import uuid
from datetime import date
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9b3d5f7a2c46"
down_revision: Union[str, None] = "8f2b4d6a1c35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYMBOLS = ("CUPID", "MTARTECH", "STLTECH", "TDPOWERSYS")
DOWNLOADS = (("1d", date(2021, 1, 1)), ("15m", date(2026, 4, 1)), ("5m", date(2026, 7, 1)))

instruments = sa.table(
    "instruments", sa.column("id", sa.Uuid), sa.column("exchange", sa.String), sa.column("symbol", sa.String),
    sa.column("name", sa.String), sa.column("data_source", sa.String),
)
bf_symbols = sa.table(
    "bf_symbols", sa.column("id", sa.Uuid), sa.column("source", sa.String), sa.column("symbol", sa.String),
    sa.column("display_name", sa.String), sa.column("created_at", sa.DateTime(timezone=True)),
)
bf_jobs = sa.table(
    "bf_backfill_jobs", sa.column("id", sa.Uuid), sa.column("symbol_id", sa.Uuid), sa.column("source", sa.String),
    sa.column("timeframe", sa.String), sa.column("start_date", sa.Date), sa.column("end_date", sa.Date),
    sa.column("status", sa.String), sa.column("downloaded_count", sa.Integer), sa.column("inserted_count", sa.Integer),
    sa.column("duplicate_count", sa.Integer), sa.column("priority", sa.Integer), sa.column("attempts", sa.Integer),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(instruments.c.id, instruments.c.symbol, instruments.c.name)
        .where(instruments.c.exchange == "NSE", instruments.c.symbol.in_(SYMBOLS), instruments.c.data_source == "unassigned")
    ).all()
    for instrument_id, symbol, name in rows:
        symbol_id = bind.execute(
            sa.select(bf_symbols.c.id).where(bf_symbols.c.source == "zerodha", bf_symbols.c.symbol == symbol)
        ).scalar_one_or_none()
        if symbol_id is None:
            symbol_id = uuid.uuid4()
            bind.execute(bf_symbols.insert().values(id=symbol_id, source="zerodha", symbol=symbol, display_name=name or symbol,
                                                    created_at=sa.func.now()))
        for timeframe, start in DOWNLOADS:
            waiting = bind.execute(
                sa.select(sa.func.count()).select_from(bf_jobs).where(
                    bf_jobs.c.symbol_id == symbol_id, bf_jobs.c.timeframe == timeframe, bf_jobs.c.status.in_(("pending", "running")),
                )
            ).scalar_one()
            if not waiting:
                bind.execute(bf_jobs.insert().values(
                    id=uuid.uuid4(), symbol_id=symbol_id, source="zerodha", timeframe=timeframe, start_date=start, end_date=None,
                    status="pending", downloaded_count=0, inserted_count=0, duplicate_count=0, priority=0, attempts=0,
                    created_at=sa.func.now(),
                ))
        bind.execute(instruments.update().where(instruments.c.id == instrument_id).values(data_source="zerodha_kite"))
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=None, action="BF_BACKFILL_STARTED", object_type="bf_symbol", object_id=str(symbol_id),
            previous_value={"data_source": "unassigned"},
            new_value={"source": "zerodha", "symbol": symbol, "timeframes": [tf for tf, _ in DOWNLOADS],
                       "reason": "listed in the 15-minute strategies since 1 Oct, never downloaded (agreed 6 Oct)"},
        ))


def downgrade() -> None:
    pass  # the downloaded history and the Zerodha source stay
