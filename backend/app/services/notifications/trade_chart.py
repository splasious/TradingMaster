"""The picture sent with a Telegram trade alert (16 Oct): NIFTY 50's 15-minute
candles for the session with the strategies' 8-candle MA of (O+H+L+C)/4, the
entry (and, once closed, the exit) marked, the time held shaded green for a
profit or red for a loss, and a PCR panel under it with the strategy's levels.

draw() only draws: the data comes in a ChartSpec (trade_alerts.py fills it).
It runs in a worker thread -- matplotlib's object API with no pyplot state,
one chart at a time -- and returns PNG bytes. A dark background, as most
Telegram users read it in dark mode; up candles hollow and down candles
filled, so they differ without colour too.
"""

import io
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.core.time import as_aware_utc
from app.services.broker.zerodha_broker import IST

SESSION_OPEN = time(9, 15)
SESSION_CANDLES = 25  # 09:15 .. 15:15 starts
BAR = timedelta(minutes=15)

SURFACE = "#1a1a19"
TEXT, TEXT2, MUTED, GRID = "#ffffff", "#c3c2b7", "#8d8c84", "#2c2c2a"
UP, DOWN, MA_COLOUR, PCR_COLOUR = "#0ca30c", "#d03b3b", "#3987e5", "#d95926"
TONES = {"good": "#0ca30c", "bad": "#d03b3b", "neutral": "#3987e5"}
MARKERS = {"bullish": ("^", "▲"), "bearish": ("v", "▼"), "sideways": ("D", "◆")}

_lock = threading.Lock()


@dataclass
class Mark:
    at: datetime
    price: float
    label: str


@dataclass
class ChartSpec:
    title: str  # "Nifty PCR Futures · trade closed"
    day: date  # the IST session drawn
    candles: list[dict]  # the session's finished 15-minute candles, oldest first: ts (UTC start), open, high, low, close
    ma: list[float | None]  # the MA once each of those candles closed
    headline: str  # top right: "▲ Net P&L +28,699" / "▲ Bullish · PCR 1.283"
    tone: str  # the headline's colour: good / bad / neutral
    subtitle: str  # under the headline
    view: str | None = None  # bullish / bearish / sideways: the entry marker
    entry: Mark | None = None
    exit: Mark | None = None
    held_from: datetime | None = None  # shaded from here (the session's start if earlier) ...
    held_to: datetime | None = None  # ... to here, in `held_tone`
    held_tone: str = "good"
    pcr: list[tuple[datetime, float]] = field(default_factory=list)  # (the 15-minute mark, PCR)
    levels: list[tuple[float, str]] = field(default_factory=list)  # (PCR, "1.10 entry")
    footnote: str = "Paper trading"


def _x(spec: ChartSpec, at: datetime) -> float:
    """Candle n spans [n, n+1]; 09:15 IST is 0."""
    start = datetime.combine(spec.day, SESSION_OPEN, tzinfo=IST)
    return (as_aware_utc(at).astimezone(IST) - start) / BAR


def draw(spec: ChartSpec) -> bytes:
    if not spec.candles:
        raise ValueError("no candles in this session yet")
    with _lock:
        return _draw(spec)


