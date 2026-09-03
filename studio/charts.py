"""
Chart building for Studio.

Kept out of `app.py` so the figures can be built and checked without a
Streamlit runtime, and so the encoding decisions live somewhere they can be
read rather than being buried in UI code.

**Encoding decisions, and why:**

- **EMAs are one measure at three lookbacks, not three unrelated series.**
  They get an *ordinal* single-hue ramp — short is light, long is dark — so
  the chart says "same thing, different speeds" before you read the legend.
  Four categorical hues would have said the opposite, and would also have
  failed the palette's all-pairs separation floors.
- **VWAP is a different measure**, so it takes a contrasting hue *and* a
  dashed stroke. The dash is deliberate: identity never rests on colour
  alone.
- **Candles use the status good/critical pair.** Direction is polarity, not
  identity, and the candle body itself carries the direction independently of
  colour.
- **No dual axes anywhere.** Price, MACD and volume have unrelated scales, so
  they are separate stacked panels rather than twinned axes.
- **The legend belongs to the price panel only.** A shared legend listing
  "MACD" and "Volume MA" next to the EMAs reads as though those were price
  overlays too, and two dotted orange entries in one legend are
  indistinguishable. Lower panels are direct-labelled at their last point
  instead — which is also the better label anyway, since it sits on the line
  it names.

Palettes are the validated reference instance: the EMA ramp passes the
ordinal checks in both modes, and the categorical hues are drawn from the
documented slot order.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from core.indicators import (
    COL_MACD,
    COL_MACD_HIST,
    COL_MACD_SIGNAL,
    COL_VOL_SMA,
    COL_VWAP,
    ema_column,
)


@dataclass(frozen=True)
class Palette:
    surface: str
    text: str
    muted: str
    grid: str
    up: str
    down: str
    #: Ordinal ramp for EMAs, light to dark. Short period gets the light end.
    ema_ramp: Sequence[str]
    vwap: str
    macd: str
    macd_signal: str
    volume: str
    volume_quiet: str


# Status good/critical are fixed and never themed.
UP = "#0ca30c"
DOWN = "#d03b3b"

LIGHT = Palette(
    surface="#fcfcfb", text="#0b0b0b", muted="#52514e",
    grid="rgba(11,11,11,0.08)",
    up=UP, down=DOWN,
    ema_ramp=("#86b6ef", "#3987e5", "#184f95"),   # ordinal, validated light
    vwap="#eb6834", macd="#2a78d6", macd_signal="#eb6834",
    volume="#2a78d6", volume_quiet="rgba(82,81,78,0.35)",
)

DARK = Palette(
    surface="#1a1a19", text="#ffffff", muted="#c3c2b7",
    grid="rgba(255,255,255,0.08)",
    up=UP, down=DOWN,
    ema_ramp=("#9ec5f4", "#5598e7", "#1c5cab"),   # ordinal, validated dark
    vwap="#d95926", macd="#3987e5", macd_signal="#d95926",
    volume="#3987e5", volume_quiet="rgba(195,194,183,0.35)",
)


def palette_for(theme: Optional[str]) -> Palette:
    return DARK if (theme or "").lower() == "dark" else LIGHT


@dataclass
class ChartOptions:
    """Which overlays and panels to draw."""

    ema_periods: Sequence[int] = ()
    show_vwap: bool = True
    show_macd: bool = True
    show_volume: bool = True
    #: Most recent N bars. The point of a chart on a phone is the current
    #: region, not six months of history squeezed into 300 pixels.
    bars: int = 60
    compact: bool = False
    annotations: Dict[str, str] = field(default_factory=dict)


def _ema_colors(periods: Sequence[int], palette: Palette) -> Dict[int, str]:
    """
    Map EMA periods onto the ordinal ramp, shortest to lightest.

    With more periods than ramp steps the ends repeat rather than inventing
    hues — a generated hue would break the "same measure" reading the ramp
    exists to create.
    """
    ordered = sorted(periods)
    ramp = list(palette.ema_ramp)
    if not ordered:
        return {}
    if len(ordered) == 1:
        return {ordered[0]: ramp[len(ramp) // 2]}
    step = (len(ramp) - 1) / (len(ordered) - 1)
    return {p: ramp[min(len(ramp) - 1, round(i * step))]
            for i, p in enumerate(ordered)}


def _recent(df: pd.DataFrame, bars: int) -> pd.DataFrame:
    return df.tail(bars) if bars and len(df) > bars else df


def _label_last(fig, x, series: pd.Series, text: str, colour: str, row: int) -> None:
    """
    Name a line at its own last point.

    A label on the line beats a legend entry off to one side, and it is the
    only way a stacked-panel chart can identify lower-panel series without a
    shared legend implying they all belong to the price panel.
    """
    valid = series.dropna()
    if valid.empty:
        return
    fig.add_annotation(
        x=x[len(series) - 1], y=valid.iloc[-1], row=row, col=1,
        text=text, showarrow=False, xanchor="left", xshift=4,
        font=dict(color=colour, size=10),
    )


def build_chart(
    df: pd.DataFrame,
    symbol: str,
    options: ChartOptions,
    palette: Palette = LIGHT,
) -> go.Figure:
    """
    Candles with overlays, MACD, and volume — as stacked panels.

    `df` must already carry the indicator columns; this draws, it does not
    compute, so the chart shows exactly what the scanner matched on.
    """
    view = _recent(df, options.bars)
    x = view.index

    rows = 1 + int(options.show_macd) + int(options.show_volume)
    heights = {1: [1.0], 2: [0.72, 0.28], 3: [0.58, 0.22, 0.20]}[rows]

    titles = [f"{symbol}"]
    if options.show_macd:
        titles.append("MACD")
    if options.show_volume:
        titles.append("Volume")

    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        row_heights=heights,
        vertical_spacing=0.05 if options.compact else 0.035,
        subplot_titles=None if options.compact else titles,
    )

    # ── Price ────────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=x, open=view["open"], high=view["high"],
        low=view["low"], close=view["close"],
        name=symbol, showlegend=False,
        increasing_line_color=palette.up, increasing_fillcolor=palette.up,
        decreasing_line_color=palette.down, decreasing_fillcolor=palette.down,
        line_width=1,
    ), row=1, col=1)

    for period, colour in _ema_colors(options.ema_periods, palette).items():
        column = ema_column(period)
        if column not in view.columns:
            continue
        fig.add_trace(go.Scatter(
            x=x, y=view[column], name=f"EMA {period}", showlegend=True,
            line=dict(color=colour, width=2),
            hovertemplate=f"EMA {period}: %{{y:.4f}}<extra></extra>",
        ), row=1, col=1)

    if options.show_vwap and COL_VWAP in view.columns:
        fig.add_trace(go.Scatter(
            x=x, y=view[COL_VWAP], name="VWAP", showlegend=True,
            line=dict(color=palette.vwap, width=2, dash="dash"),
            hovertemplate="VWAP: %{y:.4f}<extra></extra>",
        ), row=1, col=1)

    # ── MACD ─────────────────────────────────────────────────────────────
    row = 2
    if options.show_macd and COL_MACD in view.columns:
        hist = view[COL_MACD_HIST]
        fig.add_trace(go.Bar(
            x=x, y=hist, name="Histogram", showlegend=False, opacity=0.5,
            marker_color=[palette.up if h >= 0 else palette.down
                          for h in hist.fillna(0)],
            hovertemplate="Hist: %{y:.4f}<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=x, y=view[COL_MACD], name="MACD", showlegend=False,
            line=dict(color=palette.macd, width=2),
            hovertemplate="MACD: %{y:.4f}<extra></extra>",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=x, y=view[COL_MACD_SIGNAL], name="Signal", showlegend=False,
            line=dict(color=palette.macd_signal, width=2, dash="dot"),
            hovertemplate="Signal: %{y:.4f}<extra></extra>",
        ), row=row, col=1)
        fig.add_hline(y=0, line_width=1, line_color=palette.grid,
                      row=row, col=1)
        if not options.compact:
            _label_last(fig, x, view[COL_MACD], "MACD", palette.macd, row)
            _label_last(fig, x, view[COL_MACD_SIGNAL], "Signal",
                        palette.macd_signal, row)
        row += 1

    # ── Volume ───────────────────────────────────────────────────────────
    if options.show_volume:
        baseline = view.get(COL_VOL_SMA)
        colours = (
            [palette.volume if v > a else palette.volume_quiet
             for v, a in zip(view["volume"], baseline)]
            if baseline is not None else palette.volume
        )
        fig.add_trace(go.Bar(
            x=x, y=view["volume"], name="Volume", showlegend=False,
            marker_color=colours,
            hovertemplate="Vol: %{y:,.0f}<extra></extra>",
        ), row=row, col=1)
        if baseline is not None:
            fig.add_trace(go.Scatter(
                x=x, y=baseline, name="Volume MA", showlegend=False,
                line=dict(color=palette.vwap, width=2, dash="dot"),
                hovertemplate="Vol MA: %{y:,.0f}<extra></extra>",
            ), row=row, col=1)
            if not options.compact:
                _label_last(fig, x, baseline, "Vol MA", palette.vwap, row)

    fig.update_layout(
        height=320 if options.compact else 760,
        margin=dict(l=8, r=8, t=28 if options.compact else 44, b=8),
        xaxis_rangeslider_visible=False,
        plot_bgcolor=palette.surface, paper_bgcolor=palette.surface,
        font=dict(color=palette.text, size=10 if options.compact else 12),
        # Crosshair + unified tooltip: an interactive chart should answer
        # "what were all the values here" in one hover.
        hovermode="x unified",
        showlegend=not options.compact,
        legend=dict(orientation="h", y=1.02, x=0, font=dict(size=11)),
        barmode="overlay",
        dragmode="pan",
    )
    fig.update_yaxes(showgrid=True, gridcolor=palette.grid,
                     zeroline=False, tickfont=dict(color=palette.muted))
    fig.update_xaxes(showgrid=False, tickfont=dict(color=palette.muted),
                     rangeslider_visible=False)
    # Leave room on the right for the direct labels.
    if not options.compact:
        fig.update_xaxes(domain=[0.0, 0.94])

    if options.compact:
        # A tile in a grid gets its identity from one bold label, not a
        # legend it has no room for.
        fig.add_annotation(
            x=0, y=1.06, xref="paper", yref="paper", showarrow=False,
            text=f"<b>{symbol}</b>  {options.annotations.get(symbol, '')}",
            font=dict(color=palette.text, size=13), align="left",
        )
    return fig
