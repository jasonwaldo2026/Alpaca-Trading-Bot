"""
todo_watch.py -- the 9:00 AM "TO DO today" message (2026-10-24).

Every weekday at 9:00 ET the merger watch sends one normal message, before
the 9:30 open gets busy, in three parts:

  CHECKS      done by the PC itself, now:
                IB Gateway answering on its API port (VIX, IBKR news, the
                overnight alarms and the day-rules watcher need it)
                the SPCX alarms running (their heartbeat file is fresh)
                the day-rules watcher running (likewise)
  TODAY       unlocks, earnings and launches dated today, from the calendar
  HOT LIST    the top 5 of the Market Scanner's Pre-Market Hot List, the
              list Scanner Studio shows (25 Oct), read from its database
              without changing it. On the phone each symbol is a link to
              its Robinhood page. A list not rebuilt today says so.
  YOUR LIST   the lines of todo.txt, beside this file. It stays on this PC
              and is never published. One task per line; a line starting
              "2026-10-26:" shows from that day on; blank lines and lines
              starting "#" are skipped. Delete a line when it is done.

A PC that was off at 9:00 sends it when it comes back, until 9:20; after
that the day's list is skipped rather than sent in the middle of the open.

Read-only: it reads two heartbeat files, a text file and the Market
Scanner's database (opened read-only), and knocks on IB Gateway's port
without logging in. Imported by merger_watch.py.

    python todo_watch.py              # print today's list now
    python todo_watch.py --self-test
"""

from __future__ import annotations

import html
import os
import re
import socket
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

import heartbeat

HERE = os.path.dirname(os.path.abspath(__file__))
TODO_PATH = os.path.join(HERE, "todo.txt")
SEND_AT, SEND_LATEST = time(9, 0), time(9, 20)
IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.environ.get("IBKR_PORT", "4001"))
#: A heartbeat older than this means the watcher is not running.
STALE = timedelta(minutes=10)
DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*:\s*(.+)$")
ET = ZoneInfo("America/New_York")
#: The Market Scanner beside this folder: C:\Dev\Market-Scanner next to
#: C:\Dev\Feed-Check. MARKET_SCANNER_DB in the environment overrides it.
HOT_DB = os.environ.get("MARKET_SCANNER_DB") or os.path.join(
    os.path.dirname(HERE), "Market-Scanner", "data", "scanner.db")
HOT_TOP = 5
HOT_HEAD = "HOT LIST"
HOT_ROW = re.compile(r"^(\d+)\. ([A-Z][A-Z0-9.]*) ")
#: Robinhood's stock page; on a phone with the app it usually opens the app.
STOCK_LINK = "https://robinhood.com/stocks/{symbol}"
#: Pushover's limit for one message.
PUSHOVER_MAX = 1024


def tasks(text: str, today: date) -> List[str]:
    """The lines of todo.txt that apply today."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = DATED.match(line)
        if m:
            try:
                if date.fromisoformat(m.group(1)) > today:
                    continue
            except ValueError:
                pass                       # a bad date: show the line as written
            else:
                line = m.group(2).strip()
        out.append(line)
    return out


def read_tasks(today: date, path: str = TODO_PATH) -> Optional[List[str]]:
    """Today's tasks, or None if there is no todo.txt yet."""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return tasks(handle.read(), today)
    except FileNotFoundError:
        return None
    except OSError:
        return []


def hot_list(path: str = HOT_DB, top: int = HOT_TOP):
    """(rows, updated) from the Market Scanner's hot_list table, best rank
    first; updated is the list's last rebuild in Eastern time. None when
    there is no database or table to read."""
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM hot_list ORDER BY rank LIMIT ?", (top,))]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    stamps = [r.get("updated_at") for r in rows if r.get("updated_at")]
    updated = None
    if stamps:
        try:
            when = datetime.fromisoformat(max(stamps))
            updated = (when.astimezone(ET) if when.tzinfo else when).replace(tzinfo=None)
        except ValueError:
            pass
    return rows, updated


def _short(n) -> str:
    if n is None:
        return "?"
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