def _draw(spec: ChartSpec) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FuncFormatter

    fig = Figure(figsize=(8, 6.2), dpi=150, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    grid = fig.add_gridspec(2, 1, height_ratios=[3.2, 1], hspace=0.08, left=0.075, right=0.86, top=0.80, bottom=0.08)
    price, panel = fig.add_subplot(grid[0]), fig.add_subplot(grid[1])
    for ax in (price, panel):
        ax.set_facecolor(SURFACE)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(colors=TEXT2, labelsize=7.5, length=0)
        ax.grid(axis="y", color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        ax.yaxis.tick_right()
        ax.set_xlim(-0.3, SESSION_CANDLES + 0.3)

    if spec.held_from is not None and spec.held_to is not None:
        start, end = max(_x(spec, spec.held_from), 0.0), _x(spec, spec.held_to)
        for ax in (price, panel):
            ax.add_patch(Rectangle((start, 0), end - start, 1, transform=ax.get_xaxis_transform(),
                                   color=TONES[spec.held_tone], alpha=0.10, lw=0))

    lows, highs = [], []
    for bar in spec.candles:
        n = _x(spec, bar["ts"])
        o, h, low, c = bar["open"], bar["high"], bar["low"], bar["close"]
        colour = UP if c >= o else DOWN
        price.plot([n + 0.5, n + 0.5], [low, h], color=colour, lw=0.9, solid_capstyle="round", zorder=2)
        body = max(abs(c - o), (h - low) * 0.02, 0.5)
        price.add_patch(Rectangle((n + 0.2, min(o, c)), 0.6, body, facecolor=SURFACE if c >= o else colour,
                                  edgecolor=colour, lw=1.0, zorder=3))
        lows.append(low)
        highs.append(h)
    line = [(_x(spec, bar["ts"]) + 1, value) for bar, value in zip(spec.candles, spec.ma) if value is not None]
    if line:
        price.plot(*zip(*line), color=MA_COLOUR, lw=1.6, zorder=4)

    box = dict(boxstyle="round,pad=0.35", fc=SURFACE, ec=GRID, lw=0.8)
    if spec.entry is not None and _x(spec, spec.entry.at) >= 0:
        marker = MARKERS.get(spec.view or "", ("^", "▲"))[0]
        n = _x(spec, spec.entry.at)
        price.plot([n], [spec.entry.price], marker=marker, ms=9, color=TEXT, mec=SURFACE, mew=1.5, zorder=6)
        early = n < 4  # near the left edge: the label goes right of the mark
        price.annotate(spec.entry.label, (n, spec.entry.price), xytext=(14 if early else -14, 22), textcoords="offset points",
                       ha="left" if early else "right", va="bottom", color=TEXT, fontsize=7.5, fontweight="bold", bbox=box, zorder=7)
        lows.append(spec.entry.price)
        highs.append(spec.entry.price)
    if spec.exit is not None:
        n = _x(spec, spec.exit.at)
        price.plot([n], [spec.exit.price], marker="X", ms=9, color=TEXT, mec=SURFACE, mew=1.5, zorder=6)
        right = n > SESSION_CANDLES * 0.7  # late in the day: the label goes left of the mark
        price.annotate(spec.exit.label, (n, spec.exit.price), xytext=(-16 if right else 16, -46), textcoords="offset points",
                       ha="right" if right else "left", va="top", color=TEXT, fontsize=7.5, bbox=box, zorder=7)
        lows.append(spec.exit.price)
        highs.append(spec.exit.price)
    pad = max((max(highs) - min(lows)) * 0.12, 15)
    price.set_ylim(min(lows) - pad, max(highs) + pad)
    price.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))

    marks = [(_x(spec, at), value) for at, value in spec.pcr if 0 <= _x(spec, at) <= SESSION_CANDLES]
    if marks:
        panel.plot(*zip(*marks), color=PCR_COLOUR, lw=1.6, marker="o", ms=2.5, zorder=4)
    values = [v for _, v in marks] + [level for level, _ in spec.levels]
    if values:
        panel.set_ylim(min(values) - 0.08, max(values) + 0.12)
    for level, name in spec.levels:
        panel.axhline(level, color=MUTED, lw=0.8, ls=(0, (4, 3)), zorder=1)
        crowded = any(0 < other - level < 0.08 for other, _ in spec.levels)  # a level just above: label under the line
        panel.text(0.1, level - 0.012 if crowded else level + 0.012, name, color=TEXT2, fontsize=7, va="top" if crowded else "bottom")
    panel.text(0.01, 0.95, "PCR (4 expiries)", transform=panel.transAxes, color=PCR_COLOUR, fontsize=7.5, fontweight="bold", va="top")
    panel.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.1f}"))

    hours = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
    ticks = [(int(t[:2]) * 60 + int(t[3:]) - 555) / 15 for t in hours]
    panel.set_xticks(ticks, hours)
    price.set_xticks(ticks, [])

    fig.text(0.075, 0.935, spec.title, color=TEXT, fontsize=13, fontweight="bold")
    fig.text(0.075, 0.895, f"NIFTY 50 · 15-min · {spec.day:%d %b}", color=TEXT2, fontsize=8)
    fig.text(0.86, 0.935, spec.headline, color=TONES.get(spec.tone, TEXT), fontsize=13, fontweight="bold", ha="right")
    fig.text(0.86, 0.895, spec.subtitle, color=TEXT2, fontsize=8, ha="right")
    legend = [("□", "up candle", UP), ("■", "down candle", DOWN), ("━", "8-candle MA of (O+H+L+C)/4", MA_COLOUR)]
    if spec.entry is not None:
        legend.append((MARKERS.get(spec.view or "", ("^", "▲"))[1], "entry", TEXT))
    if spec.exit is not None:
        legend.append(("✕", "exit", TEXT))
    x = 0.075
    for swatch, label, colour in legend:
        fig.text(x, 0.845, swatch, color=colour, fontsize=8)
        fig.text(x + 0.018, 0.845, label, color=TEXT2, fontsize=7.5)
        x += 0.018 + 0.0082 * len(label) + 0.025
    fig.text(0.86, 0.02, spec.footnote, color=MUTED, fontsize=6.5, ha="right")

    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=SURFACE)
    return out.getvalue()
