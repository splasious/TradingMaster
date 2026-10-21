"""The picture sent with the NIFTY OI-levels alert (21 Oct): NIFTY 50's last 25
15-minute candles and the one forming (notifications/candle_panel.py) with
the nearest expiry's #1 resistance and support drawn across them and its OI
at each strike as a profile on their right -- call OI above NIFTY, put OI
below it, each bar at its strike's price, so where writers are stacked
reads straight off the candles -- and under
them each expiry as a price ladder -- its top resistance strikes (call OI)
above an orange NIFTY line and its top support strikes (put OI) below it, at
their real price levels. A bar's length is the strike's OI; a dotted outline
is its OI at the previous record, so a bar shorter than its outline is OI
unwinding and one past it is OI added. The band between the #1 support and
the #1 resistance is the range.

draw() only draws: oi_levels.py fills the OiChart. Same dark style and the
same one-at-a-time lock as the trade-alert charts (trade_chart.py). Sized
for a phone held upright -- 1280 x 1920, type readable without zooming.
"""

import io
from dataclasses import dataclass, field
from datetime import datetime

from app.services.notifications import candle_panel
from app.services.notifications.candle_panel import MA_COLOUR, NOW_COLOUR, Recent
from app.services.notifications.trade_chart import DRAW_LOCK, MUTED, SURFACE, TEXT, TEXT2

CALL, PUT, BAND, SPOT = "#d03b3b", "#0ca30c", "#3987e5", "#eda100"
PANEL = "#201f1e"
FONT = 10.5
NEAR_LEVELS = 300  # R1 / S1 more than this beyond the candles' range aren't drawn on them
PROFILE_CANDLES = 6  # the OI profile's width, in candles, right of the forming one
PROFILE_REACH = 100  # the candle chart shows at least this far either side of NIFTY when it has a profile


@dataclass
class Bar:
    rank: int  # 1 = the most OI
    strike: float
    oi: float  # lakh
    change: float | None  # lakh, since the previous record
    change_day: float | None  # lakh, since the previous session's last record


@dataclass
class Panel:
    title: str  # "Expiry 19 Oct · today"
    resistance: list[Bar]
    support: list[Bar]
    notes: list[str] = field(default_factory=list)  # "Range 22,850 – 22,900", "▲ Support moved up 22,800 → 22,850"


@dataclass
class StrikeOi:
    strike: float
    option_type: str  # "CE" above NIFTY, "PE" below it
    oi: float  # lakh


@dataclass
class OiChart:
    mark: datetime  # IST
    spot: float
    spot_change: float | None
    pcr: float | None
    panels: list[Panel]
    change_text: str  # "15m": what the outline and the first change are against
    summary: str  # the plain sentence at the bottom
    recent: Recent | None = None  # NIFTY's candles, drawn on top when there are any
    profile: list[StrikeOi] = field(default_factory=list)  # the nearest expiry's OI by strike, beside the candles


def _arrow(value: float | None) -> str:
    if value is None:
        return "—"
    return ("▲" if value > 0 else "▼" if value < 0 else "•") + f"{abs(value):.1f}"


def draw(chart: OiChart) -> bytes:
    if not chart.panels or not any(p.resistance or p.support for p in chart.panels):
        raise ValueError("no OI levels to draw")
    with DRAW_LOCK:
        return _draw(chart)


