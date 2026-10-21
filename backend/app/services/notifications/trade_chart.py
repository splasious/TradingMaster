"""The picture sent with a Telegram trade alert (16 Oct; 21 Oct: portrait,
bigger type, the recent candles): NIFTY 50's last 25 finished 15-minute
candles and the one forming now (candle_panel.py), with the strategies'
8-candle MA of (O+H+L+C)/4, the entry (and, once closed, the exit) marked,
the time held shaded green for a profit or red for a loss, and a PCR panel
under it with the strategy's levels.

draw() only draws: the data comes in a ChartSpec (trade_alerts.py fills it).
It runs in a worker thread -- matplotlib's object API with no pyplot state,
one chart at a time -- and returns PNG bytes. A dark background, as most
Telegram users read it in dark mode; up candles hollow and down candles
filled, so they differ without colour too. Sized for a phone held upright:
1280 x 1600, the type large enough to read without zooming.
"""

import io
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.services.notifications import candle_panel
from app.services.notifications.candle_panel import GRID, MA_COLOUR, MUTED, NOW_COLOUR, SURFACE, TEXT, TEXT2, UP, DOWN, Recent

SESSION_OPEN = time(9, 15)
BAR = timedelta(minutes=15)
PCR_COLOUR = "#d95926"
TONES = {"good": "#0ca30c", "bad": "#d03b3b", "neutral": "#3987e5"}
MARKERS = {"bullish": ("^", "▲"), "bearish": ("v", "▼"), "sideways": ("D", "◆")}
FONT = 10.5  # axis and label type; titles scale from it

# One matplotlib chart at a time, app-wide (these and the OI-levels pictures, options/oi_levels_chart.py).
DRAW_LOCK = threading.Lock()


@dataclass
class Mark:
    at: datetime
    price: float
    label: str


@dataclass
class ChartSpec:
    title: str  # "Nifty PCR Futures · trade closed"
    day: date  # the IST day of the alert
    recent: Recent  # the candles drawn
    headline: str  # "▲ Net P&L +28,699" / "▲ Bullish · PCR 1.283"
    tone: str  # the headline's colour: good / bad / neutral
    subtitle: str  # under the headline
    view: str | None = None  # bullish / bearish / sideways: the entry marker
    entry: Mark | None = None
    exit: Mark | None = None
    held_from: datetime | None = None  # shaded from here (the chart's left edge if earlier) ...
    held_to: datetime | None = None  # ... to here, in `held_tone`
    held_tone: str = "good"
    pcr: list[tuple[datetime, float]] = field(default_factory=list)  # (the 15-minute mark, PCR)
    levels: list[tuple[float, str]] = field(default_factory=list)  # (PCR, "1.10 entry")
    footnote: str = "Paper trading"


def draw(spec: ChartSpec) -> bytes:
    if not spec.recent.bars:
        raise ValueError("no candles to draw yet")
    with DRAW_LOCK:
        return _draw(spec)


def _legend(fig, items: list[tuple[str, str, str]], y: float, per_row: int = 3) -> None:
    for n, (swatch, label, colour) in enumerate(items):
        row, col = divmod(n, per_row)
        x = 0.06 + col * 0.31
        fig.text(x, y - row * 0.024, swatch, color=colour, fontsize=FONT + 1)
        fig.text(x + 0.028, y - row * 0.024, label, color=TEXT2, fontsize=FONT)


