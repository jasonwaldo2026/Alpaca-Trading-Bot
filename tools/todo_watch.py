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
  YOUR LIST   the lines of todo.txt, beside this file. It stays on this PC
              and is never published. One task per line; a line starting
              "2026-10-26:" shows from that day on; blank lines and lines
              starting "#" are skipped. Delete a line when it is done.

A PC that was off at 9:00 sends it when it comes back, until 9:20; after
that the day's list is skipped rather than sent in the middle of the open.

Read-only: it reads two heartbeat files and a text file, and knocks on
IB Gateway's port without logging in. Imported by merger_watch.py.

    python todo_watch.py              # print today's list now
    python todo_watch.py --self-test
"""

from __future__ import annotations

import os
import re
import socket
import sys
from datetime import date, datetime, time, timedelta
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
            gateway: bool, alarms_path: str, rules_path: str) -> str:
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
            rules_path: str = heartbeat.DAY_RULES) -> Optional[str]:
    """The day's message, once, on a weekday between 9:00 and 9:20 ET.
    `now` is Eastern; the heartbeat files hold naive Eastern times."""
    local = now.replace(tzinfo=None)
    if local.weekday() >= 5 or not SEND_AT <= local.time() <= SEND_LATEST:
        return None
    if state.get("todo_sent") == local.date().isoformat():
        return None
    state["todo_sent"] = local.date().isoformat()
    return message(local, events, read_tasks(local.date(), todo_path), gateway(),
                   alarms_path, rules_path)


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
        args = dict(gateway=lambda: False, todo_path=todo, alarms_path=alarms, rules_path=rules)
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
                  os.path.join(HERE, heartbeat.DAY_RULES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
