"""
Chart construction.

Figures are built by a pure function, so the encoding decisions can be
checked without a browser: which traces exist, what colour each carries, and
which panel it lands in.
"""

import numpy as np
import pandas as pd
import pytest

from core.indicators import IndicatorParams, add_indicators
from studio.charts import (
    CHART_EMA_PERIODS,
    DARK,
    EMA_DASHES,
    LIGHT,
    ChartOptions,
    _ema_colors,
    build_chart,
    palette_for,
)


@pytest.fixture
def bars() -> pd.DataFrame:
    n = 300
    rng = np.random.default_rng(5)
    close = 100 + np.cumsum(rng.normal(0, 0.4, n))
    open_ = close * (1 + rng.normal(0, 0.001, n))
    df = pd.DataFrame(
        {"open": open_, "high": np.maximum(open_, close) * 1.002,
         "low": np.minimum(open_, close) * 0.998, "close": close,
         "volume": rng.integers(1000, 9000, n).astype(float)},
        index=pd.date_range("2026-09-02 13:30", periods=n, freq="5min", tz="UTC"),
    )
    return add_indicators(df, IndicatorParams(bar_minutes=5))


def _names(fig):
    return [t.name for t in fig.data]


def _trace(fig, name):
    return next(t for t in fig.data if t.name == name)


# ── What gets drawn ──────────────────────────────────────────────────────────

def test_everything_requested_is_drawn(bars):
    fig = build_chart(bars, "NVDA", ChartOptions(), LIGHT)
    names = _names(fig)
    for expected in ("NVDA", "EMA 4", "EMA 12", "EMA 200", "VWAP",
                     "MACD", "Signal", "Volume", "Volume MA"):
        assert expected in names


def test_ema_9_is_computed_but_never_drawn(bars):
    """It is the exit line for the above-VWAP scenario — a rule reads it, a
    fourth line on the price panel would only crowd the chart."""
    assert "ema_9" in bars.columns
    assert 9 not in CHART_EMA_PERIODS
    assert "EMA 9" not in _names(build_chart(bars, "NVDA", ChartOptions(), LIGHT))


def test_candles_are_candles(bars):
    fig = build_chart(bars, "NVDA", ChartOptions(), LIGHT)
    assert _trace(fig, "NVDA").type == "candlestick"


def test_overlays_can_be_switched_off(bars):
    fig = build_chart(
        bars, "NVDA",
        ChartOptions(ema_periods=(), show_vwap=False, show_macd=False,
                     show_volume=False),
        LIGHT,
    )
    assert _names(fig) == ["NVDA"]


def test_each_panel_can_be_dropped_independently(bars):
    no_macd = build_chart(bars, "X", ChartOptions(show_macd=False), LIGHT)
    assert "MACD" not in _names(no_macd)
    assert "Volume" in _names(no_macd)


# ── Encoding ─────────────────────────────────────────────────────────────────

def test_each_ema_takes_the_colour_it_was_asked_for():
    """These hues are a stated preference, not a generated scheme: 4 green,
    12 red, 200 pink. Matching the charts already in the trader's head is
    worth more here than a generic ramp."""
    for palette in (LIGHT, DARK):
        colours = _ema_colors(CHART_EMA_PERIODS, palette)
        assert colours == {p: palette.ema_colors[p] for p in CHART_EMA_PERIODS}
        assert len(set(colours.values())) == len(CHART_EMA_PERIODS)


def test_emas_differ_in_dash_as_well_as_hue(bars):
    """Green and red separate cleanly for normal colour vision but not for
    red-green colour blindness, so shape carries the identity too."""
    fig = build_chart(bars, "X", ChartOptions(), LIGHT)
    dashes = {p: _trace(fig, f"EMA {p}").line.dash for p in CHART_EMA_PERIODS}
    assert dashes == {p: EMA_DASHES[p] for p in CHART_EMA_PERIODS}
    assert len(set(dashes.values())) == len(CHART_EMA_PERIODS)


def test_an_unchosen_period_falls_back_to_the_ordinal_ramp():
    """A period with no stated colour is still one measure at another
    lookback, so it takes a ramp step rather than an invented hue."""
    colours = _ema_colors((4, 50, 200), LIGHT)
    assert colours[4] == LIGHT.ema_colors[4]
    assert colours[200] == LIGHT.ema_colors[200]
    assert colours[50] in LIGHT.ema_ramp


