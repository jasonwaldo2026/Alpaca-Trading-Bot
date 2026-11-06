"""
add_alarms_option.py -- add "A  SPCX alarms" to the SPCX Morning menu (2026-10-16).

Edits the "SPCX Morning.bat" beside it in place, adding ONE option and
touching nothing else -- every existing option, --dry-run included, stays
exactly as it was:

    A   SPCX alarms   04:00 to 20:00, up and down, TO YOUR PHONE

which runs

    python open_candles.py --from 04:00 --until 20:00 --rings-only --persist 30

Keeps a timestamped backup and the file's own line endings, and says
"already added" on a second run.

    cd C:\\Dev\\Feed-Check
    python add_alarms_option.py
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

MENU_AFTER = ("echo     3   MACD alerts only       logs from 09:30, alerts from 09:40\n"
              "echo.\n")
MENU_LINES = ("echo     A   SPCX alarms            04:00 to 20:00, up and down, TO YOUR PHONE\n"
              "echo.\n")
CHOICE_AFTER = 'if "%choice%"=="3" goto alerts\n'
CHOICE_LINE = 'if /i "%choice%"=="A" goto alarms\n'
HANDLER_BEFORE = ":testpush\n"
HANDLER = (":alarms\n"
           "rem The one option that SENDS. Alarms only (no quiet updates), up and\n"
           "rem down alike; the first ring of a new move repeats on the phone until\n"
           "rem it is acknowledged. Started late, it sends nothing from before it began.\n"
           'start "SPCX alarms" cmd /k python open_candles.py --from 04:00 --until 20:00 '
           "--rings-only --persist 30\n"
           "goto menu\n"
           "\n")


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(__file__).with_name("SPCX Morning.bat")
    if not path.is_file():
        print(f"STOP: {path} not found - run this from the folder the menu is in")
        return 1
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if ":alarms\n" in text:
        print("already added - nothing to do")
        return 0
    for anchor in (MENU_AFTER, CHOICE_AFTER, HANDLER_BEFORE):
        if text.count(anchor) != 1:
            print("STOP: the menu is not laid out as expected - nothing changed")
            return 1
    text = text.replace(MENU_AFTER, MENU_AFTER + MENU_LINES)
    text = text.replace(CHOICE_AFTER, CHOICE_AFTER + CHOICE_LINE)
    text = text.replace(HANDLER_BEFORE, HANDLER + HANDLER_BEFORE)
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    print(f"added option A - backup is {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
