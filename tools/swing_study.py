"""
SPCX swing study: is this worth day trading, and with what bracket?

Pulls 30 trading days of 1-minute bars from Alpaca's full SIP tape (free
historically, as long as the request ends 15+ minutes ago) and answers
four questions, in the order they matter:

  1. SWINGS    How often does it move, and how far? A stock that drifts
               0.2% a day cannot pay for a 1% target, however good the
               signal is.

  2. CLOCK     When do the moves happen? If everything is over by 10:30,
               the afternoon is not worth watching.

  3. SIGNALS   After each buy signal, what did price actually do? How far
               it went the right way before going the wrong way (MFE) and
               vice versa (MAE) is what sizes a stop and a target.

  4. BRACKETS  Every stop/target pair scored on real bars -- including
               the 1%/2% you planned -- against a baseline of entering at
               random. A bracket that wins on signals but wins just as
               often on random entries means the signal added nothing.

READ-ONLY. Market-data client only, inherited from feed_check. There is
no trading client anywhere in these files.

Why it imports from feed_check
------------------------------
The MACD, VWAP, volume and buy-condition code lives in feed_check.py and
is imported, never copied. If this study measured signals computed one
way and the live alert fired on signals computed another, the numbers
here would not describe the thing you actually trade -- and nothing would
catch the drift. Keep both files in the same folder.

Usage
-----
    python swing_study.py                  # 30 days, MACD 9,17,6
    python swing_study.py --days 60
    python swing_study.py --macd 12,26,9
    python swing_study.py --symbol AAPL
    python swing_study.py --self-test      # verify the math, no network

Setup
-----
    pip install alpaca-py pandas python-dotenv
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

try:
    import lockups
except ImportError:            # noqa: F401 -- the calendar is a convenience
    lockups = None             # a study is still a study without unlock dates
from feed_check import (
    CONDITIONS,
    DEFAULT_MACD,
    ET,
    SESSION_OPEN,
    Macd,
    add_conditions,
    fetch_extended,
    parse_clock,
    prepare,
    trading_days,
)

# Swing sizes to report. One threshold would hide the answer: a 0.25%
# zigzag finds noise on a trending day, a 1% zigzag finds nothing on a
# quiet one. Three tells you which scale this stock actually moves at.
SWING_THRESHOLDS_PCT = (0.25, 0.5, 1.0)

# Stop and target grids for the bracket search, in percent.
STOP_GRID = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
TARGET_GRID = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)

# Minutes after entry at which to record where price got to.
HORIZONS_MIN = (15, 30, 60)

# An edge smaller than this is noise, not a strategy. A round trip on a
# $155 stock costs roughly a cent or two of spread even commission-free,
# so an edge of a few thousandths of a percent per trade buys nothing.
MIN_EDGE_PCT = 0.05

#: Below this a cell is not scored at all. Twenty trades is already thin;
#: fewer is a number with no business being compared to anything.
MIN_TRADES = 20

# The baseline samples every Nth bar rather than all 390. Entering at
# every minute of 30 days is ~11,700 trades per grid cell, which is slow
# and no more informative than a fifth of them.
BASELINE_STRIDE = 5

# Candidate trading windows, for the time-of-day section. SPCX moves
# roughly three times as much per bar at the open as it does at 14:00,
# while the signal fires at a flat rate all day -- so most alerts arrive
# when there is least to act on. These ask whether that is worth gating.
GATE_WINDOWS = (
    ("09:40-11:00", ((time(9, 40), time(11, 0)),)),
    ("09:40-10:30", ((time(9, 40), time(10, 30)),)),
    ("15:00-16:00", ((time(15, 0), time(16, 0)),)),
    ("09:40-11:00 + 15:00-16:00", ((time(9, 40), time(11, 0)),
                                   (time(15, 0), time(16, 0)))),
    ("all day (now)", ((time(0, 0), time(23, 59)),)),
)

# --------------------------------------------------------------------------
# The pre-registered VWAP hypothesis
# --------------------------------------------------------------------------
# Written down on 24 September 2026, BEFORE being tested on anything but
# the sample that produced it. In the 12 Jun - 22 Sep window, morning
# signals firing above VWAP showed a best-case/worst-case ratio of 1.67
# and finished higher an hour later 58% of the time, against 0.79 and 48%
# for those below. That is one of four cells examined, on 108
# observations, about 1.7 standard errors from a coin flip -- the same
# shape as a 54% figure that had already fooled this project once.
#
# So the rule is fixed here and not adjusted afterwards. A day on or
# after HYPOTHESIS_FROM is in-sample: the idea was found there and it is
# expected to look good, which proves nothing. A day before it has never
# been examined, and is the only place the question can actually be
# answered. Run with --days large enough to reach back past the cutoff.
HYPOTHESIS_FROM = date(2026, 6, 12)
HYPOTHESIS_WINDOW = (time(9, 40), time(11, 0))
HYPOTHESIS_BRACKET = (0.75, 2.00)

# SPCX listed on 12 June 2026, so its whole history IS the sample the VWAP
# idea came from and no earlier days exist to test on -- not "hard to get",
# none. The forward test starts today and takes months.
#
# Meanwhile the argument for the idea was never SPCX-specific: VWAP is a
# line institutions are measured against, so it is a place where behaviour
# changes. If that is true it should show somewhere else. These are liquid
# names with years of history, and the POOLED row is the answer -- testing
# six symbols is six chances at a false positive, and the best of six
# always looks better than it is.
CROSS_SYMBOLS = ("AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "AMD")

# The gate sections score ONE bracket, fixed in advance, rather than
# searching the grid again inside each window. Searching 36 cells per
# window would hand back the best of 180 tries and call it a finding --
# and with this many windows something always looks good. This is the
# bracket Jason planned to trade, scored the same way every time.
GATE_BRACKET = (1.0, 2.0)


# --------------------------------------------------------------------------
# Swings
# --------------------------------------------------------------------------

def find_swings(high: Sequence[float], low: Sequence[float], pct: float):
    """Percentage zigzag: the alternating highs and lows of a session.

    A high is only confirmed once price has fallen `pct` from it, so a
    pivot is never declared from information the moment itself did not
    have. Returns [(index, price, "H" | "L")], oldest first.
    """
    n = len(high)
    if n < 2:
        return []
    threshold = pct / 100.0
    pivots: List[Tuple[int, float, str]] = []
    trend = 0                      # +1 = seeking a high, -1 = seeking a low
    hi_i, hi_p = 0, high[0]
    lo_i, lo_p = 0, low[0]

    for i in range(1, n):
        if high[i] > hi_p:
            hi_i, hi_p = i, high[i]
        if low[i] < lo_p:
            lo_i, lo_p = i, low[i]

        if trend >= 0 and low[i] <= hi_p * (1 - threshold):
            pivots.append((hi_i, hi_p, "H"))
            trend = -1
            lo_i, lo_p = i, low[i]
        elif trend <= 0 and high[i] >= lo_p * (1 + threshold):
            pivots.append((lo_i, lo_p, "L"))
            trend = 1
            hi_i, hi_p = i, high[i]

    return pivots


def swing_legs(pivots) -> Tuple[List[float], List[float]]:
    """Run-ups (low to high) and pullbacks (high to low), each in percent."""
    run_ups, pullbacks = [], []
    for (_, p_from, kind), (_, p_to, _) in zip(pivots, pivots[1:]):
        move = 100.0 * (p_to - p_from) / p_from
        (run_ups if kind == "L" else pullbacks).append(abs(move))
    return run_ups, pullbacks


# --------------------------------------------------------------------------
# Bracket simulation
# --------------------------------------------------------------------------

@dataclass
class Trade:
    entry_index: int
    entry_price: float
    outcome: str        # "target", "stop" or "time"
    return_pct: float
    bars_held: int


def simulate(session: pd.DataFrame, entries: Sequence[int],
             stop_pct: float, target_pct: float) -> List[Trade]:
    """Run one bracket over one session.

    A signal on bar i fills at bar i+1's OPEN -- never at the close of the
    bar that produced it, which would be trading on information the moment
    did not have.

    When a single bar touches both the stop and the target, the stop is
    taken. A 1-minute bar records a high and a low but not their order, so
    the outcome is genuinely unknown; assuming the worse of the two keeps
    the result pessimistic, and a backtest that flatters itself is worse
    than useless.

    Anything still open at 16:00 is closed there. This is day trading; no
    position is carried overnight.
    """
    opens = session["open"].to_numpy()
    highs = session["high"].to_numpy()
    lows = session["low"].to_numpy()
    closes = session["close"].to_numpy()
    n = len(session)
    trades: List[Trade] = []

    for i in entries:
        fill = i + 1
        if fill >= n:
            continue
        entry = float(opens[fill])
        stop = entry * (1 - stop_pct / 100.0)
        target = entry * (1 + target_pct / 100.0)

        outcome, exit_price, held = "time", float(closes[n - 1]), n - 1 - fill
        for j in range(fill, n):
            if lows[j] <= stop:
                outcome, exit_price, held = "stop", stop, j - fill
                break
            if highs[j] >= target:
                outcome, exit_price, held = "target", target, j - fill
                break

        trades.append(Trade(
            entry_index=i,
            entry_price=entry,
            outcome=outcome,
            return_pct=100.0 * (exit_price - entry) / entry,
            bars_held=held,
        ))
    return trades


def score(trades: Sequence[Trade]) -> Dict[str, float]:
    """Win rate and average return per trade -- the number that matters."""
    if not trades:
        return {"n": 0, "win_pct": 0.0, "avg_return": 0.0,
                "target_pct": 0.0, "stop_pct": 0.0, "median_bars": 0.0}
    returns = [t.return_pct for t in trades]
    return {
        "n": len(trades),
        "win_pct": 100.0 * sum(r > 0 for r in returns) / len(returns),
        "avg_return": sum(returns) / len(returns),
        "target_pct": 100.0 * sum(t.outcome == "target" for t in trades) / len(trades),
        "stop_pct": 100.0 * sum(t.outcome == "stop" for t in trades) / len(trades),
        "median_bars": float(pd.Series([t.bars_held for t in trades]).median()),
    }


# --------------------------------------------------------------------------
# Signal outcomes
# --------------------------------------------------------------------------

def signal_outcomes(session: pd.DataFrame, entries: Sequence[int]) -> List[dict]:
    """For each signal: how far price went each way, and where it ended up.

    MFE is the best price reached before the trade would have been closed;
    MAE the worst. They are what a stop and a target should be sized
    from -- a target beyond the typical MFE rarely fills, and a stop
    inside the typical MAE is hit on trades that would have worked.
    """
    opens = session["open"].to_numpy()
    highs = session["high"].to_numpy()
    lows = session["low"].to_numpy()
    closes = session["close"].to_numpy()
    index = session.index
    n = len(session)
    rows = []

    for i in entries:
        fill = i + 1
        if fill >= n:
            continue
        entry = float(opens[fill])
        def at(column, default=float("nan")):
            """Context for the CSV. Absent on a bare OHLCV frame, which the
            outcome maths below does not need."""
            return float(session[column].iloc[i]) if column in session else default

        row = {
            "time": index[i],
            "entry_time": index[fill],
            "entry": entry,
            "macd": at("macd"),
            "volume_ratio": at("volume_ratio"),
            "above_vwap": (bool(session["close"].iloc[i] > session["vwap"].iloc[i])
                           if "vwap" in session else None),
        }
        for minutes in HORIZONS_MIN:
            end = min(fill + minutes, n - 1)
            window_hi = float(highs[fill:end + 1].max())
            window_lo = float(lows[fill:end + 1].min())
            row[f"mfe_{minutes}"] = 100.0 * (window_hi - entry) / entry
            row[f"mae_{minutes}"] = 100.0 * (window_lo - entry) / entry
            row[f"ret_{minutes}"] = 100.0 * (float(closes[end]) - entry) / entry
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def pct(values, q: float) -> float:
    return float(pd.Series(values).quantile(q)) if len(values) else float("nan")


#: How much of the winners' adverse excursion a stop should cover. 88%
#: is where 0.75% landed on the first 90 days; the number is reported
#: rather than assumed, so a changed regime shows up as a changed stop.
STOP_COVERS = 0.88

#: Half-lives to report, in sessions. Short enough to catch a regime
#: change, long enough that the effective sample is still worth reading.
HALF_LIVES = (30, 20, 10)

#: A winner is a signal that finished at least this far up an hour later.
#: The stop is sized on what those trades needed, not on every signal:
#: the all-signal median is dominated by losers and gives a stop wide
#: enough to be useless.
WINNER_PCT = 0.5


# The live alarm's own thresholds, imported rather than copied. A test
# that used different numbers would not be testing the alarm.
try:
    from open_candles import (
        ALARM_VOLUME, PRESSING_HIGH, PRESSING_LOW, PRESSURE_MINUTES,
    )
except ImportError:      # the study still runs without the watcher present
    ALARM_VOLUME, PRESSING_LOW, PRESSING_HIGH, PRESSURE_MINUTES = 1.5, 0.30, 0.70, 5


def slot_volume(sessions: Dict[date, pd.DataFrame]) -> Dict[time, float]:
    """The usual volume for each clock minute, across these sessions.

    Per clock slot, never a rolling average of the day. 09:35 and 14:35
    are different animals, and a rolling baseline turns "is this bar
    busy" into "is it the morning" -- the same reason the live watcher
    builds its baseline this way.
    """
    buckets: Dict[time, List[float]] = {}
    for session in sessions.values():
        for stamp, volume in session["volume"].items():
            buckets.setdefault(stamp.time(), []).append(float(volume))
    return {slot: float(pd.Series(v).median()) for slot, v in buckets.items()}


def lean_series(session: pd.DataFrame,
                window: int = PRESSURE_MINUTES) -> pd.Series:
    """Volume-weighted close position over a rolling window, 0 to 1.

    The same reading the alerts name in words: where in each bar's range
    the close landed, weighted by how much traded in it. A bar with no
    range has no opinion and counts as the middle.
    """
    # .where, not .replace(0, pd.NA): pd.NA turns a float column into an
    # object one, and .rolling() then refuses it. A minute with no range
    # is ordinary in thin pre-market, so this path is not exotic -- it is
    # most mornings.
    span = (session["high"] - session["low"]).astype(float)
    position = ((session["close"] - session["low"]).astype(float)
                / span.where(span > 0)).fillna(0.5)
    volume = session["volume"].astype(float)
    weighted = (position * volume).rolling(window).sum()
    total = volume.rolling(window).sum()
    return (weighted / total.where(total > 0)).fillna(0.5)


def loud(session: pd.DataFrame, usual: Dict[time, float],
         window: int = PRESSURE_MINUTES,
         multiple: float = ALARM_VOLUME) -> pd.Series:
    """Was the last `window` bars' volume unusual for that time of day?"""
    expected = pd.Series(
        [sum(usual.get(t.time(), 0.0) for t in session.index[max(0, i - window + 1):i + 1])
         for i in range(len(session))], index=session.index)
    got = session["volume"].astype(float).rolling(window).sum()
    return (got / expected.where(expected > 0)).fillna(0.0) >= multiple


