# Charts in Studio

**Status:** shipped
**Touches:** `studio/charts.py`, `studio/app.py`, `core/indicators.py`

## Why
A scanner match is a claim about a chart. Acting on it means going and
looking at that chart, and doing that in another app means reading numbers
that were computed somewhere else, by somebody else's formula. Studio draws
the chart from the same `add_indicators()` output the rule was evaluated
against, so what is on screen and what fired the alert cannot disagree.

## What is drawn

One price panel plus two stacked sub-panels:

| Panel | Contents |
|---|---|
| Price | Candlesticks, EMA 4 / 12 / 200, VWAP |
| MACD | Histogram, MACD line, signal line, zero line |
| Volume | Volume bars coloured against their moving average, plus that average |

Each overlay toggles independently. **Multi-chart (4)** swaps the single
view for a grid of four compact tiles — two columns on a desktop, one column
on a phone, so each chart is full width rather than a quarter of it. Tiles
drop the legend and subplot titles and carry a single bold symbol label
instead; there is no room for anything else at that size.

The window is the recent region only — 60 bars by default, adjustable to
240. History is still *fetched* to `min_bars() + window` so EMA 200 is warm
at the left edge; it is simply not shown.

## Computed and drawn are different lists

`IndicatorParams.ema_periods` is `(4, 9, 12, 200)`. `CHART_EMA_PERIODS` is
`(4, 12, 200)`.

EMA 9 is the exit line for the above-VWAP scenario — `close < ema_9` closes
the position (see `vwap-trend-strategy.md`). A rule cannot reference a
column that was never calculated, so 9 has to be in the params. But nothing
is decided by looking at it, and a fourth line on the price panel crowds the
three that are read by eye.

So: **never drop a period from `ema_periods` to keep it off the chart.**
Narrow `CHART_EMA_PERIODS` instead. Dropping it from the params makes the
rule reference a missing column, which fails closed — the scenario stops
firing and says nothing.

## Colour

The hues are a stated preference, not a generated scheme:

| Series | Colour |
|---|---|
| EMA 4 | green |
| EMA 12 | red |
| EMA 200 | pink |
| VWAP | purple |
| Volume | blue |
| MACD line / signal | green / red |

Matching the charts already in the trader's head is worth more here than a
generic palette. A period with no chosen colour falls back to the ordinal
ramp — EMAs are one measure at several lookbacks, so a fallback takes a
lightness step of a shared hue rather than an invented new hue.

**Known limitation, accepted deliberately.** Green and red separate cleanly
for normal colour vision (ΔE 33.9) but not for red-green colour blindness
(ΔE 4.1 deutan). Every line therefore also carries its own dash pattern —
EMA 4 solid, EMA 12 dashed, EMA 200 long-dashed, VWAP dotted — so identity
survives when hue does not. This matters even with full colour vision: two
same-weight lines crossing on a phone screen are hard to pull apart by
colour alone.

The volume baseline has its own palette slot rather than borrowing VWAP's
purple. Two panels, two unrelated measures; a shared colour would imply a
relationship that is not there.

Candles use the fixed status good/critical pair, unchanged between light and
dark, because direction is polarity rather than identity — and the candle
body carries the direction independently of colour anyway.

## Layout rules

- **No dual axes, ever.** Price, MACD and volume have unrelated scales.
  Stack a panel; never twin a y-axis. A test asserts one y-axis per panel.
- **The legend belongs to the price panel only.** A shared legend listing
  "MACD" and "Volume MA" beside the EMAs reads as though those were price
  overlays too. Lower panels are direct-labelled at their last point, which
  is the better label regardless — it sits on the line it names.
- Hover is unified across the x position, so one hover answers "what were
  all the values here".

## Done when
- [x] Figures are built by a pure function in `studio/charts.py`, so the
      encoding is testable without a browser.
- [x] Every drawn EMA takes its chosen colour and a distinct dash.
- [x] EMA 9 is present as a column and absent from the figure.
- [x] The legend contains price overlays only.
- [x] Compact tiles drop the legend and label themselves.
- [x] No panel shares an axis with another.
