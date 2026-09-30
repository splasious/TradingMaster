"""HEG is now HEGAM on NSE

NSE renamed HEG Ltd to HEG Advanced Material (same ISIN, INE545A01024);
Kite lists it only as NSE:HEGAM now, so every nightly top-up of HEG failed
"not found" on all five timeframes and its candles stopped at 15 Sep. The
price carried straight over (HEG's last daily close is within 0.4% of
HEGAM's level), so this is a rename, not a new stock:

- the backfill copy's HEG (zerodha) becomes HEGAM, keeping its history,
  its nightly coverage -- the next top-up fills in from 15 Sep under the
  new name -- and every watchlist that holds it;
- the chart catalog's NSE HEG becomes HEGAM, keeping its candles.

If HEGAM is already there (added by hand meanwhile), HEG's watchlist
entries move to it, HEG leaves the nightly top-up and its chart row is
hidden instead. Each change goes to audit_logs; a second run finds
nothing to change.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-09-30 20:30:00.000000

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD, NEW = "HEG", "HEGAM"
NEW_NAME = "HEG ADVANCED MATERIAL"  # Kite's name for NSE:HEGAM

bf_symbols = sa.table(
    "bf_symbols", sa.column("id", sa.Uuid), sa.column("source", sa.String), sa.column("symbol", sa.String),
    sa.column("display_name", sa.String),
)
bf_coverage = sa.table("bf_coverage", sa.column("symbol_id", sa.Uuid))
watchlist_items = sa.table("bf_watchlist_items", sa.column("id", sa.Uuid), sa.column("watchlist_id", sa.Uuid), sa.column("symbol_id", sa.Uuid))
instruments = sa.table(
    "instruments", sa.column("id", sa.Uuid), sa.column("exchange", sa.String), sa.column("symbol", sa.String),
    sa.column("name", sa.String), sa.column("external_ref", sa.String), sa.column("is_active", sa.Boolean),
)
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def _audit(bind, action: str, object_type: str, object_id, previous: dict, new: dict) -> None:
    bind.execute(audit_logs.insert().values(
        id=uuid.uuid4(), user_id=None, action=action, object_type=object_type, object_id=str(object_id),
        previous_value=previous, new_value=new,
    ))


def _rename_backfill_symbol(bind) -> None:
    def symbol_id(symbol: str):
        return bind.execute(
            sa.select(bf_symbols.c.id).where(bf_symbols.c.source == "zerodha", bf_symbols.c.symbol == symbol)
        ).scalar_one_or_none()

    old, new = symbol_id(OLD), symbol_id(NEW)
    if old is None:
        return
    if new is None:
        bind.execute(bf_symbols.update().where(bf_symbols.c.id == old).values(symbol=NEW, display_name=NEW_NAME))
        _audit(bind, "BF_SYMBOL_RENAMED", "bf_symbol", old, {"symbol": OLD}, {"symbol": NEW})
        return

    moved = 0
    for item_id, watchlist_id in bind.execute(
        sa.select(watchlist_items.c.id, watchlist_items.c.watchlist_id).where(watchlist_items.c.symbol_id == old)
    ).all():
        has_new = bind.execute(
            sa.select(watchlist_items.c.id).where(watchlist_items.c.watchlist_id == watchlist_id, watchlist_items.c.symbol_id == new)
        ).first()
        if has_new:
            bind.execute(watchlist_items.delete().where(watchlist_items.c.id == item_id))
        else:
            bind.execute(watchlist_items.update().where(watchlist_items.c.id == item_id).values(symbol_id=new))
            moved += 1
    dropped = bind.execute(bf_coverage.delete().where(bf_coverage.c.symbol_id == old)).rowcount
    if moved or dropped:
        _audit(bind, "BF_SYMBOL_REPLACED", "bf_symbol", old, {"symbol": OLD},
               {"symbol": NEW, "watchlist_entries_moved": moved, "coverage_rows_dropped": dropped})


def _rename_instrument(bind) -> None:
    def instrument(symbol: str):
        return bind.execute(
            sa.select(instruments.c.id, instruments.c.is_active).where(instruments.c.exchange == "NSE", instruments.c.symbol == symbol)
        ).first()

    old, new = instrument(OLD), instrument(NEW)
    if old is None:
        return
    if new is None:
        bind.execute(instruments.update().where(instruments.c.id == old.id).values(symbol=NEW, external_ref=NEW, name=NEW_NAME))
        _audit(bind, "INSTRUMENT_RENAMED", "instrument", old.id, {"symbol": OLD}, {"symbol": NEW})
    elif old.is_active:
        bind.execute(instruments.update().where(instruments.c.id == old.id).values(is_active=False))
        _audit(bind, "INSTRUMENT_HIDDEN", "instrument", old.id, {"symbol": OLD, "is_active": True}, {"replaced_by": NEW, "is_active": False})


def upgrade() -> None:
    bind = op.get_bind()
    _rename_backfill_symbol(bind)
    _rename_instrument(bind)


def downgrade() -> None:
    pass  # NSE no longer lists HEG; nothing to go back to
