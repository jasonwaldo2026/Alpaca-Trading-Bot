"""
patch_remove_retired.py -- hide Market Scanner's retired detectors (2026-10-16).

Market Scanner's October cleanup removed nine detectors: rsi, canslim,
congressional_trade, stock_sentiment, squeeze, whale_flow,
social_dominance, active_addr and rebound_watch. Scanner Studio kept
showing them, because its Signal Performance screens list every type
that has OLD outcomes, and Rebound Watch still had its own screen.

Three files change:

  analytics.py   one list, REMOVED_SIGNAL_TYPES; outcomes of those types
                 are left out of every chart, filter, leaderboard, triage
                 readout and symbol Outcomes table. Nothing is deleted --
                 take a name off the list and its history comes back.
  loaders.py     the snapshot load skips them too, so the Sheets browser
                 agrees with the charts.
  web/app.js     Rebound Watch is taken out of the screens; a help note
                 that used whale_flow as its example is reworded.

All three are checked against the exact versions this was written for
(by hash) before anything is touched. Each gets a timestamped backup,
keeps its own line endings, and if a patched .py does not compile every
file is put back. Run it again and it says it is already done.

    cd C:\\Dev\\Scanner-Studio
    python patch_remove_retired.py --check     # writes nothing
    python patch_remove_retired.py

Read-only towards Market Scanner. Nothing here can place an order.
"""

from __future__ import annotations

import hashlib
import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "REMOVED_SIGNAL_TYPES"

ANALYTICS_LIST = '''import store

#: Detectors removed from Market Scanner in its October 2026 cleanup. Their
#: old outcomes stay in the databases but are left out of every screen, so
#: the charts and filters show only what still runs. Take a name off this
#: list to bring its history back.
REMOVED_SIGNAL_TYPES = (
    "rsi", "canslim", "congressional_trade", "stock_sentiment", "squeeze",
    "whale_flow", "social_dominance", "active_addr", "rebound_watch",
)
_RETIRED = ",".join("?" * len(REMOVED_SIGNAL_TYPES))
'''

FILTER_OPTIONS_OLD = '''        types = [r[0] for r in conn.execute(
            "SELECT signal_type, COUNT(*) c FROM outcomes WHERE signal_type IS NOT NULL "
            "GROUP BY 1 ORDER BY c DESC")]
        horizons = [r[0] for r in conn.execute(
            "SELECT DISTINCT horizon_days FROM outcomes WHERE horizon_days IS NOT NULL "
            "ORDER BY 1")]
        rng = conn.execute(
            "SELECT MIN(alert_date) a, MAX(alert_date) b FROM outcomes "
            "WHERE alert_date <> ''").fetchone()
        resolved = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE return_pct IS NOT NULL").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
'''

FILTER_OPTIONS_NEW = '''        keep = f"(signal_type IS NULL OR signal_type NOT IN ({_RETIRED}))"
        retired = REMOVED_SIGNAL_TYPES
        types = [r[0] for r in conn.execute(
            "SELECT signal_type, COUNT(*) c FROM outcomes WHERE signal_type IS NOT NULL "
            f"AND {keep} GROUP BY 1 ORDER BY c DESC", retired)]
        horizons = [r[0] for r in conn.execute(
            "SELECT DISTINCT horizon_days FROM outcomes WHERE horizon_days IS NOT NULL "
            f"AND {keep} ORDER BY 1", retired)]
        rng = conn.execute(
            "SELECT MIN(alert_date) a, MAX(alert_date) b FROM outcomes "
            f"WHERE alert_date <> '' AND {keep}", retired).fetchone()
        resolved = conn.execute(
            f"SELECT COUNT(*) FROM outcomes WHERE return_pct IS NOT NULL AND {keep}",
            retired).fetchone()[0]
        total = conn.execute(f"SELECT COUNT(*) FROM outcomes WHERE {keep}", retired).fetchone()[0]
'''

WHERE_OLD = '''    # Unresolved alerts carry no return; they are not evidence either way.
'''
WHERE_NEW = '''    # Retired detectors are history nobody is trading any more.
    clauses.append(f"(o.signal_type IS NULL OR o.signal_type NOT IN ({_RETIRED}))")
    params += list(REMOVED_SIGNAL_TYPES)
    # Unresolved alerts carry no return; they are not evidence either way.
'''