def _draw(spec: ChartSpec) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FuncFormatter

    recent = spec.recent
    fig = Figure(figsize=(8, 10), dpi=160, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    grid = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.10, left=0.06, right=0.86, top=0.77, bottom=0.075)
    price, panel = fig.add_subplot(grid[0]), fig.add_subplot(grid[1])
    candle_panel.draw(price, recent, font=FONT, axis_times=False)
    panel.set_facecolor(candle_panel.PANEL)
    for spine in panel.spines.values():
        spine.set_visible(False)
    panel.tick_params(colors=TEXT2, labelsize=FONT, length=0)
    panel.grid(axis="y", color=GRID, lw=0.7)
    panel.set_axisbelow(True)
    panel.yaxis.tick_right()
    panel.set_xlim(price.get_xlim())
    panel.set_xticks(*candle_panel.time_ticks(recent))
    for n, _ in candle_panel.session_dividers(recent):
        panel.axvline(n, color=MUTED, lw=0.9, ls=(0, (2, 3)), zorder=1)

    if spec.held_from is not None and spec.held_to is not None:
        start, end = max(recent.x(spec.held_from), 0.0), recent.x(spec.held_to)
        for ax in (price, panel):
            ax.add_patch(Rectangle((start, 0), max(end - start, 0.05), 1, transform=ax.get_xaxis_transform(),
                                   color=TONES[spec.held_tone], alpha=0.12, lw=0, zorder=0))

    # The entry's label sits in a band above the candles and the exit's in
    # one below them, each on a thin line to its mark, so neither covers a
    # candle or runs into the PCR panel.
    box = dict(boxstyle="round,pad=0.4", fc=SURFACE, ec=GRID, lw=0.9)
    n_bars = len(recent.bars)
    entry_x = recent.x(spec.entry.at) if spec.entry is not None else -1.0
    shown_entry = spec.entry is not None and entry_x >= 0
    extra = [m.price for m in (spec.entry if shown_entry else None, spec.exit) if m is not None]
    lo, hi = candle_panel.price_range(recent, *extra)
    above, below = (0.13 if shown_entry else 0.0), (0.17 if spec.exit is not None else 0.0)
    span = (hi - lo) / (1 - above - below)
    price.set_ylim(lo - span * below, hi + span * above)
    on_band = price.get_xaxis_transform()  # x in candles, y in the axis' height
    corner = {"left": 0.0, "center": 0.5, "right": 1.0}

    def side(n: float) -> str:
        return "left" if n < n_bars * 0.25 else "right" if n > n_bars * 0.75 else "center"

    def line(ha: str, top: bool) -> dict:  # from the label's corner over the mark, so it runs straight down or up
        return dict(arrowstyle="-", color=MUTED, lw=0.9, shrinkA=0, shrinkB=7, relpos=(corner[ha], 0.0 if top else 1.0))

    if shown_entry:
        marker = MARKERS.get(spec.view or "", ("^", "▲"))[0]
        price.plot([entry_x], [spec.entry.price], marker=marker, ms=12, color=TEXT, mec=SURFACE, mew=1.6, zorder=6)
        price.annotate(spec.entry.label, (entry_x, spec.entry.price), xytext=(entry_x, 0.975), textcoords=on_band,
                       ha=side(entry_x), va="top", color=TEXT, fontsize=FONT, fontweight="bold", bbox=box,
                       arrowprops=line(side(entry_x), True),
                       zorder=7)
    if spec.exit is not None:
        n = recent.x(spec.exit.at)
        price.plot([n], [spec.exit.price], marker="X", ms=12, color=TEXT, mec=SURFACE, mew=1.6, zorder=6)
        price.annotate(spec.exit.label, (n, spec.exit.price), xytext=(n, 0.025), textcoords=on_band, ha=side(n), va="bottom",
                       color=TEXT, fontsize=FONT, bbox=box, arrowprops=line(side(n), False), zorder=7)

    marks = [(recent.x(at), value) for at, value in spec.pcr if 0 <= recent.x(at) <= n_bars]
    if marks:
        panel.plot(*zip(*marks), color=PCR_COLOUR, lw=2.0, marker="o", ms=3.5, zorder=4)
    values = [v for _, v in marks] + [level for level, _ in spec.levels]
    if values:
        panel.set_ylim(min(values) - 0.08, max(values) + 0.12)
    for level, name in spec.levels:
        panel.axhline(level, color=MUTED, lw=0.9, ls=(0, (4, 3)), zorder=1)
        crowded = any(0 < other - level < 0.08 for other, _ in spec.levels)  # a level just above: label under the line
        panel.text(0.1, level - 0.012 if crowded else level + 0.012, name, color=TEXT2, fontsize=FONT - 1,
                   va="top" if crowded else "bottom")
    panel.text(0.0, 1.04, "PCR (4 expiries)", transform=panel.transAxes, color=PCR_COLOUR, fontsize=FONT, fontweight="bold",
               va="bottom")
    panel.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.1f}"))

    fig.text(0.06, 0.962, spec.title, color=TEXT, fontsize=FONT + 7, fontweight="bold")
    fig.text(0.06, 0.922, spec.headline, color=TONES.get(spec.tone, TEXT), fontsize=FONT + 7, fontweight="bold")
    fig.text(0.06, 0.888, spec.subtitle, color=TEXT2, fontsize=FONT + 0.5)
    fig.text(0.06, 0.862, f"NIFTY 50 · 15-min · {recent.describe()} · {spec.day:%d %b}", color=MUTED,
             fontsize=FONT - 0.5)
    legend = [("□", "up candle", UP), ("■", "down candle", DOWN), ("━", "8-candle MA", MA_COLOUR)]
    if recent.forming is not None:
        legend.append(("▯", "forming now", NOW_COLOUR))
    if spec.entry is not None:
        legend.append((MARKERS.get(spec.view or "", ("^", "▲"))[1], "entry", TEXT))
    if spec.exit is not None:
        legend.append(("✕", "exit", TEXT))
    _legend(fig, legend, 0.825)
    fig.text(0.86, 0.022, spec.footnote, color=MUTED, fontsize=FONT - 1.5, ha="right")

    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=SURFACE)
    return out.getvalue()

