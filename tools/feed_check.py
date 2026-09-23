"""
Feed check: is Alpaca's free IEX data good enough to alert on?

Pulls completed trading days of SPCX 1-minute bars TWICE -- once from the
free IEX feed, once from the full-market SIP tape -- and compares them.

Both requests are historical and end well over 15 minutes ago, which is
what makes SIP readable on Alpaca's free plan. Nothing here costs money.

It answers three questions:

  1. COVERAGE   How many of each day's 390 minutes produced a bar on each
                feed? A minute with no trades yields no bar at all.

  2. NUMBERS    Do MACD, VWAP and the volume ratio agree between feeds?
                A different average is fine. A different SHAPE is not.

  3. SIGNALS    Run the real buy conditions over both feeds. Do they fire
                on the same minutes, and when they disagree, WHICH
                condition is responsible?

Everything runs twice more, once per MACD setting, so a faster setting can
be judged against the standard one on identical data.

READ-ONLY. It uses Alpaca's market data client only. There is no trading
client in this file, no order object, and no way for it to place a trade.

Three deliberate choices about warm-up, each different, each for its own
reason -- see prepare() for why:

  * MACD    warmed on pre-market bars, so it is settled by 09:30 and
            agrees with what DAS draws.
  * VWAP    anchored at 09:30. It is a running average within the
            trading day and resets each morning.
  * Volume  baseline from regular-hours bars only. Averaging across
            pre-market would turn "unusual volume" into "is it 09:30 yet".

Usage
-----
    python feed_check.py                      # last 5 trading days
    python feed_check.py --days 10
    python feed_check.py --date 2026-09-21    # one specific day
    python feed_check.py --macd 9,17,6        # a single setting
    python feed_check.py --symbol AAPL        # sanity-check the setup
    python feed_check.py --self-test          # verify the math, no network

Setup
-----
    pip install alpaca-py pandas python-dotenv

Keys are read from a .env file in the same folder (or the real environment):

    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...

ALPACA_API_SECRET is accepted as an alias. Keys are never printed or logged.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")

SYMBOL = "SPCX"
PREMARKET_OPEN = time(4, 0)   # fetched only to warm the MACD
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
MINUTES_IN_SESSION = 390

# The buy conditions, exactly as specified.
CROSS_LOOKBACK_BARS = 3       # (a) crossed above signal within this many bars
VOLUME_MULTIPLE = 1.2         # (d) bar volume vs the 20-bar average
VOLUME_LOOKBACK_BARS = 20
EARLIEST_ALERT = time(9, 45)  # (e) nothing before this
COOLDOWN_MINUTES = 15         # (e) at most one alert per this many minutes

CONDITIONS = {
    "cond_a_crossed": "(a) crossed above signal",
    "cond_b_rising": "(b) MACD rising",
    "cond_c_diverging": "(c) gap widening",
    "cond_d_volume": "(d) volume >= 1.2x",
    "cond_e_time": "(e) after 09:45",
}


@dataclass(frozen=True)
class Macd:
    """One MACD setting. Frozen so it can label a result and key a cache."""

    fast: int
    slow: int
    signal: int

    @classmethod
    def parse(cls, text: str) -> "Macd":
        try:
            fast, slow, signal = (int(p) for p in text.split(","))
        except ValueError:
            raise SystemExit(f"--macd wants three numbers like 9,17,6 (got {text!r})")
        if not fast < slow:
            raise SystemExit(f"--macd fast must be less than slow (got {fast} and {slow})")
        return cls(fast, slow, signal)

    @property
    def warmup_bars(self) -> int:
        """Bars needed before the values settle. Driven by slow + signal."""
        return self.slow + self.signal

    def __str__(self) -> str:
        return f"{self.fast}/{self.slow}/{self.signal}"


DEFAULT_MACD = Macd(9, 17, 6)      # Jason's setting, chosen from his own chart
BASELINE_MACD = Macd(12, 26, 9)    # the convention, as a yardstick


# --------------------------------------------------------------------------
# Indicators.  One implementation, used for every feed and every setting, so
# a difference in the output is a difference in the DATA and never the math.
# --------------------------------------------------------------------------

def add_macd(frame: pd.DataFrame, macd: Macd) -> pd.DataFrame:
    """The MACD trio on whatever bars are handed in, and nothing else.

    Separate from prepare() so a chart can draw the same numbers a signal
    fired on over a window prepare() would have trimmed away -- the report
    opens at 09:25, five minutes before the session gate. One copy of this
    arithmetic exists and everything reads it, because a chart that
    disagreed with its own alert would be worse than no chart.
    """
    df = frame.copy()
    fast = df["close"].ewm(span=macd.fast, adjust=False).mean()
    slow = df["close"].ewm(span=macd.slow, adjust=False).mean()
    df["macd"] = fast - slow
    df["macd_signal"] = df["macd"].ewm(span=macd.signal, adjust=False).mean()
    df["macd_gap"] = df["macd"] - df["macd_signal"]
    return df


def prepare(raw: pd.DataFrame, macd: Macd) -> pd.DataFrame:
    """Indicators on a full extended-hours frame, trimmed to regular hours.

    The three warm-up choices explained:

    MACD is an exponential average of price and carries across the session
    boundary -- which is what DAS and every other platform draws. Computing
    it from a cold start at 09:30 would leave it unsettled until roughly
    `warmup_bars` minutes in, and would silently disagree with the screen.
    So it is computed over the pre-market bars too, then trimmed away.

    VWAP is the opposite: a running average WITHIN the trading day that
    resets each morning. It is anchored at 09:30, after the trim.

    The volume baseline is regular-hours only. Pre-market volume is a
    fraction of regular volume, so a baseline spanning both would make
    every bar after 09:30 look like unusual volume -- turning "is this bar
    busy" into "is it the open yet".
    """
    df = add_macd(raw, macd)

    # How many bars the MACD actually got to warm up on.
    premarket_bars = int((df.index.time < SESSION_OPEN).sum())

    session = df[(df.index.time >= SESSION_OPEN) & (df.index.time < SESSION_CLOSE)].copy()
    if session.empty:
        return session

    typical = (session["high"] + session["low"] + session["close"]) / 3.0
    cum_volume = session["volume"].cumsum()
    session["vwap"] = (typical * session["volume"]).cumsum() / cum_volume.replace(0, pd.NA)

    avg_volume = session["volume"].rolling(VOLUME_LOOKBACK_BARS).mean()
    session["volume_ratio"] = session["volume"] / avg_volume.replace(0, pd.NA)

    session.attrs["premarket_bars"] = premarket_bars
    session.attrs["macd_warm_at_open"] = premarket_bars >= macd.warmup_bars
    return session


def parse_clock(text: str) -> time:
    """'09:30' -> a time. Rejects anything else loudly."""
    try:
        hh, mm = (int(part) for part in text.split(":"))
        return time(hh, mm)
    except ValueError:
        raise SystemExit(f"--from wants a clock time like 09:30 (got {text!r})")


def add_conditions(session: pd.DataFrame, require_volume: bool = True,
                   earliest: time = EARLIEST_ALERT) -> pd.DataFrame:
    """The buy conditions, plus the bar where an alert would fire.

    `require_volume` drops condition (d). Volume is the one measure the free
    IEX feed gets badly wrong, so switching it off changes what data the
    alert can run on, not just how often it fires.

    `earliest` moves the gate in condition (e). Evaluating from 09:30 is
    only meaningful because the MACD is warmed on pre-market bars -- a feed
    with no pre-market reaches the open with an unsettled indicator, and
    prepare() records whether it did.
    """
    df = session.copy()

    above = df["macd"] > df["macd_signal"]
    crossed = above & ~above.shift(1, fill_value=False)
    df["cond_a_crossed"] = (
        crossed.rolling(CROSS_LOOKBACK_BARS, min_periods=1).max().astype(bool) & above
    )
    df["cond_b_rising"] = df["macd"] > df["macd"].shift(1)
    df["cond_c_diverging"] = (df["macd_gap"] > df["macd_gap"].shift(1)) & (
        df["macd_gap"].shift(1) > df["macd_gap"].shift(2)
    )
    df["cond_d_volume"] = (
        df["volume_ratio"] >= VOLUME_MULTIPLE if require_volume
        else pd.Series(True, index=df.index)
    )
    df["cond_e_time"] = [ts.time() >= earliest for ts in df.index]

    for name in CONDITIONS:
        df[name] = df[name].fillna(False).astype(bool)

    df["all_conditions"] = df[list(CONDITIONS)].all(axis=1)
    df["alert"] = apply_cooldown(df.index, df["all_conditions"])
    return df


def apply_cooldown(index, satisfied) -> List[bool]:
    """Thin a run of satisfied bars down to one alert per cooldown window.

    A setup that stays true for twenty minutes is one alert, not twenty.
    Stateful by nature, so it cannot be a vectorised column.
    """
    fired: List[bool] = []
    last_alert: Optional[datetime] = None
    for ts, ok in zip(index, satisfied):
        if ok and (
            last_alert is None
            or (ts - last_alert) >= timedelta(minutes=COOLDOWN_MINUTES)
        ):
            fired.append(True)
            last_alert = ts
        else:
            fired.append(False)
    return fired


def alert_text(symbol: str, row: pd.Series) -> str:
    side = "below" if row["macd"] < 0 else "above"
    vwap_side = "Below" if row["close"] < row["vwap"] else "Above"
    return (
        f"{symbol}: {side} zero and rising/diverging, ${row['close']:.2f}, "
        f"Volume {row['volume_ratio']:.1f}x normal, "
        f"{vwap_side} VWAP (${row['vwap']:.2f})"
    )


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

_env_loaded = False


def load_env() -> None:
    """Read .env once, before anything asks for a key.

    Called at the top of every entry point rather than lazily from the
    fetch path: a tool that only sends a notification never fetches, and
    would otherwise look for keys in an environment nothing had filled.
    A key that was never loaded looks exactly like a key that was never
    set, which is the expensive kind of silence.

    Beside this file first, then the working directory, because the
    desktop launcher does not necessarily run from this folder. Neither
    overrides a variable already set for real.
    """
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).with_name(".env"))
    load_dotenv()


def load_credentials() -> Tuple[str, str]:
    """Read the key pair without ever printing it."""
    import os

    load_env()

    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = (
        os.getenv("ALPACA_SECRET_KEY", "").strip()
        or os.getenv("ALPACA_API_SECRET", "").strip()
    )
    if not key or not secret:
        sys.exit(
            "No Alpaca credentials found.\n"
            "Put ALPACA_API_KEY and ALPACA_SECRET_KEY in a .env file next to "
            "this script, or set them in your environment."
        )
    return key, secret


_client = None


def _data_client():
    """One market-data client, reused. No trading client exists here."""
    global _client
    if _client is None:
        from alpaca.data.historical import StockHistoricalDataClient

        _client = StockHistoricalDataClient(*load_credentials())
    return _client


def fetch_extended(symbol: str, day: date, feed_name: str) -> pd.DataFrame:
    """04:00-16:00 ET of 1-minute bars. Pre-market is kept to warm the MACD."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=datetime.combine(day, PREMARKET_OPEN, tzinfo=ET),
        end=datetime.combine(day, SESSION_CLOSE, tzinfo=ET),
        feed=DataFeed.IEX if feed_name == "iex" else DataFeed.SIP,
    )
    frame = _data_client().get_stock_bars(request).df

    if frame is None or frame.empty:
        return pd.DataFrame()
    if isinstance(frame.index, pd.MultiIndex):
        frame = frame.xs(symbol, level="symbol")
    return frame.tz_convert(ET).sort_index()


