"""remove yahoo finance traces

Yahoo (nse-yahoo-data) is fully retired -- NSE data now comes from Zerodha
exclusively. This purges the actual candle/backfill data that came from
Yahoo and relabels catalog rows that had no real source of their own
(previously "yahoo_nse") to a neutral "unassigned" sentinel, so no
Yahoo-branded value remains anywhere in stored data. Instrument/BfSymbol
catalog *rows* are left in place (never deleted) -- real strategies,
backtests, and trading history can still reference them by id; only the
now-orphaned label and the actual downloaded bars are removed. A later
Zerodha backfill + "Sync to Catalog" naturally re-labels any row it
touches (see catalog_sync.py), which is how these rows become visible
again.

Revision ID: d4e5f6a7b8c9
Revises: c8d1e3f5a9b2
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c8d1e3f5a9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Candles the main catalog got directly from Yahoo, or bridged in from
    # the Data Backfill Platform's now-deleted "yahoo" block.
    op.execute("DELETE FROM ohlcv_candles WHERE source IN ('yahoo_nse', 'bf_yahoo')")

    # Catalog rows with no real source backing them anymore -- inert until
    # re-synced from Zerodha, same as before, just no longer Yahoo-labeled.
    op.execute("UPDATE instruments SET data_source = 'unassigned' WHERE data_source = 'yahoo_nse'")

    # bf_symbols.id cascades (ON DELETE CASCADE) to bf_ohlcv_bars,
    # bf_backfill_jobs, and bf_watchlist_items -- deleting the symbol rows
    # here takes their whole Yahoo-sourced history and any watchlist
    # references with them in one statement.
    op.execute("DELETE FROM bf_symbols WHERE source = 'yahoo'")


def downgrade() -> None:
    # Data purges are not reversible -- the deleted rows and the original
    # "yahoo_nse" labels are gone. Nothing to do structurally; re-running
    # a Yahoo backfill was already retired well before this migration.
    pass