def hot_lines(found, today: date) -> List[str]:
    if found is None:
        return [f"{HOT_HEAD}", "(the Market Scanner's list could not be read)"]
    rows, updated = found
    if not rows:
        return [f"{HOT_HEAD}", "(empty: the Market Scanner has not built it)"]
    if updated is None or updated.date() != today:
        last = f"{updated:%a %I:%M %p}" if updated else "unknown"
        head = f"{HOT_HEAD} - NOT updated today (last {last}): check the Market Scanner"
    else:
        head = f"{HOT_HEAD} at {updated:%I:%M %p}"
    out = [head]
    for r in rows:
        price, change = r.get("price"), r.get("change_pct")
        parts = [f"{r.get('rank')}. {r.get('symbol')}",
                 f"${price:,.2f}" if price is not None else "?",
                 f"{change:+.1f}%" if change is not None else "",
                 f"float {_short(r.get('float_shares'))}"]
        out.append(" ".join(p for p in parts if p)
                   + (f" - {r['verdict']}" if r.get("verdict") else ""))
    return out


def as_html(text: str) -> Optional[str]:
    """The message for Pushover's html mode: everything escaped, each hot
    list symbol a link to its Robinhood page. None if it would be too long,
    so the caller sends the plain text instead."""
    out, in_hot = [], False
    for line in text.split("\n"):
        if line.startswith(HOT_HEAD):
            in_hot = True
        elif not line:
            in_hot = False
        m = HOT_ROW.match(line) if in_hot else None
        if m:
            symbol = m.group(2)
            link = html.escape(STOCK_LINK.format(symbol=symbol), quote=True)
            rest = html.escape(line[m.end(2):])
            out.append(f'{m.group(1)}. <a href="{link}">{symbol}</a>{rest}')
        else:
            out.append(html.escape(line))
    body = "\n".join(out)
    return body if len(body) <= PUSHOVER_MAX else None