def lean_entries(session: pd.DataFrame, usual: Dict[time, float]) -> List[int]:
    """Where the live alarm would ring on the buy side: the tape leaning
    past the pressing band with real volume behind it."""
    lean = lean_series(session)
    hot = loud(session, usual)
    return [i for i in range(len(session))
            if lean.iloc[i] >= PRESSING_HIGH and bool(hot.iloc[i])]


def vwap_entries(session: pd.DataFrame, usual: Dict[time, float]) -> List[int]:
    """Where price crosses above VWAP on the same volume condition."""
    if "vwap" not in session:
        return []
    above = session["close"] >= session["vwap"]
    hot = loud(session, usual)
    return [i for i in range(1, len(session))
            if bool(above.iloc[i]) and not bool(above.iloc[i - 1])
            and bool(hot.iloc[i])]


def beat_the_control(sessions: Dict[date, pd.DataFrame],
                     picked: Dict[date, List[int]]):
    """How many stop/target pairs beat entering every fifth bar regardless.

    The count is the measurement. Any one cell can win on a thin sample
    by accident; an edge that depends on the levels is not an edge, so a
    real one shows up across most of the grid.

    Takes entries already chosen rather than a picker, because they do
    not depend on the bracket. Choosing them inside the grid meant doing
    it 36 times per session -- unnoticeable for a lookup on an "alert"
    column, minutes of silence for a trigger that has to compute a
    rolling lean and a per-slot volume baseline first.
    """
    control = {day: list(range(0, len(session) - 1, BASELINE_STRIDE))
               for day, session in sessions.items()}

    beat, cells, best = 0, 0, None
    for stop_pct in STOP_GRID:
        for target_pct in TARGET_GRID:
            sig, base = [], []
            for day, session in sessions.items():
                sig += simulate(session, picked[day], stop_pct, target_pct)
                base += simulate(session, control[day], stop_pct, target_pct)
            ss, bs = score(sig), score(base)
            if ss["n"] < MIN_TRADES or bs["n"] < MIN_TRADES:
                continue
            cells += 1
            edge = ss["avg_return"] - bs["avg_return"]
            if edge > 0:
                beat += 1
            if best is None or edge > best[0]:
                best = (edge, stop_pct, target_pct, ss, bs)
    return beat, cells, best


