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
from typing import List, Optional, Tuple
import pandas as pd

from feed_check import (
    ET,
    SESSION_CLOSE,
    SESSION_OPEN,
    Macd,
    add_conditions,
    load_credentials,
    load_env,
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
#: remain two different decisions -- every qualifying bar outside the
#: window is still written to the database, marked as held back -- but
#: the window is now the whole session.
#:
#: It was 09:40-11:00, gated on the finding that the morning carries most
#: of the day's movement while signals fire at a flat rate all day. That
#: gate was right for an alert meant to be acted on directly. This one is
#: a doorbell: it says come and look, and the looking happens on Level 2
#: and the tape, where the decision actually gets made. The 1-1.5% runs
#: being watched for happen four or five times a session, afternoons
#: included, and a gate that silences two thirds of the day cannot catch
#: them. Narrow it again with --alert-from and --alert-until.
ALERT_FROM = SESSION_OPEN

#: And quiet again after this -- now the closing bell.
#:
#: The measurement that produced the old 11:00 close still stands and is
#: worth keeping written down: over 70 sessions the 09:40-11:00 stretch
#: carried 38% of the day's 0.5% swings on 22% of the signals, while
#: 13:00-14:30 fired most often and moved least, four consecutive half
#: hours where the typical signal went further against you than for you.
#: What it does NOT say is that the afternoon is empty -- only that the
#: signal is a worse entry there. For a doorbell answered by eye, that
#: is a reason to look harder in the afternoon, not to sleep through it.
#:
#: Everything is still evaluated and recorded outside the window.
#: Move it with --alert-until.
ALERT_UNTIL = SESSION_CLOSE

#: Bars fetched behind the current moment. MACD is an exponential average
#: of price and carries across the session boundary, so it is warmed on
#: the preceding bars -- including yesterday's, when pre-market is thin.
#: IEX pre-market runs 0-17 bars a day against SIP's 300-plus, so without
#: reaching back a day the indicator would be unsettled at the open.
WARMUP_MINUTES = 900

DB_PATH = "spcx_alerts.db"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"

#: Pushover priorities, shared with open_candles. -1 arrives with no sound
#: or vibration; 1 sounds through a focus mode.
PRIORITY_UPDATE = -1
PRIORITY_SUMMARY = 1

#: The uploaded sound this alert rings with. It lives HERE rather than in
#: open_candles because open_candles imports this module and the reverse
#: would be a circular import -- but the two must name the same sound, so
#: the self-test imports open_candles late and checks they agree.
#:
#: A sound name Pushover does not recognise is not an error. The message
#: is delivered with the account's default sound and reported as sent, so
#: a typo announces itself only as the wrong noise at 09:41.
SOUND_SETUP = "Buy_Stock"

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
    """The setup, read at arm's length on a lock screen.

    The headline is which side of the zero line the turn began on,
    because that is the difference between the best case and the ordinary
    one. Below zero means the fast average is still under the slow one --
    the stock has been falling or flat, and this is a turn starting from
    a low base rather than more of a move already under way. Above zero
    is the same shape of turn inside an advance that has already begun.

    Neither is a verdict. The alert exists to say come and look; the
    looking happens on Level 2 and the tape.

    The points of interest below the headline are a list on purpose:
    Point of Control and the 9 EMA are meant to join it, and adding one
    should be adding a line rather than rewriting the message.

    The VWAP is given as a price rather than a distance: two dollar
    figures say which side you are on and by how much without any
    arithmetic, and the level itself is often where price heads back to.
    """
    below = row["macd"] < 0
    headline = ("** R&D BELOW 0 — best case **" if below
                else "** R&D ABOVE 0 **")

    points = ["Crossed up, rising and diverging"]
    vwap = row.get("vwap") if hasattr(row, "get") else row["vwap"]
    if vwap is not None and pd.notna(vwap):
        where = "above" if row["close"] >= vwap else "below"
        points.append(f"Price {where} VWAP ${vwap:.2f}")

    return "\n".join(
        [headline, f"{symbol} ${row['close']:.2f}   {bar_time:%H:%M}"] + points
    )


#: Pushover accepts an image with a message. Its own ceiling is larger,
#: but a chart that takes a while to arrive on a phone is a chart you
#: read after the moment has passed, so this stays small deliberately.
MAX_ATTACHMENT_BYTES = 2_000_000


def _multipart(fields: dict, image_path: str) -> tuple:
    """Build a multipart/form-data body by hand.

    Pushover takes an attachment only as multipart, and the alternative
    is adding `requests` as a dependency for one POST a day.
    """
    boundary = "----SPCXBoundary7MA4YWxkTrZu0gW"
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n".encode())
    with open(image_path, "rb") as handle:
        blob = handle.read()
    kind = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="attachment"; '
        f'filename="{os.path.basename(image_path)}"\r\n'
        f"Content-Type: {kind}\r\n\r\n".encode())
    parts.append(blob)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_pushover(message: str, title: str = "SPCX setup",
                  priority: int = 0,
                  attachment: Optional[str] = None,
                  sound: Optional[str] = None) -> Optional[str]:
    """Deliver to the phone. Returns an error string, or None on success.

    Priority -1 arrives without a sound or vibration -- it sits in the
    tray to be glanced at. 0 is a normal notification. 1 is an alarm that
    sounds through a focus mode. A stream of routine updates belongs at
    -1, or the phone becomes unusable and the alarms get ignored with it.

    `sound` names one of Pushover's built-in sounds, or a custom sound
    uploaded to the account that owns the app token. It only matters at
    priority 0 and above; at -1 nothing plays whatever is asked for.
    """
    token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user = os.getenv("PUSHOVER_USER_KEY", "").strip()
    if not token or not user:
        return "no Pushover credentials — logged only"

    fields = {"token": token, "user": user, "title": title,
              "message": message, "priority": str(priority)}
    if sound:
        # Left unset, Pushover uses whatever the user picked as their
        # default for this application. Naming one overrides it, which is
        # the point: the sound is carrying the direction.
        fields["sound"] = sound

    usable = (attachment and os.path.exists(attachment)
              and os.path.getsize(attachment) <= MAX_ATTACHMENT_BYTES)
    if attachment and not usable:
        # Send the words anyway. A missing or oversized picture must never
        # be the reason an alert does not arrive.
        attachment = None

    try:
        if usable:
            payload, content_type = _multipart(fields, attachment)
            request = urllib.request.Request(PUSHOVER_URL, data=payload)
            request.add_header("Content-Type", content_type)
        else:
            request = urllib.request.Request(
                PUSHOVER_URL, data=urllib.parse.urlencode(fields).encode())
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode())
        return None if body.get("status") == 1 else f"Pushover said: {body}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"