def trading_days(end: date, count: int) -> List[date]:
    """The `count` most recent weekdays ending at or before `end`.

    Holidays are not filtered here -- a holiday simply returns no bars and
    is dropped when the data comes back empty.
    """
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

@dataclass
class DayResult:
    day: date
    frames: Dict[str, pd.DataFrame]          # feed -> prepared session
    premarket: Dict[str, int]                # feed -> pre-market bar count

    @property
    def usable(self) -> bool:
        return all(not f.empty for f in self.frames.values())


def disagreement_causes(
    iex: pd.DataFrame, sip: pd.DataFrame, timestamps
) -> Dict[str, int]:
    """For each disagreeing minute, which conditions differed between feeds."""
    causes: Dict[str, int] = {name: 0 for name in CONDITIONS}
    for ts in timestamps:
        if ts not in iex.index or ts not in sip.index:
            continue
        for name in CONDITIONS:
            if bool(iex.at[ts, name]) != bool(sip.at[ts, name]):
                causes[name] += 1
    return causes


def report(symbol: str, macd: Macd, results: List[DayResult]) -> pd.DataFrame:
    """Print the findings for one MACD setting; return the per-minute rows."""
    rule = "=" * 74
    usable = [r for r in results if r.usable]
    print(f"\n{rule}\n  {symbol}   MACD {macd}   {len(usable)} trading day(s)\n{rule}")

    if not usable:
        print("  No day returned data from both feeds.")
        return pd.DataFrame()

    # ---- 1. coverage ----------------------------------------------------
    print("\n1. COVERAGE -- how much of each day each feed saw\n")
    print(f"   {'Date':<12}{'IEX bars':>10}{'SIP bars':>10}{'IEX vol %':>12}"
          f"{'IEX pre':>9}{'SIP pre':>9}{'MACD warm':>11}")
    for r in usable:
        iex, sip = r.frames["iex"], r.frames["sip"]
        vol_pct = 100.0 * iex["volume"].sum() / sip["volume"].sum() if sip["volume"].sum() else 0
        warm = "both" if (iex.attrs.get("macd_warm_at_open") and sip.attrs.get("macd_warm_at_open")) \
            else ("SIP only" if sip.attrs.get("macd_warm_at_open") else "neither")
        print(f"   {r.day:%Y-%m-%d}{len(iex):>10}{len(sip):>10}{vol_pct:>11.1f}%"
              f"{r.premarket['iex']:>9}{r.premarket['sip']:>9}{warm:>11}")

    print("\n   'pre' is pre-market bars, used only to warm the MACD.")
    print(f"   MACD needs {macd.warmup_bars} bars to settle; fewer means the first")
    print("   minutes after 09:30 are computed from an unsettled indicator.")

    # ---- 2. numbers -----------------------------------------------------
    joined = []
    for r in usable:
        both = r.frames["iex"].join(r.frames["sip"], how="inner", lsuffix="_iex", rsuffix="_sip")
        both.insert(0, "date", r.day)
        joined.append(both)
    pooled = pd.concat(joined)

    print(f"\n2. NUMBERS -- {len(pooled)} minutes where both feeds have a bar\n")
    for field, name in (("close", "Close price"), ("macd", "MACD line"),
                        ("vwap", "VWAP"), ("volume_ratio", "Volume ratio")):
        a, b = pooled[f"{field}_iex"], pooled[f"{field}_sip"]
        pair = pd.concat([a, b], axis=1).dropna()
        if len(pair) < 2:
            continue
        corr = pair.iloc[:, 0].corr(pair.iloc[:, 1])
        worst = (pair.iloc[:, 0] - pair.iloc[:, 1]).abs().max()
        flag = "  <-- the weak one" if corr < 0.95 else ""
        print(f"   {name:<14} correlation {corr:>7.4f}   worst gap {worst:>9.4f}{flag}")

    # ---- 3. signals -----------------------------------------------------
    sip_all, iex_all, agreed_all = 0, 0, 0
    causes_total = {name: 0 for name in CONDITIONS}
    print("\n3. SIGNALS -- where the buy conditions fired\n")
    print(f"   {'Date':<12}{'SIP':>6}{'IEX':>6}{'agreed':>8}{'IEX false':>11}{'IEX missed':>12}")

    for r in usable:
        iex, sip = r.frames["iex"], r.frames["sip"]
        iex_times = set(iex.index[iex["alert"]])
        sip_times = set(sip.index[sip["alert"]])
        agreed = iex_times & sip_times
        sip_all += len(sip_times)
        iex_all += len(iex_times)
        agreed_all += len(agreed)
        for name, n in disagreement_causes(iex, sip, iex_times ^ sip_times).items():
            causes_total[name] += n
        print(f"   {r.day:%Y-%m-%d}{len(sip_times):>6}{len(iex_times):>6}{len(agreed):>8}"
              f"{len(iex_times - sip_times):>11}{len(sip_times - iex_times):>12}")

    print(f"   {'TOTAL':<12}{sip_all:>6}{iex_all:>6}{agreed_all:>8}"
          f"{iex_all - agreed_all:>11}{sip_all - agreed_all:>12}")

    if sip_all:
        print(f"\n   IEX caught {100.0 * agreed_all / sip_all:.0f}% of the real signals,")
        print(f"   and invented {iex_all - agreed_all} that the full tape never showed.")

    disagreements = sum(causes_total.values())
    if disagreements:
        print("\n   Which condition differed, on the minutes the feeds disagreed:\n")
        for name, n in sorted(causes_total.items(), key=lambda kv: -kv[1]):
            share = 100.0 * n / disagreements
            bar = "#" * int(share / 4)
            print(f"     {CONDITIONS[name]:<26}{n:>5}  {share:>5.1f}%  {bar}")

    # ---- verdict --------------------------------------------------------
    print(f"\n{rule}\n  VERDICT for MACD {macd}")
    if not sip_all:
        print("  No signals at all. Try more days or a different setting.")
    else:
        caught = 100.0 * agreed_all / sip_all
        false_rate = 100.0 * (iex_all - agreed_all) / iex_all if iex_all else 0.0
        print(f"  IEX caught {caught:.0f}% of real signals, and {false_rate:.0f}% of what")
        print("  it fired was not real.")
        if caught >= 90 and false_rate <= 15:
            print("  -> Free IEX is good enough. Build on it.")
        elif false_rate > 30:
            print("  -> Too many false alarms to trust. Either pay for SIP (check")
            print("     IBKR before Alpaca's $99/mo) or fix the condition named above.")
        else:
            print("  -> Marginal. More days, or tighten the condition named above.")
    print(rule)

    return pooled