def _draw(chart: OiChart) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    candles = chart.recent is not None and bool(chart.recent.bars)
    fig = Figure(figsize=(8, 12 if candles else 9), dpi=160, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    top = 0.885 if candles else 0.80
    first = chart.panels[0]

    head = f"NIFTY {chart.spot:,.1f}"
    if chart.spot_change is not None:
        head += f" ({chart.spot_change:+.1f})"
    if chart.pcr is not None:
        head += f"  ·  PCR {chart.pcr:.2f}"
    fig.text(0.05, top + 0.083, f"NIFTY OI levels · {chart.mark:%H:%M}", color=TEXT, fontsize=FONT + 7, fontweight="bold")
    fig.text(0.05, top + 0.055, f"{head}  ·  {chart.mark:%d %b}", color=TEXT2, fontsize=FONT + 1)
    if first.support:
        fig.text(0.95, top + 0.083, f"S {first.support[0].strike:,.0f}", color=PUT, fontsize=FONT + 7, fontweight="bold", ha="right")
    if first.resistance:
        fig.text(0.95 - (0.20 if first.support else 0), top + 0.083, f"R {first.resistance[0].strike:,.0f}", color=CALL,
                 fontsize=FONT + 7, fontweight="bold", ha="right")
    if first.support and first.resistance:
        fig.text(0.95, top + 0.055, f"range {first.support[0].strike:,.0f} – {first.resistance[0].strike:,.0f}", color=TEXT2,
                 fontsize=FONT + 1, ha="right")
    before = "OI at the last record" if chart.change_text == "prev" else f"OI {chart.change_text} ago"
    legend = [("■", "Resistance (call OI)", CALL), ("■", "Support (put OI)", PUT), ("□", before, TEXT2),
              ("━", "NIFTY now", SPOT)]
    if candles:
        legend += [("━", "8-candle MA", MA_COLOUR), ("▯", "candle forming", NOW_COLOUR)]
    for n, (swatch, label, colour) in enumerate(legend):
        row, col = divmod(n, 3)
        x = 0.05 + col * 0.31
        fig.text(x, top + 0.025 - row * 0.02, swatch, color=colour, fontsize=FONT + 1)
        fig.text(x + 0.028, top + 0.025 - row * 0.02, label, color=TEXT2, fontsize=FONT)

    if candles:
        upper = fig.add_gridspec(1, 1, left=0.05, right=0.86, top=top - 0.04, bottom=0.575)
        _candles(fig.add_subplot(upper[0]), chart)
        ladders = fig.add_gridspec(1, len(chart.panels), wspace=0.55, left=0.17, right=0.97, top=0.475, bottom=0.10)
    else:
        ladders = fig.add_gridspec(1, len(chart.panels), wspace=0.55, left=0.17, right=0.97, top=top - 0.11, bottom=0.12)
    for n, panel in enumerate(chart.panels):
        _ladder(fig.add_subplot(ladders[n]), panel, chart)

    fig.text(0.05, 0.05, chart.summary, color=TEXT, fontsize=FONT + 1, fontweight="bold", wrap=True)
    since = "since the last record" if chart.change_text == "prev" else f"in the last {chart.change_text}"
    fig.text(0.05, 0.022, f"OI in lakh · ▲▼ change {since} · day = since the previous close · "
             "pts = distance from NIFTY", color=MUTED, fontsize=FONT - 2)
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=SURFACE)
    return out.getvalue()


def _candles(ax, chart: OiChart) -> None:
    """NIFTY's candles with the nearest expiry's R1 and S1 across them."""
    from matplotlib.ticker import FuncFormatter

    recent = chart.recent
    candle_panel.draw(ax, recent, font=FONT)
    first = chart.panels[0]
    reach = (chart.spot - PROFILE_REACH, chart.spot + PROFILE_REACH) if chart.profile else ()
    lo, hi = candle_panel.price_range(recent, chart.spot, *reach)
    shown = []
    for levels, colour, tag in ((first.resistance, CALL, "R1"), (first.support, PUT, "S1")):
        if levels and lo - NEAR_LEVELS <= levels[0].strike <= hi + NEAR_LEVELS:
            shown.append((levels[0].strike, colour, tag))
    lo, hi = candle_panel.price_range(recent, chart.spot, *reach, *(s for s, _, _ in shown))
    ax.set_ylim(lo, hi)
    if chart.profile:
        _profile(ax, chart, lo, hi)
    for strike, colour, tag in shown:
        ax.axhline(strike, color=colour, lw=1.6, ls=(0, (6, 3)), zorder=2)
        ax.text(0.01, strike, f" {tag} {strike:,.0f} ", transform=ax.get_yaxis_transform(), color=SURFACE, fontsize=FONT - 0.5,
                fontweight="bold", va="center", ha="left", zorder=7, bbox=dict(boxstyle="round,pad=0.2", fc=colour, ec=colour))
    near = (hi - lo) * 0.065  # the price labels the NIFTY tag would cover
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: "" if abs(v - chart.spot) < near else f"{v:,.0f}"))
    ax.axhline(chart.spot, color=SPOT, lw=1.0, ls=(0, (1, 2)), zorder=2)
    ax.text(1.0, chart.spot, f" {chart.spot:,.0f}", transform=ax.get_yaxis_transform(), color=SURFACE, fontsize=FONT - 0.5,
            fontweight="bold", va="center", ha="left", zorder=7, clip_on=False, bbox=dict(boxstyle="round,pad=0.2", fc=SPOT, ec=SPOT))
    ax.set_title(f"NIFTY 50 · 15-min · {recent.describe()}", color=TEXT, fontsize=FONT + 2.5, fontweight="bold", loc="left", pad=8)


