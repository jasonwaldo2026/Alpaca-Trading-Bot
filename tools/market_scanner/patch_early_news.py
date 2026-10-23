"""
patch_early_news.py -- Market Scanner's news from 4 AM, and on to 8 PM (2026-10-23).

Market Scanner's fast_scan job runs every 5 minutes, 08:00-16:30 ET on
weekdays, in two stages: (1) volume spikes across the whole universe, from
a day of Alpaca bars, and (2) ONE call to FMP's market-wide news, kept for
the symbols already tracked. So a headline that broke at 5 AM reached
Scanner Studio's Incoming Alerts at 8:02 at the earliest.

Three edits to src/main.py:

  1. fast_scan(news_only=False). With news_only=True the Alpaca bar fetch
     is skipped, so stage 1 finds nothing and costs nothing; stage 2 runs
     exactly as it does in the day.
  2. Three more schedule entries, news only, every fast_scan_minutes on
     fast_scan_days: 04:00-07:55, 16:35-16:55 and 17:00-19:55 ET.
  3. Comments saying why.

The daytime 08:00-16:30 jobs are not changed. About 90 more FMP calls a day.

It refuses to touch the file unless every line it changes is exactly the
version it was written against, keeps a timestamped backup, keeps the
file's own line endings, and puts the backup back if the result does not
compile. Run it again and it says it is already done.

    cd C:\\Dev\\Market-Scanner
    python patch_early_news.py --check     # says what it would do, writes nothing
    python patch_early_news.py             # patches src\\main.py
    python patch_early_news.py --self-test # tries it on a copy, no real file

Then restart Market Scanner's service so the new schedule loads.
Nothing here can place an order or trade.
"""

from __future__ import annotations

import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

MARKER = "EARLY NEWS (2026-10-23)"

SIGNATURE_OLD = 'def fast_scan():\n    log.info("fast scan starting")\n'
SIGNATURE_NEW = '''def fast_scan(news_only: bool = False):
    # EARLY NEWS (2026-10-23): news_only=True runs stage 2 (news) alone -
    # the 04:00-07:55 and 16:35-19:55 ET registrations in main() use it so
    # overnight headlines reach the Live Feed before 08:00. Stage 1 needs a
    # day of bars and only means something in market hours, so it is skipped.
    log.info("fast scan starting%s", " (news only)" if news_only else "")
'''

FETCH_OLD = "        bars_by_symbol = alpaca_bars.fetch_daily_bars(symbols, days=1)\n"
FETCH_NEW = ("        # news_only: no bars, so stage 1 finds nothing and costs nothing.\n"
             "        bars_by_symbol = ({} if news_only else\n"
             "                          alpaca_bars.fetch_daily_bars(symbols, days=1))\n")

SCHEDULE_OLD = '''    sched.add_job(fast_scan, CronTrigger(
        day_of_week=fast_days, hour="16", minute=f"0-30/{fast_minutes}", timezone=tz),
        id="fast_scan_close", misfire_grace_time=300)
'''
SCHEDULE_NEW = SCHEDULE_OLD + '''
    # EARLY NEWS (2026-10-23): stage 2 (news) only, either side of the
    # window above, so overnight and early-morning headlines reach the
    # Live Feed before 08:00 and evening ones are not left until tomorrow.
    # One FMP call per run, about 90 a day. Volume stays market hours.
    for job_id, hours, minutes in (
            ("fast_news_early", "4-7", f"*/{fast_minutes}"),
            ("fast_news_late", "16", f"35-55/{fast_minutes}"),
            ("fast_news_evening", "17-19", f"*/{fast_minutes}")):
        sched.add_job(fast_scan, CronTrigger(
            day_of_week=fast_days, hour=hours, minute=minutes, timezone=tz),
            kwargs={"news_only": True}, id=job_id, misfire_grace_time=300)
'''


def plan(text: str) -> Tuple[str, List[str]]:
    """The patched text, or problems. Every edit must match exactly once,
    and the bar fetch must be the one inside fast_scan()."""
    problems = []
    if text.count(SIGNATURE_OLD) != 1:
        problems.append("fast_scan()'s first two lines are not the expected version")
    if text.count(SCHEDULE_OLD) != 1:
        problems.append("fast_scan's 16:00-16:30 schedule entry is not the expected version")
    if problems:
        return text, problems
    start = text.index(SIGNATURE_OLD)
    end = text.find("\ndef ", start + 1)
    body = text[start:end if end != -1 else len(text)]
    if body.count(FETCH_OLD) != 1:
        problems.append("fast_scan()'s Alpaca bar fetch is not the expected version")
        return text, problems
    body = body.replace(SIGNATURE_OLD, SIGNATURE_NEW).replace(FETCH_OLD, FETCH_NEW)
    text = text[:start] + body + text[start + len(text[start:end if end != -1 else len(text)]):]
    return text.replace(SCHEDULE_OLD, SCHEDULE_NEW), []