# --------------------------------------------------------------------------
# Self-test -- proves the math without network access or credentials
# --------------------------------------------------------------------------

def _index(n: int, first: time = SESSION_OPEN) -> pd.DatetimeIndex:
    start = datetime.combine(date(2026, 9, 18), first, tzinfo=ET)
    return pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(n)])


def _frame(closes, volumes, index) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": closes, "high": [c + 0.05 for c in closes],
         "low": [c - 0.05 for c in closes], "close": closes, "volume": volumes},
        index=index,
    )


def self_test() -> int:
    """Check the math offline. No network, no credentials, no data plan."""
    print("Self-test: checking the indicator and condition logic...\n")
    failures = []
    macd = DEFAULT_MACD

    # A V-shaped session with the volume spike ON the turn, which is what
    # the conditions are built to catch: momentum turning up WITH
    # participation behind it.
    n = 120
    closes = [100 - i * 0.05 for i in range(60)] + [97 + (i - 60) * 0.12 for i in range(60, n)]
    volumes = [1000] * 60 + [4000] * 16 + [1500] * 44
    df = add_conditions(prepare(_frame(closes, volumes, _index(n)), macd))

    if not df["macd"].notna().all():
        failures.append("MACD produced NaN where it should not")

    crossings = int(((df["macd"] > df["macd_signal"]) &
                     (df["macd"].shift(1) <= df["macd_signal"].shift(1))).sum())
    if crossings < 1:
        failures.append("expected a MACD crossover in the rally")

    alerts = list(df.index[df["alert"]])
    if not alerts:
        failures.append("expected an alert where the crossover met the volume spike")

    if not ((df["vwap"] >= df["low"].min()) & (df["vwap"] <= df["high"].max())).all():
        failures.append("VWAP drifted outside the day's price range")

    # A flat, quiet session must produce nothing.
    flat = add_conditions(prepare(_frame([50.0] * 60, [100] * 60, _index(60)), macd))
    if flat["alert"].any():
        failures.append("a flat session produced an alert")

    # Cooldown: 40 straight satisfied bars is 3 alerts, not 40.
    run = _index(40, time(10, 0))
    fired = apply_cooldown(run, [True] * 40)
    times = [ts for ts, f in zip(run, fired) if f]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    if sum(fired) != 3:
        failures.append(f"40 satisfied bars should fire 3 times, got {sum(fired)}")
    if any(g < COOLDOWN_MINUTES for g in gaps):
        failures.append(f"cooldown violated, gaps were {gaps}")

    # Nothing before 09:45, and the 09:30 bar must fail the time condition.
    if any(ts.time() < EARLIEST_ALERT for ts in alerts):
        failures.append("an alert fired before 09:45")
    if df["cond_e_time"].iloc[0]:
        failures.append("the 09:30 bar should fail the time condition")

    # The warm-up fix: pre-market bars must reach the MACD but not the
    # session output, and must change the MACD value at the open.
    # 60 bars of pre-market (08:30-09:29) followed by the session itself.
    warm_index = _index(n + 60, time(8, 30))
    warm_closes = [100.0] * 60 + closes
    warm = prepare(_frame(warm_closes, [500] * 60 + volumes, warm_index), macd)
    if warm.empty:
        failures.append("the warm-up fixture produced no session bars")
    if warm.attrs.get("premarket_bars") != 60:
        failures.append(f"expected 60 pre-market bars counted, got {warm.attrs.get('premarket_bars')}")
    if any(ts.time() < SESSION_OPEN for ts in warm.index):
        failures.append("pre-market bars leaked into the session output")
    if not warm.attrs.get("macd_warm_at_open"):
        failures.append("60 pre-market bars should be enough to warm a 9/17/6 MACD")

    # The volume baseline must ignore pre-market, or every bar looks busy.
    # The rolling average needs a full window, so the Nth session bar is the
    # first with a ratio -- which lands after the 09:45 gate, not before it.
    if warm["volume_ratio"].iloc[: VOLUME_LOOKBACK_BARS - 1].notna().any():
        failures.append(
            f"volume ratio should be undefined for the first "
            f"{VOLUME_LOOKBACK_BARS - 1} session bars"
        )
    if pd.isna(warm["volume_ratio"].iloc[VOLUME_LOOKBACK_BARS - 1]):
        failures.append(f"bar {VOLUME_LOOKBACK_BARS} should be the first with a volume ratio")

    # Dropping condition (d) must let through bars the volume test blocked,
    # and must never block one it allowed.
    # Same rally, flat volume: every bar sits at 1.0x, so condition (d)
    # blocks the crossover the other three conditions found.
    flat_vol = [1000] * n
    quiet_on = add_conditions(prepare(_frame(closes, flat_vol, _index(n)), macd))
    quiet_off = add_conditions(prepare(_frame(closes, flat_vol, _index(n)), macd),
                               require_volume=False)
    with_vol = set(quiet_on.index[quiet_on["all_conditions"]])
    without_vol = set(quiet_off.index[quiet_off["all_conditions"]])
    if with_vol:
        failures.append("flat volume should fail condition (d) on every bar")
    if not without_vol:
        failures.append("dropping the volume test should surface the MACD crossover")
    if not with_vol <= without_vol:
        failures.append("dropping the volume test lost bars it should have kept")

    # Moving the gate to the open must admit bars between 09:30 and 09:45.
    early = add_conditions(prepare(_frame(closes, volumes, _index(n)), macd),
                           require_volume=False, earliest=SESSION_OPEN)
    if not early["cond_e_time"].all():
        failures.append("with the gate at 09:30 every session bar should pass condition (e)")
    opened = sum(1 for ts in early.index[early["all_conditions"]]
                 if ts.time() < EARLIEST_ALERT)

    print(f"  MACD crossovers in the rally   : {crossings}")
    print(f"  Alerts fired in the rally      : {len(alerts)}")
    for ts in alerts:
        print(f"      {ts:%H:%M}  {alert_text('TEST', df.loc[ts])}")
    print(f"  Flat session alerts            : {int(flat['alert'].sum())} (expected 0)")
    print(f"  40 satisfied bars -> alerts    : {sum(fired)} (expected 3, one per 15 min)")
    print(f"  Pre-market bars warmed MACD    : {warm.attrs.get('premarket_bars')} "
          f"(needs {macd.warmup_bars})")
    print(f"  Session output starts at       : {warm.index[0]:%H:%M} (expected 09:30)")
    print(f"  First usable volume ratio at   : "
          f"{warm.index[warm['volume_ratio'].notna()][0]:%H:%M}")
    print(f"  Qualifying bars, volume on/off : {len(with_vol)} / {len(without_vol)}")
    print(f"  Extra bars once gate is 09:30  : {opened}")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll checks passed. The math is sound; now run it against real data.")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description="Compare Alpaca's IEX and SIP feeds.")
    parser.add_argument("--symbol", default=SYMBOL, help=f"Ticker (default {SYMBOL})")
    parser.add_argument("--days", type=int, default=5, help="Trading days to check (default 5)")
    parser.add_argument("--date", help="End on this day, YYYY-MM-DD (default: last weekday)")
    parser.add_argument("--macd", help="One setting as fast,slow,signal. Default: both "
                                       f"{DEFAULT_MACD} and {BASELINE_MACD}")
    parser.add_argument("--csv", default=None, help="Where to write the per-minute CSV")
    parser.add_argument("--no-volume", action="store_true",
                        help="Drop condition (d), the volume test")
    parser.add_argument("--from", dest="earliest", default="09:45",
                        help="Earliest alert time, ET (default 09:45; 09:30 is the open)")
    parser.add_argument("--self-test", action="store_true", help="Check the math offline")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    end = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else date.today() - timedelta(days=1))
    days = trading_days(end, max(1, args.days))
    symbol = args.symbol.upper()
    settings = [Macd.parse(args.macd)] if args.macd else [DEFAULT_MACD, BASELINE_MACD]
    require_volume = not args.no_volume
    earliest = parse_clock(args.earliest)
    active = [CONDITIONS[c] for c in CONDITIONS if c != "cond_d_volume" or require_volume]
    print("Conditions: " + ", ".join(active).replace("(e) after 09:45",
                                                     f"(e) after {earliest:%H:%M}"))

    # Fetch once per day per feed; every MACD setting reuses the same bars.
    print(f"Fetching {symbol} 1-minute bars for {len(days)} day(s), both feeds...")
    raw: Dict[Tuple[date, str], pd.DataFrame] = {}
    for day in days:
        got = []
        for feed in ("iex", "sip"):
            try:
                raw[(day, feed)] = fetch_extended(symbol, day, feed)
            except Exception as exc:  # noqa: BLE001 -- the message is the point
                print(f"  {day} {feed.upper()}: failed -- {type(exc).__name__}: {exc}")
                raw[(day, feed)] = pd.DataFrame()
            got.append(f"{feed.upper()} {len(raw[(day, feed)])}")
        print(f"  {day:%Y-%m-%d}  " + "  ".join(got) +
              ("   (no data -- holiday?)" if not any(len(raw[(day, f)]) for f in ("iex", "sip")) else ""))

    if not any(len(v) for v in raw.values()):
        print(f"\nNo data for {symbol} on any requested day.\n"
              f"  - Try --symbol AAPL to check the setup itself.\n"
              f"  - Try --date with a known trading day.")
        return 1

    frames = []
    for macd in settings:
        results = [
            DayResult(
                day=day,
                frames={f: prepare(raw[(day, f)], macd) if not raw[(day, f)].empty
                        else pd.DataFrame() for f in ("iex", "sip")},
                premarket={f: int((raw[(day, f)].index.time < SESSION_OPEN).sum())
                           if not raw[(day, f)].empty else 0 for f in ("iex", "sip")},
            )
            for day in days
        ]
        for r in results:
            for f in ("iex", "sip"):
                if not r.frames[f].empty:
                    attrs = r.frames[f].attrs
                    r.frames[f] = add_conditions(r.frames[f], require_volume, earliest)
                    r.frames[f].attrs.update(attrs)
        pooled = report(symbol, macd, results)
        if not pooled.empty:
            pooled.insert(1, "macd_setting", str(macd))
            frames.append(pooled)

    if frames:
        path = args.csv or f"{symbol}_{days[0]:%Y%m%d}_{days[-1]:%Y%m%d}_iex_vs_sip.csv"
        pd.concat(frames).to_csv(path)
        print(f"\nPer-minute comparison written to {path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