def _profile(ax, chart: OiChart, lo: float, hi: float) -> None:
    """Each strike's OI as a bar growing left from the chart's right edge, in
    a strip of its own beside the forming candle, labelled in lakh; the
    expiry's top three each side solid and in bold."""
    first = chart.panels[0]
    ranked = {b.strike for b in first.resistance + first.support}
    start = len(chart.recent.bars) + 0.6
    edge = start + PROFILE_CANDLES
    ax.set_xlim(-0.6, edge)
    ax.axvline(start - 0.2, color=MUTED, lw=0.7, ls=(0, (1, 3)), zorder=1)
    ax.text(edge, 1.012, "OI by strike · lakh", transform=ax.get_xaxis_transform(), color=TEXT2, fontsize=FONT - 1,
            ha="right", va="bottom")
    strikes = sorted(s.strike for s in chart.profile)
    step = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=50.0)
    rows = [s for s in chart.profile if lo + step * 0.3 <= s.strike <= hi - step * 0.3]
    top = max([s.oi for s in rows] + [1e-9])
    room = PROFILE_CANDLES - 0.3
    for row in rows:
        length = room * row.oi / top
        bold = row.strike in ranked
        ax.barh(row.strike, length, left=edge - length, height=step * 0.56, color=CALL if row.option_type == "CE" else PUT,
                alpha=0.9 if bold else 0.5, lw=0, zorder=2)
        inside = length > 2.6  # room for the number in the bar; otherwise just left of it
        ax.text(edge - 0.15 if inside else edge - length - 0.15, row.strike, f"{row.oi:.0f}", color=TEXT if inside or bold else TEXT2,
                fontsize=FONT - 1.5, fontweight="bold" if bold else "normal", ha="right", va="center", zorder=6)


def _ladder(ax, panel: Panel, chart: OiChart) -> None:
    ax.set_facecolor(PANEL)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(colors=TEXT2, labelsize=FONT, length=0)
    bars = panel.resistance + panel.support
    prices = [b.strike for b in bars] + [chart.spot]
    lo, hi = min(prices), max(prices)
    pad = max((hi - lo) * 0.12, 25)
    ax.set_ylim(lo - pad, hi + pad)
    height = (hi - lo + 2 * pad) * 0.055
    top = max([max(b.oi, b.oi - (b.change or 0)) for b in bars] + [1e-9])
    ax.set_xlim(0, top * 2.05)
    if panel.resistance and panel.support:
        ax.axhspan(panel.support[0].strike, panel.resistance[0].strike, color=BAND, alpha=0.13, lw=0, zorder=0)

    ticks, labels = [], []
    for levels, colour, tag in ((panel.resistance, CALL, "R"), (panel.support, PUT, "S")):
        for b in levels:
            ax.barh(b.strike, b.oi, height=height, color=colour, alpha=0.95 if b.rank == 1 else 0.7, zorder=3)
            right = b.oi
            if b.change is not None:
                before = b.oi - b.change
                ax.barh(b.strike, before, height=height, fill=False, edgecolor=TEXT2, lw=1.0, ls=(0, (2, 2)), zorder=4)
                right = max(right, before)
            ax.text(right + top * 0.05, b.strike, f"{b.oi:.1f} L\n{_arrow(b.change)} · day {_arrow(b.change_day)}",
                    color=TEXT, fontsize=FONT - 1, va="center", linespacing=1.2, fontweight="bold" if b.rank == 1 else "normal",
                    zorder=5)
            ticks.append(b.strike)
            labels.append(f"{tag}{b.rank}  {b.strike:,.0f}\n{b.strike - chart.spot:+,.0f} pts")
    ax.axhline(chart.spot, color=SPOT, lw=1.8, zorder=6)
    ax.annotate(f"NIFTY {chart.spot:,.0f}", (0, chart.spot), xytext=(4, -5), textcoords="offset points", color=SURFACE,
                fontsize=FONT - 1.5, fontweight="bold", ha="left", va="top", zorder=7,
                bbox=dict(boxstyle="round,pad=0.25", fc=SPOT, ec=SPOT))
    ax.set_yticks(ticks, labels)
    for label, tick in zip(ax.get_yticklabels(), ticks):
        label.set_color(CALL if tick > chart.spot else PUT)
        label.set_fontweight("bold")
    ax.set_xticks([])
    ax.grid(False)
    # Title and notes start over the strike labels, not the bars, so a long
    # note fits the right-hand panel.
    box = ax.get_position()
    x = -min(0.12, box.x0 - 0.05) / box.width
    ax.text(x, 1.012, "\n".join(panel.notes), transform=ax.transAxes, color=TEXT2, fontsize=FONT - 1, va="bottom", linespacing=1.35)
    ax.text(x, 1.012 + 0.045 * len(panel.notes) + 0.012, panel.title, transform=ax.transAxes, color=TEXT, fontsize=FONT + 2.5,
            fontweight="bold", va="bottom")
    if not bars:
        ax.text(0.5, 0.5, "none in the captured strikes", transform=ax.transAxes, color=MUTED, ha="center")