def test_more_emas_than_ramp_steps_reuse_ends_rather_than_inventing_hues():
    """A generated hue would break the 'same measure' reading the ramp
    exists to create."""
    colours = _ema_colors((5, 20, 50, 100, 150), LIGHT)
    assert set(colours.values()) <= set(LIGHT.ema_ramp)


def test_vwap_is_distinguished_by_dash_as_well_as_hue(bars):
    """Identity never rests on colour alone."""
    fig = build_chart(bars, "X", ChartOptions(), LIGHT)
    assert _trace(fig, "VWAP").line.dash == "dot"
    assert _trace(fig, "VWAP").line.color == LIGHT.vwap


def test_the_volume_baseline_does_not_borrow_the_vwap_hue(bars):
    """Two panels, two unrelated measures — sharing a colour would imply a
    relationship that is not there."""
    fig = build_chart(bars, "X", ChartOptions(), LIGHT)
    assert _trace(fig, "Volume MA").line.color == LIGHT.volume_ma
    assert LIGHT.volume_ma != LIGHT.vwap


def test_candles_use_the_status_pair(bars):
    fig = build_chart(bars, "X", ChartOptions(), LIGHT)
    candle = _trace(fig, "X")
    assert candle.increasing.line.color == LIGHT.up
    assert candle.decreasing.line.color == LIGHT.down


def test_status_colours_are_not_themed(bars):
    """Direction means the same thing in both modes."""
    assert LIGHT.up == DARK.up
    assert LIGHT.down == DARK.down


def test_volume_bars_are_coloured_against_their_baseline(bars):
    fig = build_chart(bars, "X", ChartOptions(), LIGHT)
    colours = set(_trace(fig, "Volume").marker.color)
    assert colours == {LIGHT.volume, LIGHT.volume_quiet}


# ── Legend scoping ───────────────────────────────────────────────────────────

def test_only_price_overlays_appear_in_the_legend(bars):
    """A shared legend listing MACD next to the EMAs reads as though it were
    a price overlay too."""
    fig = build_chart(bars, "X", ChartOptions(), LIGHT)
    legended = {t.name for t in fig.data if t.showlegend}
    assert legended == {"EMA 4", "EMA 12", "EMA 200", "VWAP"}


def test_lower_panels_are_labelled_on_the_chart_instead(bars):
    fig = build_chart(bars, "X", ChartOptions(), LIGHT)
    labels = {a.text for a in fig.layout.annotations}
    for expected in ("MACD", "Signal", "Vol MA"):
        assert expected in labels


# ── Compact mode ─────────────────────────────────────────────────────────────

def test_compact_drops_the_legend_and_names_itself(bars):
    """A tile in a grid has no room for a legend; one bold label carries its
    identity."""
    fig = build_chart(bars, "TSLA", ChartOptions(compact=True), LIGHT)
    assert fig.layout.showlegend is False
    assert any("TSLA" in (a.text or "") for a in fig.layout.annotations)


def test_compact_is_shorter(bars):
    tall = build_chart(bars, "X", ChartOptions(), LIGHT).layout.height
    short = build_chart(bars, "X", ChartOptions(compact=True), LIGHT).layout.height
    assert short < tall


# ── The visible window ───────────────────────────────────────────────────────

def test_only_the_recent_region_is_drawn(bars):
    """A phone screen cannot usefully show 300 bars."""
    fig = build_chart(bars, "X", ChartOptions(bars=40), LIGHT)
    assert len(_trace(fig, "X").x) == 40


def test_a_short_frame_is_drawn_whole(bars):
    fig = build_chart(bars.tail(10), "X", ChartOptions(bars=60), LIGHT)
    assert len(_trace(fig, "X").x) == 10


# ── Theme ────────────────────────────────────────────────────────────────────

def test_dark_theme_selects_the_dark_palette():
    assert palette_for("dark") is DARK
    assert palette_for("light") is LIGHT
    assert palette_for(None) is LIGHT


def test_dark_palette_paints_its_own_surface(bars):
    fig = build_chart(bars, "X", ChartOptions(), DARK)
    assert fig.layout.paper_bgcolor == DARK.surface
    assert fig.layout.plot_bgcolor == DARK.surface


def test_no_dual_axis_anywhere(bars):
    """Price, MACD and volume are stacked panels, never twinned y-scales."""
    fig = build_chart(bars, "X", ChartOptions(), LIGHT)
    axes = [k for k in fig.layout if k.startswith("yaxis")]
    assert len(axes) == 3, "one y-axis per panel, no overlaying axis"