def patch(path: Path, check: bool) -> int:
    if not path.is_file():
        print(f"STOP: {path} not found. Run this from C:\\Dev\\Market-Scanner.")
        return 1
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if MARKER in text:
        print("already patched - nothing to do")
        return 0
    new, problems = plan(text)
    if problems:
        print("STOP: nothing changed:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if check:
        print("ready: all three places match; run without --check to patch")
        return 0
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    path.write_bytes((new.replace("\n", "\r\n") if crlf else new).encode("utf-8"))
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        shutil.copy2(backup, path)
        print(f"STOP: the patched file did not compile, original restored ({exc.msg})")
        return 1
    print(f"patched - backup is {backup.name}")
    print("Now restart Market Scanner so the new schedule loads.")
    return 0


# ---------------------------------------------------------------- self-test

FIXTURE = '''import logging
log = logging.getLogger("t")
FETCHED = []


class alpaca_bars:
    @staticmethod
    def fetch_daily_bars(symbols, days=1):
        FETCHED.append(list(symbols))
        return {s: [1] for s in symbols}


def stock_scan():
    symbols = ["X"]
    try:
        bars_by_symbol = alpaca_bars.fetch_daily_bars(symbols, days=1)
    except Exception:
        return
    return bars_by_symbol


def fast_scan():
    log.info("fast scan starting")
    symbols = ["AAA", "BBB"]
    try:
        bars_by_symbol = alpaca_bars.fetch_daily_bars(symbols, days=1)
    except Exception as e:
        return None
    volume = len(bars_by_symbol)
    news = "news ran"
    return volume, news


def register(sched, CronTrigger, tz, fast_days, fast_minutes):
    sched.add_job(fast_scan, CronTrigger(
        day_of_week=fast_days, hour="8-15", minute=f"*/{fast_minutes}", timezone=tz),
        id="fast_scan_main", misfire_grace_time=300)
    sched.add_job(fast_scan, CronTrigger(
        day_of_week=fast_days, hour="16", minute=f"0-30/{fast_minutes}", timezone=tz),
        id="fast_scan_close", misfire_grace_time=300)
'''


def self_test() -> int:
    import tempfile

    failures = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "main.py"
        path.write_bytes(FIXTURE.replace("\n", "\r\n").encode("utf-8"))
        ok(patch(path, check=True) == 0 and MARKER not in path.read_text(),
           "--check writes nothing")
        ok(patch(path, check=False) == 0, "the fixture patches")
        raw = path.read_bytes()
        ok(b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""),
           "the file keeps its CRLF line endings")
        ok(len(list(Path(folder).glob("main.py.bak-*"))) == 1, "a backup is kept")
        ok(patch(path, check=False) == 0 and len(list(Path(folder).glob("main.py.bak-*"))) == 1,
           "a second run does nothing")

        space: dict = {}
        exec(compile(raw.decode().replace("\r\n", "\n"), str(path), "exec"), space)
        ok(space["fast_scan"]() == (2, "news ran") and space["FETCHED"] == [["AAA", "BBB"]],
           "the daytime run is unchanged: bars fetched, both stages")
        space["FETCHED"].clear()
        ok(space["fast_scan"](news_only=True) == (0, "news ran") and space["FETCHED"] == [],
           "news only: no bar fetch, no volume, news still runs")
        ok(space["stock_scan"]() == {"X": [1]} and space["FETCHED"] == [["X"]],
           "the same fetch line in another job is left alone")

        jobs = []

        class Sched:
            def add_job(self, fn, trigger, **kw):
                jobs.append((fn.__name__, trigger, kw))

        space["register"](Sched(), lambda **kw: kw, "America/New_York", "mon-fri", 5)
        ids = [kw["id"] for _, _, kw in jobs]
        ok(ids == ["fast_scan_main", "fast_scan_close", "fast_news_early",
                   "fast_news_late", "fast_news_evening"], f"the jobs: {ids}")
        early, late, evening = jobs[2:]
        ok(early[1]["hour"] == "4-7" and early[1]["minute"] == "*/5"
           and early[2]["kwargs"] == {"news_only": True}, f"04:00-07:55 news only: {early}")
        ok(late[1]["hour"] == "16" and late[1]["minute"] == "35-55/5", f"16:35-16:55: {late}")
        ok(evening[1]["hour"] == "17-19", f"17:00-19:55: {evening}")
        ok(all(t["day_of_week"] == "mon-fri" for _, t, _ in jobs), "weekdays, as configured")
        ok("kwargs" not in jobs[0][2] and "kwargs" not in jobs[1][2],
           "the daytime jobs still run both stages")
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger(day_of_week="mon-fri", hour="16", minute="35-55/5",
                        timezone="America/New_York")
        except ImportError:
            pass                                    # checked on the real PC instead

        # A file that is not the expected version is refused, untouched.
        other = Path(folder) / "other.py"
        changed = FIXTURE.replace('hour="16", minute=f"0-30/', 'hour="16", minute=f"0-45/')
        other.write_text(changed)
        ok(patch(other, check=False) == 1 and other.read_text() == changed,
           "a different schedule is refused and left alone")
        no_fetch = FIXTURE.replace(
            "    try:\n        bars_by_symbol = alpaca_bars.fetch_daily_bars(symbols, days=1)\n"
            "    except Exception as e:", "    try:\n        bars_by_symbol = {}\n"
            "    except Exception as e:")
        other.write_text(no_fetch)
        ok(patch(other, check=False) == 1 and other.read_text() == no_fetch,
           "a fast_scan without the expected fetch is refused, even though "
           "another job has that line")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("patch_early_news self-test passed")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    check = "--check" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = Path(args[0]) if args else Path("src") / "main.py"
    return patch(path, check)


if __name__ == "__main__":
    raise SystemExit(main())
