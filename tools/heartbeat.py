"""
heartbeat.py -- the watchers say they are alive, and watch each other.

The SPCX alarms (open_candles.py) and the merger watch (merger_watch.py)
each write a small file, about once a minute, saying "alive at HH:MM, as
process N". Each also reads the OTHER one's file. If the other's has not
moved for WATCH_AFTER, the phone is told once -- "SPCX alarms are NOT
running" -- and told again when it comes back.

It exists because twice on 19 Oct the SPCX alarms were not running and
nothing said so. A watcher that has stopped cannot report its own death;
the other one can.

The same file stops a second copy starting: a watcher whose file is
fresh and whose process is still alive is already running, and a second
copy would ring every alarm twice.

What this cannot see: the whole PC asleep, off or offline. Nothing on
the machine can send a message then.

    python heartbeat.py --self-test
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

#: How long the other watcher's file may stand still before the phone hears.
WATCH_AFTER = timedelta(minutes=10)
#: A file this fresh, from a process still alive, means "already running".
#: Generous against a watcher's slowest pass, short against a restart.
RUNNING_WITHIN = timedelta(minutes=3)

SPCX_ALARMS = "spcx_alarms.alive"
MERGER_WATCH = "merger_watch.alive"
#: Each watcher's name, and the verb that agrees with it.
NAMES = {SPCX_ALARMS: ("SPCX alarms", "are"), MERGER_WATCH: ("Merger watch", "is")}


def _named(path: str) -> Tuple[str, str]:
    return NAMES.get(os.path.basename(path), (path, "is"))


def write(path: str, now: datetime) -> None:
    """Say this process is alive now. Never fatal: a heartbeat that cannot
    be written must not stop the alarms it reports on."""
    try:
        temp = f"{path}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(f"{now.isoformat(timespec='seconds')} {os.getpid()}\n")
        os.replace(temp, path)
    except OSError:
        pass


def read(path: str) -> Optional[Tuple[datetime, int]]:
    """(when, pid) from a heartbeat file, or None if there is none."""
    try:
        with open(path, encoding="utf-8") as handle:
            stamp, pid = handle.read().split()
        return datetime.fromisoformat(stamp), int(pid)
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """Whether process `pid` is running. Asks; never signals it.

    On Windows os.kill(pid, 0) TERMINATES the process rather than probing
    it, so Windows is asked through the API instead.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)   # query limited info
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        finally:
            kernel.CloseHandle(handle)
        return code.value == 259                          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def already_running(path: str, now: datetime, alive=pid_alive) -> Optional[str]:
    """A sentence saying another copy is running, or None if it is not."""
    seen = read(path)
    if seen is None:
        return None
    when, pid = seen
    if pid == os.getpid() or now - when > RUNNING_WITHIN or not alive(pid):
        return None
    name, verb = _named(path)
    return (f"{name} {verb} already running (process {pid}, "
            f"alive at {when:%H:%M:%S}). Nothing started: a second copy would "
            f"ring every alarm twice.")


class Watch:
    """Writes this watcher's file and keeps an eye on the other's.

    `beat(now)` returns the messages to send, as (loud, text): loud True
    when the other has stopped, False when it is back. Nothing is said
    about the other until this one has been up for WATCH_AFTER, so after
    a restart of the whole PC neither shouts about a file the other has
    simply not had time to refresh yet.
    """

    def __init__(self, mine: str, theirs: str, started: datetime):
        self.mine, self.theirs, self.started = mine, theirs, started
        self.down = False

    def beat(self, now: datetime) -> List[Tuple[bool, str]]:
        write(self.mine, now)
        if now - self.started < WATCH_AFTER:
            return []
        seen = read(self.theirs)
        if seen is None:
            return []                       # never set up on this PC
        when = seen[0]
        name, verb = _named(self.theirs)
        if now - when > WATCH_AFTER:
            if self.down:
                return []
            self.down = True
            return [(True, f"{name} {verb} NOT running. Last sign of life "
                           f"{when:%a %H:%M}. Check the PC: start it again, or "
                           f"see whether the PC slept or restarted.")]
        if self.down:
            self.down = False
            return [(False, f"{name} {verb} running again.")]
        return []


def self_test() -> int:
    import tempfile

    failures = []
    t0 = datetime(2026, 10, 20, 9, 0)
    with tempfile.TemporaryDirectory() as folder:
        mine = os.path.join(folder, MERGER_WATCH)
        theirs = os.path.join(folder, SPCX_ALARMS)

        write(mine, t0)
        if read(mine) != (t0, os.getpid()):
            failures.append(f"a heartbeat should read back as written: {read(mine)}")
        if read(os.path.join(folder, "missing")) is not None:
            failures.append("no file is no heartbeat")

        # Another copy: fresh and alive blocks; stale, dead or ourselves does not.
        other = os.path.join(folder, "other.alive")
        with open(other, "w", encoding="utf-8") as h:
            h.write(f"{t0.isoformat()} 424242\n")
        if not already_running(other, t0 + timedelta(minutes=1), alive=lambda p: True):
            failures.append("a fresh heartbeat from a live process is a running copy")
        if already_running(other, t0 + timedelta(minutes=4), alive=lambda p: True):
            failures.append("a heartbeat older than 3 minutes is not a running copy")
        if already_running(other, t0 + timedelta(minutes=1), alive=lambda p: False):
            failures.append("a crashed copy's fresh file must not block its restart")
        if already_running(mine, t0, alive=lambda p: True):
            failures.append("our own heartbeat is not another copy")
        if not pid_alive(os.getpid()):
            failures.append("this process should read as alive")

        # Watching: quiet while starting up, one alarm when the other
        # stops, one note when it is back.
        watch = Watch(mine, theirs, started=t0)
        write(theirs, t0 - timedelta(hours=5))           # stale from before a reboot
        if watch.beat(t0 + timedelta(minutes=5)):
            failures.append("nothing is said in the first 10 minutes after starting")
        said = watch.beat(t0 + timedelta(minutes=11))
        if len(said) != 1 or not said[0][0] or "SPCX alarms are NOT running" not in said[0][1]:
            failures.append(f"a stale heartbeat should ring once, loud: {said}")
        if watch.beat(t0 + timedelta(minutes=12)):
            failures.append("a stopped watcher is reported once")
        write(theirs, t0 + timedelta(minutes=13))
        said = watch.beat(t0 + timedelta(minutes=13))
        if len(said) != 1 or said[0][0] or "running again" not in said[0][1]:
            failures.append(f"its return should be one quiet note: {said}")
        if read(mine)[0] != t0 + timedelta(minutes=13):
            failures.append("every beat should refresh our own file")
        os.remove(theirs)
        if Watch(mine, theirs, started=t0).beat(t0 + timedelta(minutes=30)):
            failures.append("a watcher never set up on this PC is not reported")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("heartbeat self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else 0)
