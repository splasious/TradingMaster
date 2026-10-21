"""NIFTY 50's recent 15-minute candles for the Telegram pictures (21 Oct): the
last RECENT finished candles -- the previous session's too, early in the
day -- and the one forming now, with the strategies' 8-candle MA of
(O+H+L+C)/4, drawn by draw() on a chart axis. Used by the trade alerts
(trade_chart.py) and the OI-levels picture (options/oi_levels_chart.py).

Candles sit side by side whatever the clock says (no overnight gap); a
dotted line marks where a session changes, the date under its first time.
Only finished candles are stored (bar_periods.py), so the forming one is
built from the live price: opened at the previous candle's close (or at the
live price when the session or a candle has just begun), and drawn faded
with a "now" tag.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.market_data import OhlcvCandle
from app.services.broker.zerodha_broker import IST

RECENT = 25
MA_CANDLES = 8
BAR = timedelta(minutes=15)
SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)  # IST

SURFACE, PANEL = "#1a1a19", "#201f1e"
TEXT, TEXT2, MUTED, GRID = "#ffffff", "#c3c2b7", "#8d8c84", "#2c2c2a"
UP, DOWN, MA_COLOUR, NOW_COLOUR = "#0ca30c", "#d03b3b", "#3987e5", "#eda100"


def typical(bar: dict) -> float:
    return (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4


@dataclass
class Recent:
    candles: list[dict]  # finished candles, oldest first: ts (UTC start), open, high, low, close
    ma: list[float | None]  # the MA once each closed
    forming: dict | None = None  # the candle forming now, if there's one

    @property
    def bars(self) -> list[dict]:
        return self.candles + ([self.forming] if self.forming else [])

    def x(self, at: datetime) -> float:
        """Where `at` falls: candle n spans [n, n+1]. A time between sessions
        goes to the next candle's start; before the first, -0.5; after the
        last, its end."""
        starts = [as_aware_utc(b["ts"]) for b in self.bars]
        at = as_aware_utc(at)
        if not starts or at < starts[0]:
            return -0.5
        n = bisect_right(starts, at) - 1
        offset = (at - starts[n]) / BAR
        if offset <= 1:
            return n + offset
        return n + 1.0 if n + 1 >= len(starts) else float(n + 1)

    def last_close_by(self, at: datetime) -> tuple[dict, float | None] | None:
        """The newest candle finished by `at`, and the MA once it closed."""
        done = [(b, m) for b, m in zip(self.candles, self.ma) if as_aware_utc(b["ts"]) + BAR <= as_aware_utc(at)]
        return done[-1] if done else None

    def describe(self) -> str:
        return f"last {len(self.candles)} candles" + (" + now" if self.forming else "")


def with_ma(bars: list[dict]) -> list[float | None]:
    t = [typical(b) for b in bars]
    return [sum(t[i - MA_CANDLES + 1:i + 1]) / MA_CANDLES if i >= MA_CANDLES - 1 else None for i in range(len(bars))]


def period_start(now: datetime) -> datetime | None:
    """The start (UTC) of the 15-minute candle `now` falls in, None outside
    the session."""
    ist = as_aware_utc(now).astimezone(IST)
    if not SESSION_OPEN <= ist.time() < SESSION_CLOSE:
        return None
    opened = ist.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
    return (opened + ((ist - opened) // BAR) * BAR).astimezone(as_aware_utc(now).tzinfo)


def _forming(bars: list[dict], now: datetime, live: float) -> dict | None:
    start = period_start(now)
    if start is None:
        return None
    last = bars[-1] if bars else None
    follows = last is not None and last["ts"] + BAR == start and last["ts"].astimezone(IST).date() == start.astimezone(IST).date()
    opened = last["close"] if follows else live
    return {"ts": start, "open": opened, "high": max(opened, live), "low": min(opened, live), "close": live}


async def load_recent(db, instrument_id, now: datetime, live: float | None = None, n: int = RECENT) -> Recent:
    """The `n` newest candles finished by `now` (with MA_CANDLES more before
    them for the MA) and, when `live` is given during the session, the one
    forming. A stored candle that hadn't finished by `now` is left out: live
    there is none (only finished candles are saved), and for an earlier
    `now` its high, low and close are from later."""
    now = as_aware_utc(now)
    rows = (
        await db.execute(
            select(OhlcvCandle)
            .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == "15m", OhlcvCandle.ts <= now - BAR)
            .order_by(OhlcvCandle.ts.desc())
            .limit(n + MA_CANDLES)
        )
    ).scalars().all()
    bars = [{"ts": as_aware_utc(c.ts), "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in reversed(rows)]
    forming = _forming(bars, now, live) if live is not None else None
    ma = with_ma(bars)
    return Recent(bars[-n:], ma[-n:], forming)


def price_range(recent: Recent, *more: float) -> tuple[float, float]:
    lows = [b["low"] for b in recent.bars] + list(more)
    highs = [b["high"] for b in recent.bars] + list(more)
    pad = max((max(highs) - min(lows)) * 0.10, 15)
    return min(lows) - pad, max(highs) + pad


def draw(ax, recent: Recent, *, font: float = 10.5, label_now: bool = True, axis_times: bool = True) -> None:
    """Candles (up hollow, down filled), the MA line, the forming candle
    faded with a "now" tag, session dividers with the date, time ticks."""
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FuncFormatter

    ax.set_facecolor(PANEL)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=TEXT2, labelsize=font, length=0)
    ax.grid(axis="y", color=GRID, lw=0.7)
    ax.set_axisbelow(True)
    ax.yaxis.tick_right()
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    bars = recent.bars
    ax.set_xlim(-0.6, len(bars) + 0.6)
    for n, bar in enumerate(bars):
        o, h, low, c = bar["open"], bar["high"], bar["low"], bar["close"]
        colour = UP if c >= o else DOWN
        forming = bar is recent.forming
        alpha = 0.45 if forming else 1.0
        ax.plot([n + 0.5, n + 0.5], [low, h], color=colour, lw=1.1, solid_capstyle="round", zorder=2, alpha=alpha)
        body = max(abs(c - o), (h - low) * 0.02, 0.5)
        ax.add_patch(Rectangle((n + 0.18, min(o, c)), 0.64, body, facecolor=PANEL if c >= o else colour, edgecolor=colour,
                               lw=1.2, zorder=3, alpha=alpha, ls=(0, (2, 1.5)) if forming else "-"))
        if forming and label_now:
            ax.annotate("now", (n + 0.5, h), xytext=(0, 11), textcoords="offset points", ha="center", va="bottom",
                        color=NOW_COLOUR, fontsize=font * 0.9, fontweight="bold", zorder=6)
    line = [(n + 1, m) for n, m in enumerate(recent.ma) if m is not None]
    if line:
        ax.plot(*zip(*line), color=MA_COLOUR, lw=2.0, zorder=4)

    for n, _ in session_dividers(recent):
        ax.axvline(n, color=MUTED, lw=0.9, ls=(0, (2, 3)), zorder=1)
    ticks, labels = time_ticks(recent)
    ax.set_xticks(ticks, labels if axis_times else [])


def session_dividers(recent: Recent) -> list[tuple[int, object]]:
    """(x, the new session's date) where the session changes."""
    out = []
    bars = recent.bars
    for n in range(1, len(bars)):
        day = bars[n]["ts"].astimezone(IST).date()
        if day != bars[n - 1]["ts"].astimezone(IST).date():
            out.append((n, day))
    return out


def time_ticks(recent: Recent) -> tuple[list[float], list[str]]:
    """09:15 and every other hour after it (11:15, 13:15, 15:15) -- not one
    just before a session opens -- and the newest candle unless one is close
    by. With more than one session shown, each session's first time gets its
    date under it."""
    bars = recent.bars
    opens = [n for n, _ in session_dividers(recent)]
    ticks: list[int] = []
    for n, bar in enumerate(bars):
        ist = bar["ts"].astimezone(IST)
        if ist.minute == 15 and (ist.hour - 9) % 2 == 0 and not any(0 < o - n < 3 for o in opens):
            ticks.append(n)
    if opens and opens[0] >= 3 and not any(n < opens[0] for n in ticks):
        ticks.insert(0, 0)  # the earlier session would show no time at all
    if bars and (not ticks or len(bars) - 1 - ticks[-1] >= 3):
        ticks.append(len(bars) - 1)
    labels = []
    dated: set[object] = set()
    for n in ticks:
        ist = bars[n]["ts"].astimezone(IST)
        label = f"{ist:%H:%M}"
        if opens and ist.date() not in dated:
            dated.add(ist.date())
            label += f"\n{ist:%d %b}"
        labels.append(label)
    return [n + 0.5 for n in ticks], labels
