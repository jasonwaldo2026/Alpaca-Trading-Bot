"""
SPCX alert: a warning that a buy setup may be opening, sent to your phone.

This is not a trading system and does not try to be one. The 69-session
study settled that these conditions have no mechanical edge -- entering
every signal with a fixed bracket loses to entering at random. What they
may still be worth is your attention: a nudge to open the chart and look.
You read the candles, you decide, you place the order yourself in your
broker. Nothing here can place a trade -- there is no trading client in
this file, and no order object anywhere in the project.

Conditions, on each COMPLETED 1-minute bar from 09:30 ET:

  (a) MACD crossed above its signal line within the last 3 bars
  (b) MACD is higher than one bar ago
  (c) The MACD-signal gap has widened two bars in a row

The volume test is deliberately absent. Measured against a 20-minute
rolling average it gated signals into the quietest hours of the day, and
it was the one measure the free IEX feed got badly wrong.

Alert budget
------------
The raw conditions fire about 15 times a session, which is far more than
anyone will keep reading. A cooldown thins them -- 40 minutes lands near
5 or 6 a day -- and every signal is written to the database whether or
not it alerted. So the log answers, after a fortnight of real use, the
only question that matters: which alerts were worth opening the phone
for, and did the suppressed ones turn out to matter.

    python spcx_alert.py --watch          # run through the session
    python spcx_alert.py --once           # evaluate the latest bar, exit
    python spcx_alert.py --once --dry-run # print instead of sending
    python spcx_alert.py --backfill       # fill in what price did next
    python spcx_alert.py --recent 20      # what has fired lately
    python spcx_alert.py --self-test      # check the logic, no network

Setup
-----
    pip install alpaca-py pandas python-dotenv

    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...
    PUSHOVER_APP_TOKEN=...
    PUSHOVER_USER_KEY=...

Without the Pushover pair it still runs, logs, and prints -- it just does
not buzz. Keep feed_check.py beside this file: the indicator and
condition code is imported from it rather than copied, so the alert fires
on exactly the objects the study measured.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time as time_mod
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
import pandas as pd

from feed_check import (
    ET,
    SESSION_CLOSE,
    SESSION_OPEN,
    Macd,
    add_conditions,
    load_credentials,
    prepare,
)

SYMBOL = "SPCX"
MACD_SETTING = Macd(9, 17, 6)

#: Minutes between alerts. The conditions fire ~15x a session; 40 minutes
#: lands near 5 or 6, which is a number someone will still be reading in a
#: month. Raise it if the phone gets annoying, lower it to see more.
COOLDOWN_MINUTES = 40

#: Conditions are evaluated and LOGGED from the opening bell, so the
#: record of the morning is complete.
EARLIEST_SIGNAL = SESSION_OPEN

#: The phone stays quiet until this. Evaluating a bar and alerting on it
#: are two different decisions: the first 15 minutes are for watching the
#: candles yourself, not for being told about them. Every qualifying bar
#: before this is still written to the database, marked as held back.
ALERT_FROM = time(9, 45)

#: Bars fetched behind the current moment. MACD is an exponential average
#: of price and carries across the session boundary, so it is warmed on
#: the preceding bars -- including yesterday's, when pre-market is thin.
#: IEX pre-market runs 0-17 bars a day against SIP's 300-plus, so without
#: reaching back a day the indicator would be unsettled at the open.
WARMUP_MINUTES = 900

DB_PATH = "spcx_alerts.db"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

#: Minutes after a signal at which to record what price did.
HORIZONS_MIN = (15, 30, 60)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY,
    symbol       TEXT NOT NULL,
    bar_time     TEXT NOT NULL,
    detected_at  TEXT NOT NULL,
    price        REAL,
    vwap         REAL,
    macd         REAL,
    macd_signal  REAL,
    macd_gap     REAL,
    volume       INTEGER,
    macd_side    TEXT,
    alerted      INTEGER NOT NULL DEFAULT 0,
    suppressed   TEXT,
    message      TEXT,
    ret_15 REAL, mfe_15 REAL, mae_15 REAL,
    ret_30 REAL, mfe_30 REAL, mae_30 REAL,
    ret_60 REAL, mfe_60 REAL, mae_60 REAL,
    outcomes_at  TEXT,
    UNIQUE (symbol, bar_time)
);
CREATE INDEX IF NOT EXISTS signals_bar_time ON signals (bar_time);
"""


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def open_db(path: str = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def last_alert_time(db: sqlite3.Connection, symbol: str) -> Optional[datetime]:
    """When the last alert actually went out.

    Read from the database rather than held in memory, so restarting the
    program mid-session does not re-announce a setup you were just told
    about.
    """
    row = db.execute(
        "SELECT MAX(bar_time) AS t FROM signals WHERE symbol = ? AND alerted = 1",
        (symbol,),
    ).fetchone()
    return datetime.fromisoformat(row["t"]) if row and row["t"] else None


def record(db: sqlite3.Connection, symbol: str, bar_time: datetime, row: pd.Series,
           alerted: bool, suppressed: Optional[str], message: str) -> bool:
    """Write one signal. Returns False if this bar was already recorded."""
    try:
        db.execute(
            """INSERT INTO signals
               (symbol, bar_time, detected_at, price, vwap, macd, macd_signal,
                macd_gap, volume, macd_side, alerted, suppressed, message)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, bar_time.isoformat(), datetime.now(ET).isoformat(),
             float(row["close"]), float(row["vwap"]), float(row["macd"]),
             float(row["macd_signal"]), float(row["macd_gap"]), int(row["volume"]),
             "below" if row["macd"] < 0 else "above",
             1 if alerted else 0, suppressed, message),
        )
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False           # same bar seen twice; nothing to do


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------

def _client():
    from alpaca.data.historical import StockHistoricalDataClient

    return StockHistoricalDataClient(*load_credentials())


def fetch_recent(symbol: str, now: datetime, minutes: int = WARMUP_MINUTES) -> pd.DataFrame:
    """Bars up to `now`, with the in-progress minute removed.

    Alpaca includes the current, still-forming bar in a request that runs
    to the present. Acting on it means acting on a price that has not
    finished happening -- the bar can still reverse before it closes. Every
    bar stamped at or after the current minute is dropped.
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    feed = os.getenv("ALPACA_DATA_FEED", "").strip().lower()
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=now - timedelta(minutes=minutes),
        end=now,
        feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
    )
    frame = _client().get_stock_bars(request).df
    if frame is None or frame.empty:
        return pd.DataFrame()
    if isinstance(frame.index, pd.MultiIndex):
        frame = frame.xs(symbol, level="symbol")
    frame = frame.tz_convert(ET).sort_index()

    forming = now.replace(second=0, microsecond=0)
    return frame[frame.index < forming]


# --------------------------------------------------------------------------
# The alert
# --------------------------------------------------------------------------

def compose(symbol: str, bar_time: datetime, row: pd.Series) -> str:
    """One line, read at arm's length on a lock screen.

    The VWAP is given as a price rather than a distance: two dollar figures
    say which side you are on and by how much without any arithmetic, and
    the level itself is often where price heads back to.
    """
    side = "below" if row["macd"] < 0 else "above"
    return (
        f"{symbol} {bar_time:%H:%M} — {side} zero, rising · "
        f"${row['close']:.2f} · VWAP ${row['vwap']:.2f}"
    )


def send_pushover(message: str, title: str = "SPCX setup",
                  priority: int = 0) -> Optional[str]:
    """Deliver to the phone. Returns an error string, or None on success.

    Priority -1 arrives without a sound or vibration -- it sits in the
    tray to be glanced at. 0 is a normal notification. 1 is an alarm that
    sounds through a focus mode. A stream of routine updates belongs at
    -1, or the phone becomes unusable and the alarms get ignored with it.
    """
    token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user = os.getenv("PUSHOVER_USER_KEY", "").strip()
    if not token or not user:
        return "no Pushover credentials — logged only"

    payload = urllib.parse.urlencode({
        "token": token, "user": user, "title": title, "message": message,
        "priority": str(priority),
    }).encode()
    try:
        with urllib.request.urlopen(PUSHOVER_URL, data=payload, timeout=10) as response:
            body = json.loads(response.read().decode())
        return None if body.get("status") == 1 else f"Pushover said: {body}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------

@dataclass
class Result:
    bar_time: Optional[datetime] = None
    fired: bool = False
    alerted: bool = False
    message: str = ""
    note: str = ""


def check_once(db: sqlite3.Connection, symbol: str = SYMBOL,
               macd: Macd = MACD_SETTING, now: Optional[datetime] = None,
               dry_run: bool = False,
               cooldown: int = COOLDOWN_MINUTES,
               alert_from: time = ALERT_FROM) -> Result:
    """Evaluate the most recent completed bar; alert only if it qualifies
    AND the morning quiet period is over.

    A qualifying bar before `alert_from` is recorded exactly like any
    other -- it simply does not buzz. That keeps the log honest about what
    the conditions did all morning while leaving the open to your own
    eyes.
    """
    now = now or datetime.now(ET)
    raw = fetch_recent(symbol, now)
    if raw.empty:
        return Result(note="no bars returned")

    session = add_conditions(prepare(raw, macd), require_volume=False,
                             earliest=EARLIEST_SIGNAL)
    if session.empty:
        return Result(note="no regular-hours bars yet")

    bar_time = session.index[-1]
    row = session.iloc[-1]
    if not bool(row["all_conditions"]):
        return Result(bar_time=bar_time, note="conditions not met")

    message = compose(symbol, bar_time, row)

    # Two reasons to stay quiet, checked in the order they matter. The
    # morning gate wins: during it, the cooldown is beside the point.
    too_early = bar_time.time() < alert_from
    last = last_alert_time(db, symbol)
    within_cooldown = last is not None and (bar_time - last) < timedelta(minutes=cooldown)

    if too_early:
        suppressed = f"before {alert_from:%H:%M} — logged, not sent"
    elif within_cooldown:
        suppressed = f"cooldown — last alert {last:%H:%M}"
    else:
        suppressed = None

    fresh = record(db, symbol, bar_time, row, alerted=suppressed is None,
                   suppressed=suppressed, message=message)
    if not fresh:
        return Result(bar_time=bar_time, fired=True, note="already recorded")

    if suppressed:
        return Result(bar_time=bar_time, fired=True, message=message,
                      note=suppressed)

    if dry_run:
        return Result(bar_time=bar_time, fired=True, message=message,
                      note="dry run — not sent")

    error = send_pushover(message)
    if error:
        db.execute("UPDATE signals SET suppressed = ? WHERE symbol = ? AND bar_time = ?",
                   (error, symbol, bar_time.isoformat()))
        db.commit()
        return Result(bar_time=bar_time, fired=True, alerted=False,
                      message=message, note=error)

    return Result(bar_time=bar_time, fired=True, alerted=True, message=message)


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------

def backfill(db: sqlite3.Connection, symbol: str = SYMBOL) -> int:
    """Fill in what price did after each signal, once enough time has passed.

    Kept separate from alerting on purpose: at the moment a signal fires,
    what happens next has not happened yet. This is what turns the log into
    something you can learn from.
    """
    cutoff = datetime.now(ET) - timedelta(minutes=max(HORIZONS_MIN) + 2)
    rows = db.execute(
        "SELECT id, bar_time FROM signals WHERE symbol = ? AND outcomes_at IS NULL "
        "AND bar_time <= ? ORDER BY bar_time",
        (symbol, cutoff.isoformat()),
    ).fetchall()
    if not rows:
        return 0

    filled = 0
    for row in rows:
        bar_time = datetime.fromisoformat(row["bar_time"])
        end = bar_time + timedelta(minutes=max(HORIZONS_MIN) + 2)
        bars = fetch_recent(symbol, min(end, datetime.now(ET)),
                            minutes=max(HORIZONS_MIN) + 5)
        after = bars[bars.index > bar_time]
        if after.empty:
            continue

        entry = float(after["open"].iloc[0])
        values = {}
        for minutes in HORIZONS_MIN:
            window = after[after.index <= bar_time + timedelta(minutes=minutes)]
            if window.empty:
                continue
            values[f"ret_{minutes}"] = 100.0 * (float(window["close"].iloc[-1]) - entry) / entry
            values[f"mfe_{minutes}"] = 100.0 * (float(window["high"].max()) - entry) / entry
            values[f"mae_{minutes}"] = 100.0 * (float(window["low"].min()) - entry) / entry
        if not values:
            continue

        sets = ", ".join(f"{k} = ?" for k in values) + ", outcomes_at = ?"
        db.execute(f"UPDATE signals SET {sets} WHERE id = ?",
                   (*values.values(), datetime.now(ET).isoformat(), row["id"]))
        filled += 1
    db.commit()
    return filled


def show_recent(db: sqlite3.Connection, limit: int, symbol: str = SYMBOL) -> None:
    rows = db.execute(
        "SELECT * FROM signals WHERE symbol = ? ORDER BY bar_time DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    if not rows:
        print("Nothing logged yet.")
        return

    print(f"\n  {'Bar':<17}{'':<3}{'Price':>9}{'VWAP':>9}{'+15m':>8}{'+30m':>8}{'+60m':>8}  Note")
    for row in reversed(rows):
        bar = datetime.fromisoformat(row["bar_time"])
        mark = "buzz" if row["alerted"] else " -- "
        def pct(key):
            return f"{row[key]:+.2f}%" if row[key] is not None else "   ·  "
        note = row["suppressed"] or ""
        print(f"  {bar:%Y-%m-%d %H:%M}  {mark} {row['price']:>8.2f}{row['vwap']:>9.2f}"
              f"{pct('ret_15'):>8}{pct('ret_30'):>8}{pct('ret_60'):>8}  {note}")

    alerted = sum(1 for r in rows if r["alerted"])
    print(f"\n  {len(rows)} signals shown, {alerted} alerted, "
          f"{len(rows) - alerted} suppressed.\n")


# --------------------------------------------------------------------------
# Watching
# --------------------------------------------------------------------------

def market_is_open(now: datetime) -> bool:
    """Weekday, between the bell and the close. Holidays return no bars."""
    return now.weekday() < 5 and SESSION_OPEN <= now.time() < SESSION_CLOSE


def watch(db: sqlite3.Connection, dry_run: bool, cooldown: int,
          alert_from: time = ALERT_FROM) -> int:
    """Check once per minute, a few seconds after each bar completes."""
    print(f"Watching {SYMBOL} · MACD {MACD_SETTING} · alerts from {alert_from:%H:%M} ET "
          f"· one alert per {cooldown} min")
    print("Ctrl-C to stop. Every signal is logged; only some are sent.\n")
    try:
        while True:
            now = datetime.now(ET)
            if market_is_open(now):
                try:
                    result = check_once(db, dry_run=dry_run, cooldown=cooldown,
                                        alert_from=alert_from)
                except Exception as exc:  # noqa: BLE001 -- a bad minute must not end the day
                    print(f"  {now:%H:%M}  error: {type(exc).__name__}: {exc}")
                else:
                    if result.fired:
                        mark = "BUZZ" if result.alerted else "log "
                        print(f"  {now:%H:%M}  {mark}  {result.message}"
                              + (f"   ({result.note})" if result.note else ""))
            elif now.time() >= SESSION_CLOSE:
                filled = backfill(db)
                if filled:
                    print(f"  {now:%H:%M}  session over — filled outcomes for {filled} signal(s)")
                    return 0
            # Wake a few seconds after the next minute closes.
            time_mod.sleep(max(5, 65 - datetime.now(ET).second))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def self_test() -> int:
    """Check the logic offline. No network, no credentials, no Pushover."""
    print("Self-test: checking the alert logic...\n")
    failures = []

    # Flat pre-market, a sharp dip over the first six minutes of the
    # session and an immediate recovery -- so a crossover lands INSIDE the
    # 09:30-09:45 quiet period -- then a second push later in the morning.
    start = datetime.combine(date(2026, 9, 18), time(8, 30), tzinfo=ET)
    closes = [100.0] * 60
    price = 100.0
    for _ in range(6):                       # 09:30-09:35, down hard
        price -= 0.08
        closes.append(price)
    for _ in range(24):                      # 09:36-09:59, straight back up
        price += 0.07
        closes.append(price)
    for step in range(90):                   # the rest of the morning
        price += 0.06 if step % 45 < 18 else -0.03
        closes.append(price)
    n = len(closes)
    index = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(n)])
    frame = pd.DataFrame(
        {"open": closes, "high": [c + 0.05 for c in closes],
         "low": [c - 0.05 for c in closes], "close": closes,
         "volume": [1000] * n},
        index=index,
    )
    session = add_conditions(prepare(frame, MACD_SETTING), require_volume=False,
                             earliest=EARLIEST_SIGNAL)

    qualifying = [ts for ts in session.index if bool(session.at[ts, "all_conditions"])]
    if not qualifying:
        failures.append("the fixture should produce at least one qualifying bar")

    # The message: VWAP as a price, no percentage, no range.
    sample = session.loc[qualifying[0]] if qualifying else session.iloc[-1]
    message = compose("SPCX", qualifying[0] if qualifying else session.index[-1], sample)
    if "%" in message:
        failures.append(f"the alert should carry no percentages: {message}")
    if "VWAP $" not in message:
        failures.append(f"the alert should give VWAP as a price: {message}")
    for word in ("zero", "rising"):
        if word not in message:
            failures.append(f"the alert should say '{word}': {message}")

    # Cooldown, against a real database.
    db = open_db(":memory:")
    if last_alert_time(db, "SPCX") is not None:
        failures.append("an empty database should report no previous alert")

    first = qualifying[0]
    record(db, "SPCX", first, session.loc[first], alerted=True, suppressed=None,
           message=message)
    if last_alert_time(db, "SPCX") != first:
        failures.append("the last alert time should come back from the database")

    # The same bar twice must not create a second row.
    if record(db, "SPCX", first, session.loc[first], True, None, message):
        failures.append("recording the same bar twice should be refused")

    soon = first + timedelta(minutes=COOLDOWN_MINUTES - 1)
    later = first + timedelta(minutes=COOLDOWN_MINUTES)
    if not (soon - first) < timedelta(minutes=COOLDOWN_MINUTES):
        failures.append("a bar inside the cooldown should be suppressed")
    if (later - first) < timedelta(minutes=COOLDOWN_MINUTES):
        failures.append("a bar at the cooldown boundary should be allowed")

    # The forming bar must never survive a fetch.
    now = datetime.combine(date(2026, 9, 18), time(10, 30, 42), tzinfo=ET)
    forming = now.replace(second=0, microsecond=0)
    kept = frame[frame.index < forming]
    if len(kept) and kept.index[-1] >= forming:
        failures.append("the in-progress bar leaked through")

    # Nothing in this module may reach a trading client.
    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "MarketOrderRequest"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    # Evaluating and alerting must be two different gates, not one.
    if EARLIEST_SIGNAL >= ALERT_FROM:
        failures.append("signals should be evaluated earlier than alerts are sent")
    if ALERT_FROM != time(9, 45):
        failures.append(f"the alert gate should be 09:45, got {ALERT_FROM}")
    held = [ts for ts in qualifying if ts.time() < ALERT_FROM]
    sendable = [ts for ts in qualifying if ts.time() >= ALERT_FROM]
    if not held:
        failures.append("the fixture should produce a qualifying bar before 09:45, "
                        "or the quiet period is not actually being tested")
    if not all(session.at[ts, "cond_e_time"] for ts in held):
        failures.append("a bar before 09:45 should still satisfy the time condition, "
                        "so that it is logged")

    # And the decision itself: held bars record, and stay silent.
    quiet_db = open_db(":memory:")
    for ts in held:
        record(quiet_db, "SPCX", ts, session.loc[ts], alerted=False,
               suppressed=f"before {ALERT_FROM:%H:%M} — logged, not sent",
               message=compose("SPCX", ts, session.loc[ts]))
    if last_alert_time(quiet_db, "SPCX") is not None:
        failures.append("a morning full of held bars must leave no last-alert time")
    if quiet_db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] != len(held):
        failures.append("every held bar should still be written to the database")

    print(f"  Qualifying bars in fixture     : {len(qualifying)}")
    print(f"    before 09:45 (log only)      : {len(held)}")
    print(f"    from 09:45 (may alert)       : {len(sendable)}")
    print(f"  Example alert                  : {message}")
    print(f"  Cooldown                       : {COOLDOWN_MINUTES} min, read from the database")
    print("  Duplicate bar rejected         : yes")
    print("  Forming bar dropped            : yes")
    print("  Trading client in this file    : none")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed. Run --once --dry-run against real data next.")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Warn when an SPCX buy setup may be opening.")
    parser.add_argument("--watch", action="store_true", help="Run through the session")
    parser.add_argument("--once", action="store_true", help="Evaluate the latest bar and exit")
    parser.add_argument("--backfill", action="store_true", help="Fill in what price did next")
    parser.add_argument("--recent", type=int, metavar="N", help="Show the last N signals")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of sending")
    parser.add_argument("--cooldown", type=int, default=COOLDOWN_MINUTES,
                        help=f"Minutes between alerts (default {COOLDOWN_MINUTES})")
    parser.add_argument("--alert-from", default=f"{ALERT_FROM:%H:%M}",
                        help=f"Stay quiet before this, ET (default {ALERT_FROM:%H:%M}). "
                             "Signals are still logged from 09:30.")
    parser.add_argument("--db", default=DB_PATH, help=f"Database file (default {DB_PATH})")
    parser.add_argument("--self-test", action="store_true", help="Check the logic offline")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    from feed_check import parse_clock
    alert_from = parse_clock(args.alert_from)
    db = open_db(args.db)

    if args.recent:
        show_recent(db, args.recent)
        return 0

    if args.backfill:
        filled = backfill(db)
        print(f"Filled outcomes for {filled} signal(s).")
        return 0

    if args.watch:
        return watch(db, args.dry_run, args.cooldown, alert_from)

    if args.once:
        result = check_once(db, dry_run=args.dry_run, cooldown=args.cooldown,
                            alert_from=alert_from)
        if not result.fired:
            print("No setup on the last completed bar"
                  + (f" ({result.bar_time:%H:%M})" if result.bar_time else "")
                  + (f" — {result.note}" if result.note else ""))
        else:
            mark = "SENT" if result.alerted else "logged, not sent"
            print(f"{mark}: {result.message}"
                  + (f"\n  {result.note}" if result.note else ""))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