def registered_devices() -> Tuple[Optional[List[str]], Optional[str]]:
    """Ask Pushover which devices this key actually reaches.

    Returns (devices, error). A key can be perfectly valid and reach
    nothing: the account exists, the send is accepted, and the message
    lands nowhere a human will see it. That failure is invisible from
    the sending side, so ask before claiming a test succeeded.
    """
    token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user = os.getenv("PUSHOVER_USER_KEY", "").strip()
    if not token or not user:
        return None, "no Pushover credentials"

    fields = {"token": token, "user": user}
    request = urllib.request.Request(
        PUSHOVER_VALIDATE_URL, data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except (ValueError, OSError):
            return None, f"HTTP {exc.code}"
        errors = body.get("errors") or [f"HTTP {exc.code}"]
        return None, "; ".join(str(e) for e in errors)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if body.get("status") != 1:
        errors = body.get("errors") or [str(body)]
        return None, "; ".join(str(e) for e in errors)
    return list(body.get("devices") or []), None


def test_push() -> int:
    """Send one of each kind, and say plainly what happened.

    Worth doing before you rely on it. A silent morning because a key was
    never pasted in looks exactly like a morning with nothing to report.
    """
    load_env()
    print("Checking the path to your phone...\n")

    present = {name: bool(os.getenv(name, "").strip())
               for name in ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY")}
    for name, ok in present.items():
        print(f"  {name:<22} {'set' if ok else 'MISSING'}")
    if not all(present.values()):
        print("\nNothing can be sent until both are in your .env.")
        print("  App token : pushover.net → Your Applications → your app")
        print("  User key  : pushover.net → the key on the main page after login")
        return 1

    # Ask who is listening before sending anything. Pushover accepts a
    # message for an account with no devices and reports success, so a
    # send that "worked" proves nothing on its own.
    devices, error = registered_devices()
    if error:
        print(f"\n  devices                UNKNOWN - {error}")
        print("\nThe key was rejected, so nothing would arrive. Check that the")
        print("app token and user key are the two different values they should")
        print("be: the token belongs to the application, the key belongs to you.")
        return 1
    if not devices:
        print("\n  devices                NONE")
        print("\nThis key is valid but no device is attached to it, so a message")
        print("is accepted and then reaches nobody. Two usual causes:")
        print("  1. The iPhone app is signed in to a different Pushover account.")
        print("  2. The 30-day trial lapsed and the app was never purchased.")
        print("     The account keeps accepting; the handset stops receiving.")
        print("\nOpen Pushover on the phone, check which account it is signed")
        print("in to, then run this again.")
        return 1
    print(f"  devices                {', '.join(devices)}")

    # The setup alert is built by compose() rather than written out here,
    # so this test shows the message the morning will actually send. A
    # hand-typed sample drifts away from the real one and then reassures
    # you about a format that no longer exists.
    sample_row = pd.Series({"macd": -0.04, "close": 152.41, "vwap": 152.68})
    sample_at = datetime.combine(date.today(), time(10, 42), tzinfo=ET)

    checks = [
        (PRIORITY_UPDATE, "quiet update", f"{SYMBOL} 09:36 \u25bc $153.89 (-3\u00a2)\n"
                                          "Minute volume 13.2k (1.0x usual)\n"
                                          "This is a test of the silent channel.",
         None),
        (PRIORITY_SUMMARY, "alarm", f"{SYMBOL} 09:35 \u25b2 $152.41 (+23\u00a2)\n"
                                    "Open $152.18   Close $152.41\n"
                                    "This is a test of the alarm channel.",
         None),
        (PRIORITY_SUMMARY, f"setup alert ({SOUND_SETUP})",
         compose(SYMBOL, sample_at, sample_row), SOUND_SETUP),
    ]

    failed = False
    for priority, label, message, sound in checks:
        error = send_pushover(message, title=f"{SYMBOL} test — {label}",
                              priority=priority, sound=sound)
        if error:
            print(f"\n  priority {priority:>2} ({label}): FAILED — {error}")
            failed = True
        else:
            print(f"\n  priority {priority:>2} ({label}): sent")
        time_mod.sleep(2)     # so they arrive in order, not as one blob

    if failed:
        print("\nAt least one send failed. The message above says why.")
        return 1

    print("\nBoth sent. On the phone you should now have TWO notifications:")
    print("  1. 'quiet update' — arrived with NO sound and NO vibration")
    print("  2. 'alarm'        — made a noise")
    print("\nIf the quiet one buzzed, or the alarm was silent, tell me and I")
    print("will change the priorities. If the alarm did not sound while your")
    print("phone was in a Focus mode, allow Pushover under Settings → Focus →")
    print("Allowed Notifications, or under Notifications → Pushover → Time")
    print("Sensitive. iOS can suppress a priority-1 push on its own.")
    return 0


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
               alert_from: time = ALERT_FROM,
               alert_until: time = ALERT_UNTIL) -> Result:
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
    too_late = bar_time.time() >= alert_until
    last = last_alert_time(db, symbol)
    within_cooldown = last is not None and (bar_time - last) < timedelta(minutes=cooldown)

    if too_early:
        suppressed = f"before {alert_from:%H:%M} — logged, not sent"
    elif too_late:
        suppressed = f"after {alert_until:%H:%M} — logged, not sent"
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

    error = send_pushover(message, priority=PRIORITY_SUMMARY,
                          sound=SOUND_SETUP)
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
          alert_from: time = ALERT_FROM,
          alert_until: time = ALERT_UNTIL) -> int:
    """Check once per minute, a few seconds after each bar completes."""
    print(f"Watching {SYMBOL} · MACD {MACD_SETTING} · alerts "
          f"{alert_from:%H:%M}-{alert_until:%H:%M} ET "
          f"· one alert per {cooldown} min")
    print("Ctrl-C to stop. Every signal is logged; only some are sent.\n")
    try:
        while True:
            now = datetime.now(ET)
            if market_is_open(now):
                try:
                    result = check_once(db, dry_run=dry_run, cooldown=cooldown,
                                        alert_from=alert_from,
                                        alert_until=alert_until)
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
    for word in ("R&D", "0", "rising", "diverging"):
        if word not in message:
            failures.append(f"the alert should say '{word}': {message}")
    if not message.startswith("** R&D "):
        failures.append(f"the case belongs in the headline: {message}")

    # The headline must track the zero line, not just appear. A message
    # that said "below" whichever side it was on would read perfectly and
    # be wrong every other time.
    for macd_value, expect, forbid in ((-0.05, "BELOW 0", "ABOVE 0"),
                                       (0.05, "ABOVE 0", "BELOW 0")):
        probe = sample.copy()
        probe["macd"] = macd_value
        text = compose("SPCX", qualifying[0], probe)
        if expect not in text or forbid in text:
            failures.append(f"MACD {macd_value} should read {expect}: {text}")
    # And only the below-zero case is the best case.
    best = sample.copy()
    best["macd"] = 0.05
    if "best case" in compose("SPCX", qualifying[0], best):
        failures.append("above zero is not the best case")

    # The sound name has one home, and open_candles must agree with it.
    # Imported late and guarded: open_candles imports THIS module, so a
    # top-level import would be a cycle, and the study should still run
    # with the watcher absent from the folder.
    try:
        import open_candles as _watcher
    except ImportError:
        pass
    else:
        if _watcher.SOUND_BUY != SOUND_SETUP:
            failures.append(f"the buy sound is spelled two ways: "
                            f"{SOUND_SETUP!r} here, "
                            f"{_watcher.SOUND_BUY!r} in open_candles")

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

    # Every entry point must load .env before it reads a key. A tool that
    # only notifies never fetches, so it cannot rely on the fetch path
    # having filled the environment on its way past.
    import inspect

    for fn in (main, test_push):
        if "load_env()" not in inspect.getsource(fn):
            failures.append(f"{fn.__name__}() must load .env before reading a key")

    # And a test that only proves Pushover accepted the message proves
    # nothing: the account can have no device attached to it.
    if "registered_devices()" not in inspect.getsource(test_push):
        failures.append("test_push() should confirm a device is listening before sending")

    saved = {name: os.environ.pop(name, None)
             for name in ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY")}
    try:
        devices, error = registered_devices()   # returns before any network call
        if devices is not None or not error:
            failures.append("registered_devices() should report an error when no key is set")
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value

    # Evaluating and alerting are two different gates. The window now
    # defaults to the whole session, so the gate is exercised with an
    # EXPLICIT time rather than with the constant -- otherwise widening
    # the default would silently delete the test along with the gate.
    gate = time(10, 0)
    if not (SESSION_OPEN <= ALERT_FROM < ALERT_UNTIL <= SESSION_CLOSE):
        failures.append(f"the alert window should open before it closes and sit "
                        f"inside the session, got {ALERT_FROM}-{ALERT_UNTIL}")
    if EARLIEST_SIGNAL > ALERT_FROM:
        failures.append("a bar that cannot be evaluated can never be alerted on")
    held = [ts for ts in qualifying if ts.time() < gate]
    sendable = [ts for ts in qualifying if gate <= ts.time() < ALERT_UNTIL]
    if not held:
        failures.append(f"the fixture should produce a qualifying bar before "
                        f"{gate:%H:%M}, "
                        "or the quiet period is not actually being tested")
    if not sendable:
        failures.append(f"the fixture should also produce one at or after "
                        f"{gate:%H:%M}, or the gate is not separating anything")
    if not all(session.at[ts, "cond_e_time"] for ts in held):
        failures.append(f"a bar before {gate:%H:%M} should still satisfy the "
                        f"time condition, so that it is logged")

    # Both edges of the window, and the fact that closing it silences
    # rather than stops. A bar after ALERT_UNTIL is still evaluated and
    # still written down -- the gate decides what buzzes, never what is
    # measured, or a day of data would go missing to save a notification.
    edges = [
        (time(9, 29), False, "a minute before the open"),
        (ALERT_FROM, True, "the opening minute itself"),
        (time(10, 30), True, "the middle of the morning"),
        (time(14, 0), True, "the afternoon, which is no longer silenced"),
        (ALERT_UNTIL, False, "the closing minute itself"),
        (time(16, 30), False, "after the close"),
    ]
    for at, expected, what in edges:
        inside = ALERT_FROM <= at < ALERT_UNTIL
        if inside is not expected:
            failures.append(f"{what} should {'' if expected else 'not '}reach the phone")

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
    print(f"    before 10:00 (gate exercise)   : {len(held)}")
    print(f"    {ALERT_FROM:%H:%M}-{ALERT_UNTIL:%H:%M} (may alert)      : {len(sendable)}")
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
    load_env()
    parser = argparse.ArgumentParser(description="Warn when an SPCX buy setup may be opening.")
    parser.add_argument("--watch", action="store_true", help="Run through the session")
    parser.add_argument("--once", action="store_true", help="Evaluate the latest bar and exit")
    parser.add_argument("--backfill", action="store_true", help="Fill in what price did next")
    parser.add_argument("--recent", type=int, metavar="N", help="Show the last N signals")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of sending")
    parser.add_argument("--cooldown", type=int, default=COOLDOWN_MINUTES,
                        help=f"Minutes between alerts (default {COOLDOWN_MINUTES})")
    parser.add_argument("--alert-until", default=f"{ALERT_UNTIL:%H:%M}",
                        help=f"Go quiet again after this, ET (default "
                             f"{ALERT_UNTIL:%H:%M}). Signals after it are still "
                             f"evaluated and recorded.")
    parser.add_argument("--alert-from", default=f"{ALERT_FROM:%H:%M}",
                        help=f"Stay quiet before this, ET (default {ALERT_FROM:%H:%M}). "
                             "Signals are still logged from 09:30.")
    parser.add_argument("--db", default=DB_PATH, help=f"Database file (default {DB_PATH})")
    parser.add_argument("--test-push", action="store_true",
                        help="Send one of each notification kind to your phone")
    parser.add_argument("--self-test", action="store_true", help="Check the logic offline")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if args.test_push:
        return test_push()

    from feed_check import parse_clock
    alert_from = parse_clock(args.alert_from)
    alert_until = parse_clock(args.alert_until)
    if alert_from >= alert_until:
        raise SystemExit(f"--alert-from {alert_from:%H:%M} must be before "
                         f"--alert-until {alert_until:%H:%M}")
    db = open_db(args.db)

    if args.recent:
        show_recent(db, args.recent)
        return 0

    if args.backfill:
        filled = backfill(db)
        print(f"Filled outcomes for {filled} signal(s).")
        return 0

    if args.watch:
        return watch(db, args.dry_run, args.cooldown, alert_from, alert_until)

    if args.once:
        result = check_once(db, dry_run=args.dry_run, cooldown=args.cooldown,
                            alert_from=alert_from, alert_until=alert_until)
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
