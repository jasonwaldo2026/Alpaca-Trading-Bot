"""
add_overnight_to_alarms.py -- make menu option A run day AND night (2026-10-18).

Edits the "SPCX Morning.bat" beside it in place. Option A gains
--overnight, so after the day (04:00-20:00, Alpaca) it carries on into
IBKR's overnight session (20:00-03:50, Sunday to Thursday nights, read
from IB Gateway) and keeps going, day and night, until its window is
closed. Nothing else in the menu changes.

Keeps a timestamped backup and the file's own line endings, and says
"already added" on a second run.

    cd C:\\Dev\\Feed-Check
    python add_overnight_to_alarms.py
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

OLD_MENU = "echo     A   SPCX alarms            04:00 to 20:00, up and down, TO YOUR PHONE\n"
NEW_MENU = "echo     A   SPCX alarms            day and night, up and down, TO YOUR PHONE\n"
OLD_CMD = ('start "SPCX alarms" cmd /k python open_candles.py --from 04:00 '
           "--until 20:00 --rings-only --persist 30\n")
NEW_CMD = ("rem --overnight: 20:00-03:50, Sunday to Thursday nights, from IB Gateway.\n"
           "rem Keep IB Gateway logged in; if it is not, the phone is told.\n"
           + OLD_CMD.replace("--persist 30\n", "--persist 30 --overnight\n"))


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(__file__).with_name("SPCX Morning.bat")
    if not path.is_file():
        print(f"STOP: {path} not found - run this from the folder the menu is in")
        return 1
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if "--overnight" in text:
        print("already added - nothing to do")
        return 0
    if text.count(OLD_CMD) != 1 or text.count(OLD_MENU) != 1:
        print("STOP: option A is not laid out as expected - nothing changed.\n"
              "Run python add_alarms_option.py first if the menu has no option A.")
        return 1
    text = text.replace(OLD_CMD, NEW_CMD).replace(OLD_MENU, NEW_MENU)
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    print(f"option A now runs day and night - backup is {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
