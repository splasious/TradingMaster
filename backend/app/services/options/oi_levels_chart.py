"""The picture sent with the NIFTY OI-levels alert (21 Oct): each expiry shown
as a price ladder -- its top resistance strikes (call OI) above an orange
NIFTY line and its top support strikes (put OI) below it, at their real
price levels. A bar's length is the strike's OI; a dotted outline is its OI
at the previous record, so a bar shorter than its outline is OI unwinding
and one past it is OI added. The band between the #1 support and the #1
resistance is the range.

draw() only draws: oi_levels.py fills the OiChart. Same dark style and the
same one-at-a-time lock as the trade-alert charts (trade_chart.py).
"""

import io
from dataclasses import dataclass, field
from datetime import datetime

from app.services.notifications.trade_chart import DRAW_LOCK, MUTED, SURFACE, TEXT, TEXT2

CALL, PUT, BAND, SPOT = "#d03b3b", "#0ca30c", "#3987e5", "#eda100"
PANEL = "#201f1e"


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
class OiChart:
    mark: datetime  # IST
    spot: float
    spot_change: float | None
    pcr: float | None
    panels: list[Panel]
    change_text: str  # "15m": what the outline and the first change are against
    summary: str  # the plain sentence at the bottom


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

    fig = Figure(figsize=(8, 7.4), dpi=150, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    grid = fig.add_gridspec(1, len(chart.panels), wspace=0.42, left=0.115, right=0.97, top=0.75, bottom=0.12)

    first = chart.panels[0]
    head = f"NIFTY {chart.spot:,.1f}"
    if chart.spot_change is not None:
        head += f"  ({chart.spot_change:+.1f})"
    if chart.pcr is not None:
        head += f"   ·   PCR {chart.pcr:.2f}"
    fig.text(0.04, 0.945, f"NIFTY OI levels · {chart.mark:%H:%M}", color=TEXT, fontsize=14, fontweight="bold")
    fig.text(0.04, 0.905, f"{head}   ·   {chart.mark:%d %b}", color=TEXT2, fontsize=9)
    if first.support:
        fig.text(0.97, 0.945, f"S {first.support[0].strike:,.0f}", color=PUT, fontsize=14, fontweight="bold", ha="right")
    if first.resistance:
        fig.text(0.97 - (0.165 if first.support else 0), 0.945, f"R {first.resistance[0].strike:,.0f}", color=CALL,
                 fontsize=14, fontweight="bold", ha="right")
    if first.support and first.resistance:
        fig.text(0.97, 0.905, f"range {first.support[0].strike:,.0f} – {first.resistance[0].strike:,.0f}", color=TEXT2,
                 fontsize=9, ha="right")
    x = 0.04
    for swatch, label, colour in (("■", "Resistance: call OI above NIFTY", CALL), ("■", "Support: put OI below NIFTY", PUT),
                                  ("□", f"outline = OI {chart.change_text} ago", TEXT2), ("━", "NIFTY now", SPOT)):
        fig.text(x, 0.855, swatch, color=colour, fontsize=8.5)
        fig.text(x + 0.02, 0.855, label, color=TEXT2, fontsize=7.5)
        x += 0.02 + 0.0078 * len(label) + 0.03

    for n, panel in enumerate(chart.panels):
        _ladder(fig.add_subplot(grid[n]), panel, chart)

    fig.text(0.04, 0.065, chart.summary, color=TEXT, fontsize=8.5, fontweight="bold")
    fig.text(0.04, 0.03, f"OI in lakh · ▲▼ change in the last {chart.change_text} and since the previous close · "
             "pts = distance from NIFTY", color=MUTED, fontsize=6.5)
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=SURFACE)
    return out.getvalue()


def _ladder(ax, panel: Panel, chart: OiChart) -> None:
    ax.set_facecolor(PANEL)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(colors=TEXT2, labelsize=7.5, length=0)
    bars = panel.resistance + panel.support
    prices = [b.strike for b in bars] + [chart.spot]
    lo, hi = min(prices), max(prices)
    pad = max((hi - lo) * 0.12, 25)
    ax.set_ylim(lo - pad, hi + pad)
    height = (hi - lo + 2 * pad) * 0.055
    top = max([max(b.oi, b.oi - (b.change or 0)) for b in bars] + [1e-9])
    ax.set_xlim(0, top * 1.75)
    if panel.resistance and panel.support:
        ax.axhspan(panel.support[0].strike, panel.resistance[0].strike, color=BAND, alpha=0.13, lw=0, zorder=0)

    ticks, labels = [], []
    for levels, colour, tag in ((panel.resistance, CALL, "R"), (panel.support, PUT, "S")):
        for b in levels:
            ax.barh(b.strike, b.oi, height=height, color=colour, alpha=0.95 if b.rank == 1 else 0.7, zorder=3)
            right = b.oi
            if b.change is not None:
                before = b.oi - b.change
                ax.barh(b.strike, before, height=height, fill=False, edgecolor=TEXT2, lw=0.9, ls=(0, (2, 2)), zorder=4)
                right = max(right, before)
            ax.text(right + top * 0.04, b.strike, f"{b.oi:.1f} L\n{_arrow(b.change)} {chart.change_text} · {_arrow(b.change_day)} day",
                    color=TEXT, fontsize=6.8, va="center", linespacing=1.25, fontweight="bold" if b.rank == 1 else "normal",
                    zorder=5)
            ticks.append(b.strike)
            labels.append(f"{tag}{b.rank}  {b.strike:,.0f}\n{b.strike - chart.spot:+,.0f} pts")
    ax.axhline(chart.spot, color=SPOT, lw=1.6, zorder=6)
    ax.annotate(f"NIFTY {chart.spot:,.0f}", (0, chart.spot), xytext=(4, -5), textcoords="offset points", color=SURFACE,
                fontsize=7, fontweight="bold", ha="left", va="top", zorder=7, bbox=dict(boxstyle="round,pad=0.25", fc=SPOT, ec=SPOT))
    ax.set_yticks(ticks, labels)
    for label, tick in zip(ax.get_yticklabels(), ticks):
        label.set_color(CALL if tick > chart.spot else PUT)
        label.set_fontweight("bold")
    ax.set_xticks([])
    ax.grid(False)
    ax.set_title(panel.title, color=TEXT, fontsize=10, fontweight="bold", loc="left", pad=12 + 9 * len(panel.notes))
    if panel.notes:
        ax.text(0, 1.012, "\n".join(panel.notes), transform=ax.transAxes, color=TEXT2, fontsize=7, va="bottom", linespacing=1.4)
    if not bars:
        ax.text(0.5, 0.5, "none in the captured strikes", transform=ax.transAxes, color=MUTED, ha="center")
