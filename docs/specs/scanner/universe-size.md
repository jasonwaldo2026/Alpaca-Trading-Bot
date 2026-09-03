# Scanning the whole market

**Status:** supported, with a real cost
**Touches:** `core/universe.py`, `scanner/cli.py`

## Why unrestricted

Any stock can fit any scenario. Restricting the scanner to large caps means
the oscillator never sees the small caps that actually swing 10% in a day,
and the momentum screen never sees the names it was designed for. So
`all_tradable` — every active, tradable US equity from Alpaca's asset list —
is the default universe on every shipped scenario.

The list is cached to `.alpaca-assets.json` for the day, since it changes
slowly.

## What it costs

Roughly 11,000 symbols. Alpaca takes 100 symbols per bars request, so:

| | per scan |
|---|---|
| Batched requests | ~110 |
| Bars fetched (300 each) | ~3.3 million |

At a 5-minute cadence that is ~21,000 requests a day. Alpaca's basic plan
allows 200 requests a minute, so the rate limit is not the binding
constraint — **wall-clock time is**. A pass this wide will not reliably
finish inside five minutes over REST, so scans overlap or lag.

`universe.warn_if_large()` prints this when a universe exceeds 500 symbols,
so a slow scan is explained rather than mysterious.

## Ways round it, in order of effort

1. **Lengthen the interval.** `--every 15` gives a wide scan room to finish.
   The cost is noticing a setup up to fifteen minutes late.

2. **Two-stage scan.** Sweep the full market on a slow cadence with a cheap
   filter (a single bar per symbol is enough for a price and volume gate),
   then run the full indicator set on the few hundred that pass. This is the
   standard shape for market-wide scanners and is the right next step.

3. **Streaming.** Alpaca offers a websocket feed. Subscribing to the whole
   market and maintaining bars in memory removes the per-scan fetch
   entirely. It is the professional answer and a substantial rewrite: the
   scanner becomes a stateful service rather than a script.

4. **Narrow deliberately.** Not every scenario needs the whole market. A
   scenario can name its own smaller universe, and `--universe` overrides
   all of them.

## Not done

- [ ] Two-stage scanning. Until then, a whole-market scan should use
      `--every 15` or longer.
- [ ] Any filtering of the asset list beyond `tradable`. It includes
      warrants, units, and very thin names that will never fill well. A
      minimum price and minimum average volume filter would cut it
      substantially and is cheap to add once two-stage scanning exists.