def winners(outcomes: pd.DataFrame) -> pd.DataFrame:
    """Signals that finished up an hour later -- the ones a stop must not
    have thrown away."""
    if outcomes.empty or "ret_60" not in outcomes:
        return outcomes.iloc[0:0]
    return outcomes[outcomes["ret_60"] >= WINNER_PCT]


def stop_for(rows: pd.DataFrame, covers: float = STOP_COVERS,
             weights: Optional[pd.Series] = None) -> Optional[float]:
    """The stop distance that would have kept `covers` of these winners.

    Their MAE is negative, so the quantile is taken on its magnitude:
    cover 88% of them and the stop is wide enough that only the worst
    12% were shaken out.
    """
    if rows.empty or "mae_60" not in rows:
        return None
    dips = rows["mae_60"].abs()
    if weights is None:
        return float(dips.quantile(covers))
    return weighted_quantile(dips, weights.loc[dips.index], covers)


def weighted_quantile(values: pd.Series, weights: pd.Series,
                      q: float) -> Optional[float]:
    """A quantile where some observations count more than others.

    Sort, walk the cumulative weight, and take the first value at which
    it passes q. No interpolation: with an effective sample this small,
    interpolating between two points would be precision the data has not
    earned.
    """
    frame = pd.DataFrame({"v": values, "w": weights}).dropna().sort_values("v")
    total = frame["w"].sum()
    if not len(frame) or total <= 0:
        return None
    running = frame["w"].cumsum() / total
    hit = frame.loc[running >= q, "v"]
    return float(hit.iloc[0]) if len(hit) else float(frame["v"].iloc[-1])


def recency_weights(days: Sequence[date], half_life: float) -> Dict[date, float]:
    """Exponential weights: the newest session counts 1, and a session
    `half_life` sessions older counts half as much.

    This adds no information -- it discards some. With a 90-session
    history a 20-session half-life leaves an effective sample near 30,
    and small samples in this project have a record of flattering
    themselves. It exists to be compared against the unweighted answer,
    not to replace it.
    """
    order = sorted(days)
    newest = len(order) - 1
    return {day: 0.5 ** ((newest - i) / half_life)
            for i, day in enumerate(order)}


def effective_n(weights: Sequence[float]) -> float:
    """Kish's effective sample size: how many equally-weighted
    observations this weighting is really worth."""
    total = sum(weights)
    squares = sum(w * w for w in weights)
    return (total * total / squares) if squares else 0.0