LOADERS_OLD = '''        for r in src.execute(_OUTCOMES_Q):
            ts = r["alert_ts"] or ""
'''
LOADERS_NEW = '''        from analytics import REMOVED_SIGNAL_TYPES   # retired in Market Scanner
        for r in src.execute(_OUTCOMES_Q):
            if r["signal_type"] in REMOVED_SIGNAL_TYPES:
                continue
            ts = r["alert_ts"] or ""
'''

NOTE_OLD = ('a <b>fast_volume</b> score is a\n'
            '       volume ratio, a <b>whale_flow</b> score is a z-score. Bucketing within each type is what makes\n'
            '       these comparable.')
NOTE_NEW = ('a <b>fast_volume</b> score is a\n'
            '       volume ratio, and other detectors score on scales of their own. Bucketing within each type is\n'
            '       what makes these comparable.')

BOOT_OLD = 'placeNavRow();\n\nboot();'
BOOT_NEW = ('placeNavRow();\n\n'
            '// Rebound Watch was retired with Market Scanner\'s rebound_watch\n'
            '// (2026-10-16, REMOVED_SIGNAL_TYPES): drop any button still pointing at it.\n'
            'document.querySelectorAll(\'[data-view="rebound"]\').forEach(b => b.remove());\n\n'
            'boot();')

FILES = {
    "analytics.py": {
        "sha256": "8ace377bef8ddfb2b14e4fc5e48b7157f48eda62ed9afd8ae7910e273cb963a0",
        "edits": [("import store\n", ANALYTICS_LIST), (WHERE_OLD, WHERE_NEW),
                  (FILTER_OPTIONS_OLD, FILTER_OPTIONS_NEW)],
        "python": True,
    },
    "loaders.py": {
        "sha256": "bf76ea26a38957d06eaaa5acf7272ebc8f31ba3d355818b878a1aa1ad8e1cde9",
        "edits": [(LOADERS_OLD, LOADERS_NEW)],
        "python": True,
    },
    "web/app.js": {
        "sha256": "223311720738eba9e5dfafe2097f6f9e61de7f55ff70c7d1e8cabc499729b624",
        "edits": [(NOTE_OLD, NOTE_NEW), ("  rebound: viewRebound,\n", ""),
                  (BOOT_OLD, BOOT_NEW)],
        "python": False,
    },
}


def plan(root: Path) -> tuple[dict, list[str]]:
    """Every file's new bytes, or the reasons nothing may be written."""
    out, problems = {}, []
    for rel, spec in FILES.items():
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel}: not found")
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8").replace("\r\n", "\n")
        if MARKER in text:
            out[rel] = None                              # already done
            continue
        if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
            problems.append(f"{rel}: not the version this was written for")
            continue
        for old, new in spec["edits"]:
            if text.count(old) != 1:
                problems.append(f"{rel}: an expected passage was not found exactly once")
                break
            text = text.replace(old, new)
        else:
            if b"\r\n" in raw:
                text = text.replace("\n", "\r\n")
            out[rel] = text.encode("utf-8")
    return out, problems


def main() -> int:
    root = Path(__file__).resolve().parent
    new, problems = plan(root)
    if problems:
        print("STOP - nothing changed:")
        for p in problems:
            print(f"  {p}")
        return 1
    todo = {rel: data for rel, data in new.items() if data is not None}
    if not todo:
        print("already patched - nothing to do")
        return 0
    if "--check" in sys.argv:
        print("ready: " + ", ".join(todo) + " match; run without --check to patch")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = {}
    for rel in todo:
        path = root / rel
        backups[rel] = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backups[rel])
    for rel, data in todo.items():
        (root / rel).write_bytes(data)
    for rel in todo:
        if FILES[rel]["python"]:
            try:
                py_compile.compile(str(root / rel), doraise=True)
            except py_compile.PyCompileError as exc:
                for r, b in backups.items():
                    shutil.copy2(b, root / r)
                print(f"STOP: {rel} did not compile; every file put back ({exc.msg})")
                return 1
    for rel, b in backups.items():
        print(f"patched {rel} - backup {b.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