def gateway_up(host: str = IBKR_HOST, port: int = IBKR_PORT, timeout: float = 3.0) -> bool:
    """Whether IB Gateway's API port answers. It knocks; it never logs in."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def watcher_line(path: str, name: str, now: datetime) -> str:
    seen = heartbeat.read(path)
    if seen is None:
        return f"FIX  {name}: not running (never started on this PC)"
    when = seen[0]
    # The SPCX alarms stamp Eastern time with its offset, the day-rules
    # watcher without; compare both as plain Eastern time.
    if when.tzinfo is not None:
        when = when.astimezone(ET).replace(tzinfo=None)
    if now - when > STALE:
        return f"FIX  {name}: NOT running since {when:%a %I:%M %p}"
    return f"OK   {name}: running"


def message(now: datetime, events: list, todo: Optional[List[str]],
            gateway: bool, alarms_path: str, rules_path: str, hot=None) -> str:
    lines = [f"TO DO today, {now:%a %b %d}", "", "CHECKS"]
    lines.append("OK   IB Gateway: answering" if gateway
                 else f"FIX  IB Gateway: not answering on port {IBKR_PORT} - log in")
    lines.append(watcher_line(alarms_path, "SPCX alarms", now))
    lines.append(watcher_line(rules_path, "Day-rules watcher", now))
    today = [e for e in events if getattr(e, "day", None) == now.date()]
    if today:
        lines += ["", "TODAY"]
        for e in today:
            lines.append(f"- {e.title}{e.flag()}, {e.when_words()}")
    lines += [""] + hot_lines(hot, now.date())
    lines += ["", "YOUR LIST"]
    if todo is None:
        lines.append("(no todo.txt in the Feed-Check folder yet)")
    elif not todo:
        lines.append("(nothing on it)")
    else:
        lines += [f"{i}. {t}" for i, t in enumerate(todo, 1)]
    return "\n".join(lines)


def morning(state: dict, now: datetime, events: list,
            gateway: Callable[[], bool] = gateway_up,
            todo_path: str = TODO_PATH,
            alarms_path: str = heartbeat.SPCX_ALARMS,
            rules_path: str = heartbeat.DAY_RULES,
            hot_path: str = HOT_DB) -> Optional[str]:
    """The day's message, once, on a weekday between 9:00 and 9:20 ET.
    `now` is Eastern; the heartbeat files hold naive Eastern times."""
    local = now.replace(tzinfo=None)
    if local.weekday() >= 5 or not SEND_AT <= local.time() <= SEND_LATEST:
        return None
    if state.get("todo_sent") == local.date().isoformat():
        return None
    state["todo_sent"] = local.date().isoformat()
    return message(local, events, read_tasks(local.date(), todo_path), gateway(),
                   alarms_path, rules_path, hot_list(hot_path))


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    import tempfile
    from types import SimpleNamespace as NS

    failures = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    monday = date(2026, 10, 26)
    text = ("# my list\n\n2026-10-26: Run the Market Scanner check\n"
            "2026-10-27: Not yet\nDecide on the day-rules fix\n"
            "2026-13-01: a bad date shows as written\n   \n")
    got = tasks(text, monday)
    ok(got == ["Run the Market Scanner check", "Decide on the day-rules fix",
               "2026-13-01: a bad date shows as written"],
       f"dated lines from their day, comments and blanks skipped: {got}")
    ok(tasks(text, date(2026, 10, 27))[1] == "Not yet", "a dated line shows from its day on")

    unlock = NS(day=monday, title="SPCX share unlock, 328M shares",
                flag=lambda: " [UNCONFIRMED]", when_words=lambda: "shares sellable from the open")
    later = NS(day=date(2026, 12, 8), title="later", flag=lambda: "", when_words=lambda: "")
    with tempfile.TemporaryDirectory() as folder:
        todo = os.path.join(folder, "todo.txt")
        alarms = os.path.join(folder, heartbeat.SPCX_ALARMS)
        rules = os.path.join(folder, heartbeat.DAY_RULES)
        nine = datetime(2026, 10, 26, 9, 0)
        heartbeat.write(alarms, (nine - timedelta(minutes=1)).replace(tzinfo=ET))
        with open(todo, "w", encoding="utf-8") as handle:
            handle.write(text)
        st: dict = {}
        hot_db = os.path.join(folder, "scanner.db")
        args = dict(gateway=lambda: False, todo_path=todo, alarms_path=alarms, rules_path=rules,
                    hot_path=hot_db)
        ok(morning(st, datetime(2026, 10, 26, 8, 59), [unlock], **args) is None, "not before 9:00")
        msg = morning(st, nine, [unlock, later], **args)
        ok(msg is not None and msg.startswith("TO DO today, Mon Oct 26"), f"at 9:00: {msg}")
        ok("FIX  IB Gateway: not answering on port" in msg, f"Gateway down is named: {msg}")
        ok("OK   SPCX alarms: running" in msg, "a fresh heartbeat reads as running")
        ok("FIX  Day-rules watcher: not running (never started on this PC)" in msg,
           "a watcher that never ran says so")
        ok("- SPCX share unlock, 328M shares [UNCONFIRMED], shares sellable from the open" in msg
           and "later" not in msg, f"today's events only: {msg}")
        ok("1. Run the Market Scanner check\n2. Decide on the day-rules fix" in msg,
           f"the list, numbered: {msg}")
        ok(morning(st, nine + timedelta(minutes=5), [unlock], **args) is None, "once a day")
        heartbeat.write(rules, nine - timedelta(hours=3))
        msg = morning({}, nine + timedelta(minutes=20),
                      [], **dict(args, gateway=lambda: True))
        ok(msg and "OK   IB Gateway: answering" in msg
           and "FIX  Day-rules watcher: NOT running since Mon 06:00 AM" in msg
           and "TODAY" not in msg, f"9:20 still sends; a stale heartbeat is named: {msg}")
        ok(morning({}, nine + timedelta(minutes=21), [], **args) is None, "not after 9:20")
        from datetime import timezone
        heartbeat.write(alarms, (nine - timedelta(hours=3)).replace(tzinfo=ET).astimezone(timezone.utc))
        msg = morning({}, nine, [], **args)
        ok("FIX  SPCX alarms: NOT running since Mon 06:00 AM" in msg,
           f"a heartbeat stamped in another zone is compared as Eastern time: {msg}")
        ok(morning({}, datetime(2026, 10, 24, 9, 0), [], **args) is None, "not on Saturday")
        os.remove(todo)
        msg = morning({}, nine, [], **args)
        ok("(no todo.txt in the Feed-Check folder yet)" in msg, "no file says so")
        with open(todo, "w", encoding="utf-8") as handle:
            handle.write("# all done\n")
        ok("(nothing on it)" in morning({}, nine, [], **args), "an empty list says so")
        from zoneinfo import ZoneInfo
        aware = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        ok(morning({}, aware, [], **args) is not None, "an Eastern-aware clock works too")

        # The hot list: read-only, top five, each a Robinhood link on the phone.
        ok("(the Market Scanner's list could not be read)" in morning({}, nine, [], **args)
           and not os.path.exists(hot_db), "no database: says so, and does not create one")
        db = sqlite3.connect(hot_db)
        db.execute("CREATE TABLE hot_list (rank INTEGER, symbol TEXT, price REAL, change_pct REAL,"
                   " float_shares REAL, verdict TEXT, updated_at TEXT)")
        db.commit()
        ok("(empty: the Market Scanner has not built it)" in morning({}, nine, [], **args),
           "an empty list says so")
        stamp = "2026-10-26T12:59:06+00:00"          # 08:59 Eastern
        db.executemany("INSERT INTO hot_list VALUES (?, ?, ?, ?, ?, ?, ?)",
                       [(1, "BKYI", 3.2595, 8.65, 1335843.0, "Trade Approved", stamp),
                        (2, "CCI", 91.5, 6.04, 425024440.0, "Avoid", stamp),
                        (6, "SIXTH", 1.0, 1.0, 1.0, "", stamp),
                        (3, "AXTI", 53.31, -4.14, 61608463.0, None, stamp),
                        (4, "D", 10.0, None, None, "Middle", stamp),
                        (5, "E", None, 2.0, 2.5e9, "Middle", stamp)])
        db.commit()
        db.close()
        before = os.path.getmtime(hot_db)
        msg = morning({}, nine, [], **args)
        ok("HOT LIST at 08:59 AM\n1. BKYI $3.26 +8.7% float 1.3M - Trade Approved\n"
           "2. CCI $91.50 +6.0% float 425.0M - Avoid\n3. AXTI $53.31 -4.1% float 61.6M\n"
           "4. D $10.00 float ? - Middle\n5. E ? +2.0% float 2.5B - Middle\n\nYOUR LIST" in msg
           and "SIXTH" not in msg, f"the top five, best first: {msg}")
        ok(os.path.getmtime(hot_db) == before, "the Market Scanner's database is not written")
        page = as_html("TO DO S&P <b>\n\n" + msg[msg.index("HOT LIST"):] + "\n1. ABC not a stock ")
        ok(page is not None
           and '1. <a href="https://robinhood.com/stocks/BKYI">BKYI</a> $3.26 +8.7%' in page
           and '<a href="https://robinhood.com/stocks/AXTI">AXTI</a>' in page
           and "S&amp;P &lt;b&gt;" in page and "1. ABC not a stock" in page,
           f"hot list symbols link to Robinhood, nothing else is linked, the rest escaped: {page}")
        ok(as_html("x" * (PUSHOVER_MAX + 1)) is None, "too long for Pushover: send it plain")
        monday = morning({}, datetime(2026, 10, 27, 9, 0), [], **args)
        ok("HOT LIST - NOT updated today (last Mon 08:59 AM): check the Market Scanner" in monday,
           f"yesterday's list says so: {monday}")

    ok(gateway_up("127.0.0.1", 1, timeout=0.5) is False, "a closed port is not a Gateway")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("todo_watch self-test passed")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if "--self-test" in sys.argv:
        return self_test()
    from zoneinfo import ZoneInfo

    import calendar_watch

    now = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    print(message(now, calendar_watch.all_events(now.date()), read_tasks(now.date()),
                  gateway_up(), os.path.join(HERE, heartbeat.SPCX_ALARMS),
                  os.path.join(HERE, heartbeat.DAY_RULES), hot_list()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