def report(symbol: str, macd: Macd, sessions: Dict[date, pd.DataFrame],
           setup: str = "") -> pd.DataFrame:
    rule = "=" * 76
    days = sorted(sessions)
    total_bars = sum(len(s) for s in sessions.values())
    print(f"\n{rule}\n  {symbol}  swing study  --  {len(days)} sessions, "
          f"{days[0]:%Y-%m-%d} to {days[-1]:%Y-%m-%d}, MACD {macd}\n{rule}")
    if setup:
        print(f"  {setup}")
    print(f"\n  {total_bars:,} one-minute bars "
          f"({total_bars / len(days):.0f} per session out of 390)")

    # ---- 1. swings ------------------------------------------------------
    print("\n1. SWINGS -- how often it moves, and how far\n")
    print(f"   {'Threshold':<12}{'Swings/day':>12}{'Run-up: med':>14}{'90th':>9}"
          f"{'Pullback: med':>16}{'90th':>9}")
    for threshold in SWING_THRESHOLDS_PCT:
        ups, downs, per_day = [], [], []
        for session in sessions.values():
            pivots = find_swings(session["high"].to_numpy(),
                                 session["low"].to_numpy(), threshold)
            u, d = swing_legs(pivots)
            ups += u
            downs += d
            per_day.append(len(pivots))
        print(f"   {threshold:>5.2f}%     {sum(per_day) / len(per_day):>11.1f}"
              f"{pct(ups, 0.5):>13.2f}%{pct(ups, 0.9):>8.2f}%"
              f"{pct(downs, 0.5):>15.2f}%{pct(downs, 0.9):>8.2f}%")
    print("\n   A swing is only counted once price has retraced the threshold,")
    print("   so nothing here is measured with hindsight the moment lacked.")

    # ---- 2. clock -------------------------------------------------------
    print("\n2. CLOCK -- when the moves and the signals happen\n")
    buckets: Dict[str, Dict[str, float]] = {}
    for session in sessions.values():
        pivots = find_swings(session["high"].to_numpy(), session["low"].to_numpy(), 0.5)
        for i, _, kind in pivots:
            key = f"{session.index[i]:%H}:{'00' if session.index[i].minute < 30 else '30'}"
            buckets.setdefault(key, {"swings": 0, "signals": 0, "range": []})["swings"] += 1
        for i in range(len(session)):
            ts = session.index[i]
            key = f"{ts:%H}:{'00' if ts.minute < 30 else '30'}"
            slot = buckets.setdefault(key, {"swings": 0, "signals": 0, "range": []})
            slot["range"].append(100.0 * (session["high"].iloc[i] - session["low"].iloc[i])
                                 / session["close"].iloc[i])
            if bool(session["alert"].iloc[i]):
                slot["signals"] += 1

    print(f"   {'Half hour':<12}{'Swings':>9}{'Signals':>10}{'Avg bar range':>16}")
    for key in sorted(buckets):
        slot = buckets[key]
        avg_range = sum(slot["range"]) / len(slot["range"]) if slot["range"] else 0.0
        bar = "#" * int(avg_range * 200)
        print(f"   {key:<12}{slot['swings']:>9}{slot['signals']:>10}"
              f"{avg_range:>15.3f}%  {bar}")

    # ---- 3. signals -----------------------------------------------------
    all_rows: List[dict] = []
    signals_per_day = []
    for day, session in sessions.items():
        entries = [i for i in range(len(session)) if bool(session["alert"].iloc[i])]
        signals_per_day.append(len(entries))
        for row in signal_outcomes(session, entries):
            row["date"] = day
            all_rows.append(row)

    outcomes = pd.DataFrame(all_rows)
    print(f"\n3. SIGNALS -- {len(outcomes)} of them, "
          f"{sum(signals_per_day) / len(sessions):.1f} per session\n")

    if outcomes.empty:
        print("   No signals fired. Nothing to measure.")
    else:
        print(f"   {'Horizon':<10}{'Median MFE':>13}{'Median MAE':>13}"
              f"{'Median move':>14}{'Higher after':>15}")
        for minutes in HORIZONS_MIN:
            higher = 100.0 * (outcomes[f"ret_{minutes}"] > 0).mean()
            print(f"   +{minutes:<9}{outcomes[f'mfe_{minutes}'].median():>12.2f}%"
                  f"{outcomes[f'mae_{minutes}'].median():>12.2f}%"
                  f"{outcomes[f'ret_{minutes}'].median():>13.2f}%{higher:>14.0f}%")
        print("\n   MFE is how far it went your way before you would have been out;")
        print("   MAE how far against. A target past the median MFE rarely fills;")
        print("   a stop inside the median MAE is hit on trades that would work.")
        print("\n   'Higher after' at 50% is a coin flip. Meaningfully above 50%")
        print("   is the signal earning its keep.")

    # ---- 4. brackets ----------------------------------------------------
    print("\n4. BRACKETS -- every stop/target pair on real bars\n")

    def grid(entry_picker, label: str):
        print(f"   {label}")
        header = "stop vs target"
        print(f"   {header:<14}" + "".join(f"{t:>10.2f}%" for t in TARGET_GRID))
        best = None
        for stop_pct in STOP_GRID:
            cells = []
            for target_pct in TARGET_GRID:
                trades = []
                for session in sessions.values():
                    trades += simulate(session, entry_picker(session), stop_pct, target_pct)
                s = score(trades)
                cells.append(s["avg_return"])
                if s["n"] >= MIN_TRADES and (best is None or s["avg_return"] > best[0]):
                    best = (s["avg_return"], stop_pct, target_pct, s)
            print(f"   {stop_pct:>6.2f}%       " + "".join(f"{c:>10.3f}" for c in cells))
        return best

    def signal_entries(session):
        return [i for i in range(len(session)) if bool(session["alert"].iloc[i])]

    def baseline_entries(session):
        return list(range(0, len(session) - 1, BASELINE_STRIDE))

    print("   Average % return per trade. Entry fills at the NEXT bar's open;")
    print("   when one bar touches both levels the stop is taken; anything")
    print("   still open at 16:00 is closed there.\n")

    best_signal = grid(signal_entries, "ON SIGNALS")
    print()
    best_base = grid(baseline_entries,
                     f"BASELINE -- entering every {BASELINE_STRIDE}th bar regardless")

    print(f"\n{rule}\n  VERDICT")
    if best_signal is None:
        print("  Too few signals to score a bracket. Try more days.")
    else:
        avg, stop_pct_best, target_pct_best, s = best_signal
        timed_out = 100.0 - s["target_pct"] - s["stop_pct"]
        print(f"  Best bracket on signals : {stop_pct_best:.2f}% stop / "
              f"{target_pct_best:.2f}% target")
        print(f"    {s['n']} trades, {s['win_pct']:.0f}% winners, "
              f"{avg:+.3f}% average, {s['median_bars']:.0f} bars held")
        print(f"    {s['target_pct']:.0f}% hit the target, {s['stop_pct']:.0f}% stopped, "
              f"{timed_out:.0f}% closed at 16:00")

        if timed_out > 50:
            print("\n  WARNING: most trades never reached either level, so this grid")
            print("  is mostly measuring how the stock drifted over the day rather")
            print("  than how the bracket performed. Either the levels are too wide")
            print("  for this stock's daily range, or it does not move enough to")
            print("  trade this way. Look at section 1 before trusting any cell.")

        if best_base:
            edge = avg - best_base[0]
            print(f"\n  Best bracket on random entries: {best_base[0]:+.3f}% average")
            print(f"  Signal edge: {edge:+.3f}% per trade")
            if edge <= 0:
                print("  -> The signal did NOT beat entering at random. Do not build")
                print("     the alert on these conditions; they are not finding")
                print("     anything a coin flip would miss.")
            elif edge < MIN_EDGE_PCT:
                print(f"  -> The edge is under {MIN_EDGE_PCT}% a trade, which is inside")
                print("     the bid-ask spread. That is noise, not a strategy: the")
                print("     signal is picking entries no better than a coin flip.")
            elif avg <= 0:
                print("  -> The signal beats random but still loses money. The")
                print("     conditions have some information; the bracket does not")
                print("     harvest it. Costs would make this worse.")
            else:
                print("  -> The signal beats random AND makes money before costs.")
                print("     Worth building. Check the edge survives commissions and")
                print("     the bid-ask spread before trading it.")
    print(rule)

    # Your planned bracket, scored explicitly, whatever the grid best was.
    if not outcomes.empty:
        trades = []
        for session in sessions.values():
            trades += simulate(session, signal_entries(session), 1.0, 2.0)
        s = score(trades)
        print("\n  Your planned 1% stop / 2% target, on signals:")
        print(f"    {s['n']} trades, {s['win_pct']:.0f}% winners, "
              f"{s['avg_return']:+.3f}% average per trade")
        print(f"    {s['target_pct']:.0f}% hit the target, {s['stop_pct']:.0f}% were "
              f"stopped, {100 - s['target_pct'] - s['stop_pct']:.0f}% closed at 16:00\n")

    # ---- 5. is a signal worth more at some times than others? ----------
    if not outcomes.empty:
        print("\n5. SIGNAL QUALITY BY TIME OF DAY\n")
        print("   Section 2 counts signals. This one asks whether they were any")
        print("   good -- the same measurements as section 3, cut by half hour.\n")
        print(f"   {'Half hour':<12}{'Signals':>9}{'Med MFE':>10}{'Med MAE':>10}"
              f"{'MFE/MAE':>10}{'Higher 60m':>13}")
        stamped = outcomes.copy()
        stamped["slot"] = [f"{t:%H}:{'00' if t.minute < 30 else '30'}"
                           for t in stamped["time"]]
        for slot in sorted(stamped["slot"].unique()):
            part = stamped[stamped["slot"] == slot]
            mfe = part["mfe_60"].median()
            mae = part["mae_60"].median()
            ratio = abs(mfe / mae) if mae else float("nan")
            higher = 100.0 * (part["ret_60"] > 0).mean()
            print(f"   {slot:<12}{len(part):>9}{mfe:>9.2f}%{mae:>9.2f}%"
                  f"{ratio:>10.2f}{higher:>12.0f}%")
        print("\n   MFE/MAE above 1.00 means the typical signal went further your")
        print("   way than against it. At 1.00 the two are the same size, which")
        print("   is what a random walk looks like and what no bracket can fix.")

    # ---- 6. what a time-of-day gate would actually buy -------------------
    print("\n6. A TIME-OF-DAY GATE\n")
    stop_gate, target_gate = GATE_BRACKET
    print(f"   One bracket, {stop_gate:.2f}% stop / {target_gate:.2f}% target, fixed")
    print("   in advance and scored identically in every window -- not a fresh")
    print("   search per window, which would find a winner by trying enough.\n")
    print("   The control matters most here. Restricting to the busiest hours")
    print("   raises returns on its own, because there is more movement to")
    print("   catch; so random entry is restricted to the SAME hours. The last")
    print("   column is the only one that says whether the SIGNAL improved.\n")

    def inside(stamp, spans) -> bool:
        return any(lo <= stamp.time() < hi for lo, hi in spans)

    total_swings = 0
    total_range = 0.0
    swing_slots: List[datetime] = []
    range_rows: List[Tuple[datetime, float]] = []
    for session in sessions.values():
        pivots = find_swings(session["high"].to_numpy(),
                             session["low"].to_numpy(), 0.5)
        for i, _, _ in pivots:
            swing_slots.append(session.index[i])
        for i in range(len(session)):
            span = 100.0 * (session["high"].iloc[i] - session["low"].iloc[i]) \
                / session["close"].iloc[i]
            range_rows.append((session.index[i], span))
            total_range += span
    total_swings = len(swing_slots)

    print(f"   {'Window':<28}{'Signals':>9}{'Swings':>9}{'Movement':>11}"
          f"{'Signal':>9}{'Random':>9}{'Edge':>9}")
    for label, spans in GATE_WINDOWS:
        signal_trades, random_trades = [], []
        for session in sessions.values():
            picks = [i for i in range(len(session))
                     if bool(session["alert"].iloc[i])
                     and inside(session.index[i], spans)]
            signal_trades += simulate(session, picks, stop_gate, target_gate)
            rolls = [i for i in range(0, len(session) - 1, BASELINE_STRIDE)
                     if inside(session.index[i], spans)]
            random_trades += simulate(session, rolls, stop_gate, target_gate)

        sig, rnd = score(signal_trades), score(random_trades)
        kept_swings = sum(1 for stamp in swing_slots if inside(stamp, spans))
        kept_range = sum(span for stamp, span in range_rows if inside(stamp, spans))
        swing_share = (f"{100.0 * kept_swings / total_swings:>7.0f}%"
                       if total_swings else f"{'--':>8}")
        range_share = (f"{100.0 * kept_range / total_range:>9.0f}%"
                       if total_range else f"{'--':>10}")
        # A handful of trades is not a measurement. Say so rather than
        # printing three decimals that invite reading a pattern into six
        # coin flips -- the whole point of this section is to resist that.
        if sig["n"] < 20 or rnd["n"] < 20:
            numbers = f"{'--':>9}{'--':>9}{'too few':>9}"
        else:
            numbers = (f"{sig['avg_return']:>9.3f}{rnd['avg_return']:>9.3f}"
                       f"{sig['avg_return'] - rnd['avg_return']:>9.3f}")
        print(f"   {label:<28}{sig['n']:>9}{swing_share}{range_share}{numbers}")

    print("\n   Signals / Swings / Movement are shares of the whole session kept")
    print("   by the gate. Signal and Random are average % per trade inside the")
    print("   window; Edge is the difference. An edge that is still near zero")
    print("   in every row means the gate cut the noise without finding an")
    print("   edge underneath -- fewer interruptions, not a better entry.")

    # ---- 7. the pre-registered VWAP test --------------------------------
    print("\n7. THE VWAP HYPOTHESIS, TESTED THE ONLY WAY THAT COUNTS\n")
    lo_h, hi_h = HYPOTHESIS_WINDOW
    stop_h, target_h = HYPOTHESIS_BRACKET
    print(f"   Rule, fixed before this ran: a signal between {lo_h:%H:%M} and")
    print(f"   {hi_h:%H:%M} with price above VWAP. {stop_h:.2f}% stop, "
          f"{target_h:.2f}% target.")
    print(f"   Days from {HYPOTHESIS_FROM:%d %b %Y} are where the idea came "
          f"from, so they")
    print("   are expected to look good and prove nothing. Days before it have")
    print("   never been examined. Only that row is evidence.\n")

    def vwap_picks(session, signals_only: bool):
        if "vwap" not in session:
            return []
        picks = []
        for i in range(len(session)):
            stamp = session.index[i]
            if not (lo_h <= stamp.time() < hi_h):
                continue
            close, vwap = session["close"].iloc[i], session["vwap"].iloc[i]
            if not (vwap == vwap and close > vwap):     # NaN-safe
                continue
            if signals_only:
                if bool(session["alert"].iloc[i]):
                    picks.append(i)
            elif i % BASELINE_STRIDE == 0:
                picks.append(i)
        return picks

    groups = {
        "before the sample (never looked at)": [d for d in days if d < HYPOTHESIS_FROM],
        f"from {HYPOTHESIS_FROM:%d %b} (where it came from)":
            [d for d in days if d >= HYPOTHESIS_FROM],
    }
    print(f"   {'Days':<38}{'n':>6}{'Signal':>9}{'Random':>9}{'Edge':>9}")
    for label, group in groups.items():
        sig_trades, rnd_trades = [], []
        for day in group:
            session = sessions[day]
            sig_trades += simulate(session, vwap_picks(session, True),
                                   stop_h, target_h)
            rnd_trades += simulate(session, vwap_picks(session, False),
                                   stop_h, target_h)
        sig, rnd = score(sig_trades), score(rnd_trades)
        if sig["n"] < 20 or rnd["n"] < 20:
            body = f"{sig['n']:>6}{'--':>9}{'--':>9}{'too few':>9}"
        else:
            body = (f"{sig['n']:>6}{sig['avg_return']:>9.3f}"
                    f"{rnd['avg_return']:>9.3f}"
                    f"{sig['avg_return'] - rnd['avg_return']:>9.3f}")
        print(f"   {label:<38}{body}")

    out_of_sample = [d for d in days if d < HYPOTHESIS_FROM]
    if not out_of_sample:
        print(f"\n   No days before {HYPOTHESIS_FROM:%d %b %Y} were fetched, so the")
        print("   test has not actually been run. Increase --days until the first")
        print("   session listed above is earlier than the cutoff; anything else")
        print("   is the idea grading its own homework.")
    else:
        print(f"\n   {len(out_of_sample)} unexamined sessions. An edge under "
              f"{MIN_EDGE_PCT}% is inside")
        print("   the spread and counts as zero however it is signed.")

    # ---- 8. what actually happened on unlock days -----------------------
    dated = [u for u in (lockups.for_symbol(symbol) if lockups else [])
             if u.day and u.day in sessions]
    if dated:
        print("\n8. UNLOCK DAYS\n")
        print(f"   {len(dated)} dated unlock(s) fall inside this window. This is what")
        print("   happened on them. It is not what happens on them: a handful of")
        print("   events is an anecdote, and the honest use of this table is to")
        print("   see whether the days were remarkable at all, not to forecast")
        print("   the next one.\n")

        def day_stats(session):
            first, last = session.iloc[0], session.iloc[-1]
            move = 100.0 * (last["close"] - first["open"]) / first["open"]
            span = 100.0 * (session["high"].max() - session["low"].min()) / first["open"]
            return move, span, float(session["volume"].sum())

        stats = {day: day_stats(session) for day, session in sessions.items()}
        volumes = sorted(v for _, _, v in stats.values())
        typical = volumes[len(volumes) // 2] if volumes else 0.0

        print(f"   {'Date':<12}{'Shares':>10}{'Day move':>11}{'Range':>9}"
              f"{'Volume':>11}{'x typical':>11}")
        for unlock in dated:
            move, span, volume = stats[unlock.day]
            ratio = volume / typical if typical else float("nan")
            print(f"   {unlock.day:%d %b %Y}{unlock.size().replace(' shares', ''):>10}"
                  f"{move:>10.2f}%{span:>8.2f}%"
                  f"{volume / 1_000_000:>10.1f}M{ratio:>11.2f}")

        others = [(m, r, v) for day, (m, r, v) in stats.items()
                  if day not in {u.day for u in dated}]
        if others:
            moves = sorted(m for m, _, _ in others)
            spans = sorted(r for _, r, _ in others)
            mid = len(moves) // 2
            print(f"\n   Every other session here, for comparison ({len(others)} days):")
            print(f"   {'median':<12}{'':>10}{moves[mid]:>10.2f}%{spans[mid]:>8.2f}%")
            down = sum(1 for m in moves if m < 0)
            print(f"   {down} of {len(moves)} were down days "
                  f"({100.0 * down / len(moves):.0f}%), so a fall on any given")
            print("   day is not itself evidence of anything.")
        print("\n   Dates are UNCONFIRMED unless lockups.json says otherwise.")

    # ---- 9. has the stock settled down? ---------------------------------
    print(f"\n{rule}")
    print("9. HAS IT SETTLED DOWN?\n")
    won = winners(outcomes) if not outcomes.empty else outcomes

    if outcomes.empty or won.empty:
        print("   No winning signals to size a stop on.")
    else:
        # A plain split first. If the first stretch is wild and the rest
        # are alike, the answer is not a weighting scheme -- it is that
        # the IPO weeks were a different stock, and saying so is cleaner
        # than burying it in an exponential.
        thirds = max(1, len(days) // 3)
        parts = (("first", days[:thirds]), ("middle", days[thirds:2 * thirds]),
                 ("recent", days[2 * thirds:]))
        print(f"   {'Period':<9}{'Sessions':>10}{'Med range':>12}{'Signals':>9}"
              f"{'Winners':>9}{'Their MAE':>12}{'Stop @88%':>11}")
        for name, span in parts:
            if not span:
                continue
            ranges = [100.0 * (s["high"].max() - s["low"].min()) / s["open"].iloc[0]
                      for d, s in sessions.items() if d in span and len(s)]
            here = outcomes[outcomes["date"].isin(span)]
            hw = winners(here)
            stop = stop_for(hw)
            med_range = pd.Series(ranges).median() if ranges else float("nan")
            dip = hw["mae_60"].abs().median() if len(hw) else float("nan")
            print(f"   {name:<9}{len(span):>10}{med_range:>11.2f}%{len(here):>9}"
                  f"{len(hw):>9}{dip:>11.2f}%"
                  + (f"{stop:>10.2f}%" if stop is not None else f"{'--':>11}"))
        print("\n   'Their MAE' is how far the winners dipped before working.")
        print(f"   'Stop @88%' is the distance that would have kept {STOP_COVERS:.0%}")
        print("   of them. If the recent column is much tighter than the first,")
        print("   the IPO weeks were a different stock. If the last two columns")
        print("   agree, nothing has changed and the whole history is usable.")

        # Then the weighting, alongside the plain answer rather than
        # instead of it.
        flat = stop_for(won)
        print(f"\n   {'Weighting':<22}{'Eff. sessions':>15}{'Stop @88%':>12}")
        print(f"   {'none (all ' + str(len(days)) + ' equal)':<22}"
              f"{len(days):>15}{flat:>11.2f}%")
        for hl in HALF_LIVES:
            if hl >= len(days):
                continue
            w = recency_weights(days, hl)
            per_signal = won["date"].map(w)
            stop = stop_for(won, weights=per_signal)
            eff = effective_n([w[d] for d in days])
            if stop is None:
                continue
            print(f"   {'half-life ' + str(hl) + ' sessions':<22}{eff:>15.0f}"
                  f"{stop:>11.2f}%")
        print("\n   Weighting adds no data -- it discards some. 'Eff. sessions'")
        print("   is what the weighted sample is really worth, and this project")
        print("   has already been fooled once by a result that rested on three")
        print("   days. Treat a move of a few hundredths as noise.")
        print("\n   A stop too wide costs a little on every loss. A stop too")
        print("   tight costs the trades that would have worked. Those are not")
        print("   the same mistake, so tighten only on a difference that is")
        print("   plainly larger than the wobble between these rows.")

    # ---- 10. the recent regime, scored against its own control ----------
    print(f"\n{rule}")
    print("10. THE RECENT REGIME -- does the signal beat chance NOW?\n")
    recent = days[2 * (len(days) // 3):]
    recent_sessions = {d: s for d, s in sessions.items() if d in recent}

    if len(recent_sessions) < 10:
        print("   Too few recent sessions to score. Ask for more days.")
    else:
        macd_entries = {d: [i for i in range(len(ses))
                            if bool(ses["alert"].iloc[i])]
                        for d, ses in recent_sessions.items()}
        beat, cells, best_cell = beat_the_control(recent_sessions, macd_entries)

        if not cells:
            print(f"   No cell had {MIN_TRADES} trades on both sides. Too thin "
                  f"to judge.")
        else:
            share = 100.0 * beat / cells
            print(f"   {len(recent_sessions)} sessions, {cells} bracket "
                  f"combinations with enough trades on both sides.\n")
            print(f"   Cells where the signal beat its own control : "
                  f"{beat} of {cells}  ({share:.0f}%)\n")
            print("   This count is the answer, not the best cell. Search 36")
            print("   brackets on a thin sample and one will look excellent by")
            print("   accident -- that is how a three-day result once passed for")
            print("   an edge here. A real edge shows up as MOST cells beating")
            print("   the control, because it does not depend on the levels.")
            if share >= 70:
                verdict = ("Most cells beat chance. Worth a forward test with "
                           "one bracket fixed in advance.")
            elif share <= 30:
                verdict = ("Most cells LOST to chance. The signal is not "
                           "working in this regime.")
            else:
                verdict = ("About half either way, which is what a coin flip "
                           "looks like. No edge here.")
            print(f"\n   {verdict}")

            edge, sp, tp, ss, bs = best_cell
            print(f"\n   Best of the {cells}, and inflated by being the best "
                  f"of {cells}:")
            print(f"     {sp:.2f}% stop / {tp:.2f}% target")
            print(f"     signal   {ss['avg_return']:+.3f}% over {ss['n']} trades")
            print(f"     control  {bs['avg_return']:+.3f}% over {bs['n']} trades")
            print(f"     edge     {edge:+.3f}% a trade"
                  + ("  -- inside the spread, so it is nothing"
                     if abs(edge) < MIN_EDGE_PCT else ""))
            print("\n   Do not trade that cell because it topped this table.")
            print("   Pick a bracket for reasons that exist before the search,")
            print("   then measure it forward.")

    # ---- 11. the triggers that actually ring the phone ------------------
    print(f"\n{rule}")
    print("11. WHAT THE ALARM ACTUALLY FIRES ON\n")

    if len(recent_sessions) < 10:
        print("   Too few recent sessions to score.")
    else:
        usual = slot_volume(sessions)
        triggers = (
            ("MACD crossover",
             lambda ses: [i for i in range(len(ses)) if bool(ses["alert"].iloc[i])]),
            (f"lean >= {PRESSING_HIGH:.0%} on {ALARM_VOLUME:.1f}x volume",
             lambda ses: lean_entries(ses, usual)),
            (f"VWAP cross up on {ALARM_VOLUME:.1f}x volume",
             lambda ses: vwap_entries(ses, usual)),
        )
        print("   Buy side only -- long-only, so a sell-side trigger is an exit")
        print("   rather than an entry and cannot be scored this way.\n")
        print(f"   {'Trigger':<38}{'Entries':>9}{'Beat control':>15}{'Verdict':>12}")
        for label, picker in triggers:
            picked = {d: picker(ses) for d, ses in recent_sessions.items()}
            fired = sum(len(v) for v in picked.values())
            beat, cells, _ = beat_the_control(recent_sessions, picked)
            if not cells:
                print(f"   {label:<38}{fired:>9}{'too thin':>15}{'--':>12}")
                continue
            share = 100.0 * beat / cells
            verdict = ("edge?" if share >= 70 else
                       "loses" if share <= 30 else "chance")
            print(f"   {label:<38}{fired:>9}"
                  f"{str(beat) + ' of ' + str(cells):>15}{verdict:>12}")

        print("\n   Same grid, same matched control, same count as section 10.")
        print("   Noise scores about a third of the cells; a real edge scores")
        print("   most of them. 'edge?' is a question, not a finding -- it means")
        print("   this is worth pre-registering and measuring forward, not that")
        print("   it has been proven.")
        print("\n   The volume baseline here is the median for each clock minute")
        print("   across all these sessions, which is how the live watcher builds")
        print("   its own. The thresholds are imported from it, so this cannot")
        print("   drift from what actually rings.")

    return outcomes


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _session(closes, highs=None, lows=None, volumes=None, first=SESSION_OPEN):
    n = len(closes)
    start = datetime.combine(date(2026, 9, 18), first, tzinfo=ET)
    idx = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(n)])
    return pd.DataFrame(
        {"open": closes,
         "high": highs if highs is not None else [c + 0.02 for c in closes],
         "low": lows if lows is not None else [c - 0.02 for c in closes],
         "close": closes,
         "volume": volumes if volumes is not None else [1000] * n},
        index=idx,
    )


def self_test() -> int:
    print("Self-test: checking the swing, bracket and outcome logic...\n")
    failures = []

    # A clean triangle: up 10%, down 10%. One high, one low.
    closes = [100 + i for i in range(11)] + [110 - i for i in range(1, 11)]
    pivots = find_swings([c + 0.01 for c in closes], [c - 0.01 for c in closes], 1.0)
    kinds = [k for _, _, k in pivots]
    # The path starts at 100 and rises, so 100 is itself a swing low -- the
    # anchor the run-up is measured from -- and the peak follows it.
    if kinds != ["L", "H"]:
        failures.append(f"expected a low then a high on an up-then-down path, got {kinds}")
    highs_found = [p for _, p, k in pivots if k == "H"]
    if not highs_found or abs(highs_found[0] - 110.01) > 0.05:
        failures.append(f"the high should be ~110, got {highs_found}")
    run_ups, pullbacks = swing_legs(pivots)
    if not run_ups or abs(run_ups[0] - 10.0) > 0.2:
        failures.append(f"the run-up should measure ~10%, got {run_ups}")

    # Noise below the threshold must produce no pivots at all.
    flat = [100 + (0.05 if i % 2 else -0.05) for i in range(60)]
    if find_swings([c + 0.01 for c in flat], [c - 0.01 for c in flat], 1.0):
        failures.append("0.1% noise should not register as a 1% swing")

    # Bracket: a bar that touches both levels must be recorded as a stop.
    both = _session([100.0, 100.0, 100.0], highs=[100.0, 100.0, 103.0],
                    lows=[100.0, 100.0, 98.0])
    trades = simulate(both, [0], stop_pct=1.0, target_pct=2.0)
    if not trades or trades[0].outcome != "stop":
        failures.append(f"a bar touching both levels must be a stop, got "
                        f"{trades[0].outcome if trades else 'nothing'}")

    # Entry fills at the NEXT bar's open, never the signal bar's close.
    gap = _session([100.0, 105.0, 105.0])
    t = simulate(gap, [0], stop_pct=50.0, target_pct=50.0)[0]
    if abs(t.entry_price - 105.0) > 1e-9:
        failures.append(f"entry should fill at the next open (105), got {t.entry_price}")

    # A clean winner and a clean loser.
    up = _session([100.0] + [100.0 + i for i in range(1, 10)],
                  highs=[100.0] + [100.4 + i for i in range(1, 10)])
    t = simulate(up, [0], stop_pct=1.0, target_pct=2.0)[0]
    if t.outcome != "target" or abs(t.return_pct - 2.0) > 1e-6:
        failures.append(f"a clean rally should hit the target for +2%, got "
                        f"{t.outcome} {t.return_pct:.3f}%")

    down = _session([100.0] + [100.0 - i for i in range(1, 10)],
                    lows=[100.0] + [99.6 - i for i in range(1, 10)])
    t = simulate(down, [0], stop_pct=1.0, target_pct=2.0)[0]
    if t.outcome != "stop" or abs(t.return_pct + 1.0) > 1e-6:
        failures.append(f"a clean decline should stop out for -1%, got "
                        f"{t.outcome} {t.return_pct:.3f}%")

    # Never exiting means closing at 16:00, not running forever.
    quiet = _session([100.0] * 30)
    t = simulate(quiet, [0], stop_pct=5.0, target_pct=5.0)[0]
    if t.outcome != "time":
        failures.append(f"an unresolved trade should close at the bell, got {t.outcome}")

    # --- recency weighting -------------------------------------------------
    span = [date(2026, 6, 12) + timedelta(days=i) for i in range(40)]
    w = recency_weights(span, half_life=10)
    if abs(w[span[-1]] - 1.0) > 1e-9:
        failures.append("the newest session should weigh 1")
    if abs(w[span[-11]] - 0.5) > 1e-9:
        failures.append(f"ten sessions back should weigh a half, got {w[span[-11]]}")
    if abs(w[span[-21]] - 0.25) > 1e-9:
        failures.append("twenty back should weigh a quarter")
    if any(w[span[i]] > w[span[i + 1]] for i in range(len(span) - 1)):
        failures.append("weights must rise with recency, never fall")

    # Weighting discards information, and the effective sample says how
    # much. Equal weights must come back as the real count.
    if abs(effective_n([1.0] * 40) - 40) > 1e-9:
        failures.append("equal weights are worth their own count")
    if effective_n([w[d] for d in span]) >= 40:
        failures.append("a weighted sample cannot be worth more than its count")
    if effective_n([]) != 0.0:
        failures.append("nothing weighs nothing")

    # A weighted quantile with equal weights is an ordinary one, and
    # piling weight on the low values must pull it down.
    flat = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    even = pd.Series([1.0] * 10)
    mid = weighted_quantile(flat, even, 0.5)
    if mid is None or not (4.5 <= mid <= 6.0):
        failures.append(f"an evenly weighted median should sit mid-range: {mid}")
    low_heavy = pd.Series([10.0] * 5 + [0.01] * 5)
    pulled = weighted_quantile(flat, low_heavy, 0.5)
    if pulled is None or pulled >= mid:
        failures.append(f"weighting the low half must pull the quantile down: "
                        f"{pulled} vs {mid}")
    if weighted_quantile(flat, pd.Series([0.0] * 10), 0.5) is not None:
        failures.append("weights summing to zero should yield nothing")
    if weighted_quantile(pd.Series([], dtype=float), pd.Series([], dtype=float),
                         0.5) is not None:
        failures.append("no values should yield nothing")

    # The stop is sized on winners only. Including the losers is what
    # produced the 1% that had to be corrected.
    rows = pd.DataFrame({
        "ret_60": [2.0, 1.5, 0.8, -3.0, -4.0],
        "mae_60": [-0.2, -0.4, -0.6, -3.0, -5.0],
    })
    won = winners(rows)
    if len(won) != 3:
        failures.append(f"three of those finished up 0.5%, got {len(won)}")
    tight = stop_for(won)
    loose = stop_for(rows)
    if tight is None or loose is None or tight >= loose:
        failures.append("sizing on winners must give a tighter stop than "
                        "sizing on everything")
    if stop_for(rows.iloc[0:0]) is not None:
        failures.append("no rows should size no stop")

    # MFE and MAE must bracket the realised move.
    rows = signal_outcomes(up, [0])
    if rows:
        r = rows[0]
        if not (r["mae_15"] <= r["ret_15"] <= r["mfe_15"]):
            failures.append("MAE <= return <= MFE was violated")

    # score() on nothing must not divide by zero.
    if score([])["n"] != 0:
        failures.append("score([]) should report zero trades")

    print(f"  Triangle pivots found          : {kinds}")
    print("  Sub-threshold noise pivots     : 0 (expected)")
    print("  Bar touching both levels       : stop (pessimistic, as specified)")
    print("  Entry fill price               : next bar's open")
    print("  Unresolved trade               : closed at 16:00")
    print("  Recency weights                : newest 1.0, one half-life back 0.5")
    print(f"  40 sessions, half-life 10      : worth "
          f"{effective_n([w[d] for d in span]):.0f} equally-weighted")
    print("  Stop sizing                    : winners only, tighter than all")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll checks passed. The math is sound; now run it against real data.")
    return 0


# --------------------------------------------------------------------------

def cross_symbol(symbols: Sequence[str], days: Sequence[date], macd: Macd,
                 earliest: time) -> int:
    """The pre-registered VWAP rule, on symbols it was not invented on.

    Not a test of SPCX -- nothing can be, until time passes. A test of the
    reason the idea was proposed: that VWAP is a line behaviour changes at.
    A mechanism that only exists in the seventy days that suggested it is
    not a mechanism.
    """
    lo, hi = HYPOTHESIS_WINDOW
    stop, target = HYPOTHESIS_BRACKET
    print(f"\n{'=' * 76}")
    print("  THE VWAP RULE, ON SYMBOLS IT WAS NOT FOUND ON")
    print(f"{'=' * 76}")
    print(f"  A signal between {lo:%H:%M} and {hi:%H:%M} with price above VWAP, "
          f"{stop:.2f}% stop,")
    print(f"  {target:.2f}% target. Fixed in advance, identical for every symbol, "
          f"scored")
    print("  against random entry under the same conditions.\n")
    print(f"  {len(days)} trading days per symbol, MACD {macd}, volume "
          f"condition off.\n")

    def picks(session, signals_only: bool):
        if "vwap" not in session:
            return []
        out = []
        for i in range(len(session)):
            stamp = session.index[i]
            if not (lo <= stamp.time() < hi):
                continue
            close, vwap = session["close"].iloc[i], session["vwap"].iloc[i]
            if not (vwap == vwap and close > vwap):
                continue
            if signals_only:
                if bool(session["alert"].iloc[i]):
                    out.append(i)
            elif i % BASELINE_STRIDE == 0:
                out.append(i)
        return out

    print(f"  {'Symbol':<10}{'Days':>7}{'n':>8}{'Signal':>10}{'Random':>10}{'Edge':>10}")
    pooled_signal, pooled_random = [], []
    for symbol in symbols:
        sessions: Dict[date, pd.DataFrame] = {}
        for day in days:
            try:
                raw = fetch_extended(symbol, day, "sip")
            except Exception:  # noqa: BLE001 -- one symbol short is not fatal
                continue
            if raw.empty:
                continue
            session = add_conditions(prepare(raw, macd), False, earliest)
            if not session.empty:
                sessions[day] = session

        sig_trades, rnd_trades = [], []
        for session in sessions.values():
            sig_trades += simulate(session, picks(session, True), stop, target)
            rnd_trades += simulate(session, picks(session, False), stop, target)
        pooled_signal += sig_trades
        pooled_random += rnd_trades

        sig, rnd = score(sig_trades), score(rnd_trades)
        if sig["n"] < 20 or rnd["n"] < 20:
            body = f"{sig['n']:>8}{'--':>10}{'--':>10}{'too few':>10}"
        else:
            body = (f"{sig['n']:>8}{sig['avg_return']:>10.3f}"
                    f"{rnd['avg_return']:>10.3f}"
                    f"{sig['avg_return'] - rnd['avg_return']:>10.3f}")
        print(f"  {symbol:<10}{len(sessions):>7}{body}")

    sig, rnd = score(pooled_signal), score(pooled_random)
    print(f"  {'-' * 53}")
    if sig["n"] < 20 or rnd["n"] < 20:
        print(f"  {'POOLED':<10}{'':>7}{sig['n']:>8}{'--':>10}{'--':>10}"
              f"{'too few':>10}")
        print("\n  Not enough trades anywhere to say anything.")
        return 1

    edge = sig["avg_return"] - rnd["avg_return"]
    print(f"  {'POOLED':<10}{'':>7}{sig['n']:>8}{sig['avg_return']:>10.3f}"
          f"{rnd['avg_return']:>10.3f}{edge:>10.3f}")

    print(f"\n  The POOLED row is the answer. Individual symbols are "
          f"{len(symbols)} chances")
    print("  at a false positive, and one of them looking good is what noise "
          "does.")
    if edge <= 0:
        print(f"\n  -> {edge:+.3f}%. The rule did not beat random entry under "
              f"its own")
        print("     conditions on symbols it was not invented on. The VWAP")
        print("     filter is not a mechanism; it was a pattern in the seventy")
        print("     days that suggested it.")
    elif edge < MIN_EDGE_PCT:
        print(f"\n  -> {edge:+.3f}%, under {MIN_EDGE_PCT}% and therefore inside "
              f"the spread.")
        print("     Real in sign, worth nothing after costs. Not tradeable, and")
        print("     not a reason to change anything.")
    else:
        print(f"\n  -> {edge:+.3f}%, above the {MIN_EDGE_PCT}% noise floor, on "
              f"symbols the")
        print("     idea was not built on. That is the first result in this")
        print("     project to survive a test it could have failed. Worth")
        print("     pursuing -- and still not proof about SPCX, which needs")
        print("     its own forward test.")
    print(f"{'=' * 76}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure SPCX's swings and brackets.")
    parser.add_argument("--symbol", default="SPCX")
    parser.add_argument("--days", type=int, default=30, help="Trading days (default 30)")
    parser.add_argument("--date", help="End on this day, YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--macd", help="fast,slow,signal (default 9,17,6)")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--no-volume", action="store_true",
                        help="Drop condition (d), the volume test")
    parser.add_argument("--from", dest="earliest", default="09:45",
                        help="Earliest signal time, ET (default 09:45; 09:30 is the open)")
    parser.add_argument("--cross-symbol", nargs="?", const=",".join(CROSS_SYMBOLS),
                        metavar="AAPL,MSFT",
                        help="Run the pre-registered VWAP rule on other symbols "
                             "instead of the full study. SPCX listed in June 2026 "
                             "so it has no out-of-sample past; this tests the "
                             "reason the idea was proposed, not the stock. "
                             f"Default set: {','.join(CROSS_SYMBOLS)}")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    macd = Macd.parse(args.macd) if args.macd else DEFAULT_MACD
    end = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else date.today() - timedelta(days=1))
    days = trading_days(end, max(1, args.days))
    symbol = args.symbol.upper()
    require_volume = not args.no_volume
    earliest = parse_clock(args.earliest)
    active = [CONDITIONS[c] for c in CONDITIONS if c != "cond_d_volume" or require_volume]
    print("Conditions: " + ", ".join(active).replace("(e) after 09:45",
                                                     f"(e) after {earliest:%H:%M}"))

    if args.cross_symbol:
        names = [n.strip().upper() for n in args.cross_symbol.split(",") if n.strip()]
        return cross_symbol(names, days, macd, earliest)

    print(f"Fetching {symbol} 1-minute bars for {len(days)} trading days from SIP...")
    sessions: Dict[date, pd.DataFrame] = {}
    empty: List[date] = []
    errors: List[Tuple[date, str]] = []
    shown = 0                       # identical errors already printed
    for n, day in enumerate(days, 1):
        try:
            raw = fetch_extended(symbol, day, "sip")
        except Exception as exc:  # noqa: BLE001 -- the message is the point
            kind = f"{type(exc).__name__}: {exc}"
            errors.append((day, kind))
            # One cause repeated is one fact. Printing it once per day turns
            # the message that matters into a wall nobody reads to the end
            # of -- and a network that is down is down for all of them.
            same = sum(1 for _, k in errors if k == kind)
            if same <= 2:
                print(f"  {day:%Y-%m-%d}  failed: {kind}")
                shown += 1
            elif same == 3:
                print(f"  {day:%Y-%m-%d}  failed: (same again -- further "
                      f"identical failures counted, not printed)")
            continue
        if raw.empty:
            empty.append(day)
            continue
        session = add_conditions(prepare(raw, macd), require_volume, earliest)
        if session.empty:
            empty.append(day)
            continue
        sessions[day] = session
        if n % 5 == 0 or n == len(days):
            print(f"  {n}/{len(days)} days fetched...")

    if errors:
        kinds = {k for _, k in errors}
        print(f"\n  {len(errors)} of {len(days)} days failed to fetch.")
        if len(kinds) == 1:
            print(f"  Every one of them with the same error, so this is one "
                  f"problem and not {len(errors)}:")
            print(f"    {errors[0][1][:150]}")
            if "resolve" in errors[0][1].lower() or "NameResolution" in errors[0][1]:
                print("  That is your machine's name resolution, not Alpaca and "
                      "not this")
                print("  code. Check the network, a VPN, or Tailscale, then run "
                      "it again.")

    if empty:
        # A long unbroken run of empty days is not a run of holidays. It is a
        # symbol that was not trading -- which is a more useful thing to be
        # told, and for a recent listing it is the whole explanation.
        runs, run = [], [empty[0]]
        for previous, day in zip(empty, empty[1:]):
            if (days.index(day) - days.index(previous)) == 1:
                run.append(day)
            else:
                runs.append(run)
                run = [day]
        runs.append(run)
        longest = max(runs, key=len)
        print(f"\n  {len(empty)} of {len(days)} days returned no bars.")
        if len(longest) >= 5:
            print(f"  {longest[0]} to {longest[-1]} is {len(longest)} consecutive "
                  f"sessions,")
            print(f"  which is not a run of holidays -- {symbol} was almost "
                  f"certainly not")
            print("  trading then. For a recent listing that is the whole story.")

    if not sessions:
        print(f"\nNo usable data for {symbol}. Try --symbol AAPL to check the setup.")
        return 1

    setup = ("volume condition OFF" if not require_volume else "volume condition on")
    setup += f" · signals from {earliest:%H:%M}"
    outcomes = report(symbol, macd, sessions, setup)

    if not outcomes.empty:
        path = args.csv or f"{symbol}_signals_{days[0]:%Y%m%d}_{days[-1]:%Y%m%d}.csv"
        outcomes.to_csv(path, index=False)
        print(f"Per-signal outcomes written to {path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
