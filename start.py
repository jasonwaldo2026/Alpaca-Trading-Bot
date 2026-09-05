"""
Start everything with one command.

    python start.py            # scanner in watch mode + Studio
    python start.py --studio   # Studio only
    python start.py --scanner  # scanner only
    python start.py --dry-run  # show what would run, run nothing
    python start.py --no-update  # skip pulling the latest version first

On every start it first pulls the latest version of the project from
GitHub (fast-forward only, so it can never clobber local edits) and
reinstalls requirements if they changed. That is the whole update routine:
merge on GitHub, then double-click start.

Double-click `start.command` on a Mac or `start.bat` on Windows to run
this without opening a terminal yourself.

The scanner prints matches here and sends Pushover alerts; Studio runs
quietly and logs to studio.log. Both stop together on Ctrl-C. While this
runs the computer is kept from idling to sleep — `caffeinate` on a Mac,
SetThreadExecutionState on Windows — because a sleeping computer scans
nothing and alerts nobody.
"""

import glob
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent
STUDIO_PORT = 8501
STUDIO_LOG = ROOT / "studio.log"

# Tailscale's CLI is on PATH on Linux, and usually on Windows; the Mac App
# Store build keeps it inside the app bundle, and a fresh Windows install
# may not have refreshed PATH yet.
TAILSCALE_PATHS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "tailscale.exe"),
)

#: GitHub Desktop bundles its own git, which is not on PATH. These are the
#: places it keeps it, so "I only have GitHub Desktop" is enough to update.
GIT_BUNDLED = (
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "GitHubDesktop", "app-*",
                 "resources", "app", "git", "cmd", "git.exe"),
    "/Applications/GitHub Desktop.app/Contents/Resources/app/git/bin/git",
)


def git_executable() -> Optional[str]:
    exe = shutil.which("git")
    if exe:
        return exe
    for pattern in GIT_BUNDLED:
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]          # newest GitHub Desktop version
    return None


def _run(cmd: List[str], timeout: float = 90) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def self_update(python: str = sys.executable) -> str:
    """
    Pull the latest project from GitHub before starting.

    Fast-forward only: if this copy has local edits that diverge, git
    refuses and we start what is here rather than merge anything. If
    requirements.txt changed, reinstall so a new dependency does not turn
    into a crash on launch.
    """
    git = git_executable()
    if not git:
        return "Update skipped: git not found (install GitHub Desktop or git)."
    try:
        before = _run([git, "rev-parse", "HEAD"]).stdout.strip()
        pulled = _run([git, "pull", "--ff-only"])
        if pulled.returncode != 0:
            reason = (pulled.stderr.strip() or pulled.stdout.strip()).splitlines()[-1:]
            return "Update skipped: " + (reason[0] if reason else "git pull failed") + \
                   " (starting the version already here)"
        after = _run([git, "rev-parse", "HEAD"]).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Update skipped: {exc} (starting the version already here)"

    if before == after:
        return "Already up to date."

    changed = _run([git, "diff", "--name-only", before, after]).stdout.split()
    note = f"Updated to the latest version ({len(changed)} file(s) changed)."
    if "requirements.txt" in changed:
        install = _run([python, "-m", "pip", "install", "-r", "requirements.txt"], timeout=600)
        note += (" Requirements reinstalled." if install.returncode == 0
                 else " Requirements changed but reinstall failed — run: pip install -r requirements.txt")
    return note


# Windows: SetThreadExecutionState flags.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake_windows(enable: bool) -> None:
    """Stop (or allow) idle sleep while the scanner runs. Display may still
    turn off; that is fine."""
    if os.name != "nt":
        return
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:                       # noqa: BLE001 - best effort
        pass


def scanner_command(env: Dict[str, str], python: str = sys.executable) -> List[str]:
    """
    The scanner in watch mode, across the full extended session.

    Float via FMP is switched on when a key is configured; without one the
    low-float condition in the strong-above-VWAP scenario can never match,
    and the scanner says so at startup.
    """
    cmd = [python, "-m", "scanner.cli", "--watch", "--extended-hours"]
    if env.get("FMP_API_KEY"):
        cmd.append("--fmp-floats")
    return cmd


def studio_command(python: str = sys.executable, port: int = STUDIO_PORT) -> List[str]:
    """Studio, headless (no browser pop-up), on a fixed port so the phone
    bookmark never changes."""
    return [
        python, "-m", "streamlit", "run", str(ROOT / "studio" / "app.py"),
        "--server.headless", "true",
        "--server.port", str(port),
        "--browser.gatherUsageStats", "false",
    ]


def is_tailscale_address(ip: str) -> bool:
    """Tailscale hands out addresses in 100.64.0.0/10 — 100.64.x.x through
    100.127.x.x. Ordinary home networks never use that range."""
    parts = ip.split(".")
    return (len(parts) == 4 and parts[0] == "100" and parts[1].isdigit()
            and 64 <= int(parts[1]) <= 127)


def tailscale_ip_from_interfaces() -> Optional[str]:
    """The Tailscale address as the OS sees it, no CLI needed. Works when
    the app is installed but its command-line tool is not on PATH."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except socket.gaierror:
        return None
    for info in infos:
        ip = info[4][0]
        if is_tailscale_address(ip):
            return ip
    return None


def tailscale_ip() -> Optional[str]:
    """This machine's Tailscale address, or None when Tailscale is absent."""
    found = tailscale_ip_from_interfaces()
    if found:
        return found
    for exe in (shutil.which("tailscale"), *TAILSCALE_PATHS):
        if not exe or not Path(exe).exists():
            continue
        try:
            out = subprocess.run(
                [exe, "ip", "-4"], capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        ip = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
        if is_tailscale_address(ip):
            return ip
    return None


def studio_urls(port: int = STUDIO_PORT) -> List[str]:
    """Where Studio can be opened from, most useful first."""
    urls = []
    ip = tailscale_ip()
    host = socket.gethostname().split(".")[0].lower()
    if ip:
        urls.append(f"http://{host}:{port}   (phone, anywhere, via Tailscale)")
        urls.append(f"http://{ip}:{port}   (same, by address)")
    urls.append(f"http://localhost:{port}   (this computer)")
    if not ip:
        urls.append(
            "Tailscale not detected on this computer. If it is installed and "
            "connected, use the address shown for this computer in the "
            f"phone's Tailscale app, with :{port} on the end."
        )
    return urls


def port_in_use(port: int) -> bool:
    """True when something is already listening on the port — usually a
    Studio from another start window."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.settimeout(0.5)
        return probe_socket.connect_ex(("127.0.0.1", port)) == 0


def missing_keys(env: Dict[str, str]) -> List[str]:
    return [k for k in ("ALPACA_API_KEY", "ALPACA_API_SECRET") if not env.get(k)]


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    want_scanner = "--studio" not in argv
    want_studio = "--scanner" not in argv
    dry_run = "--dry-run" in argv

    os.chdir(ROOT)
    if not dry_run and "--no-update" not in argv:
        print(f"Checking for updates… {self_update()}")
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    env = dict(os.environ)

    if missing_keys(env):
        print(
            f"Missing {' and '.join(missing_keys(env))}. Copy .env.example to "
            f".env in {ROOT} and fill in your Alpaca paper keys.",
            file=sys.stderr,
        )
        if not dry_run:
            return 1

    if want_scanner and not (env.get("PUSHOVER_TOKEN") and env.get("PUSHOVER_USER")):
        print("Pushover keys not set: matches will print here but not reach your phone.")

    if want_studio and not dry_run and port_in_use(STUDIO_PORT):
        print(
            f"Studio is already running on port {STUDIO_PORT} — probably in "
            f"another start window. Leaving that one be."
        )
        want_studio = False
        if not want_scanner:
            return 0

    plans = []
    if want_scanner:
        plans.append(("scanner", scanner_command(env), None))
    if want_studio:
        plans.append(("studio", studio_command(), STUDIO_LOG))

    if dry_run:
        for name, cmd, log in plans:
            print(f"{name}: {' '.join(cmd)}" + (f"  > {log.name}" if log else ""))
        for url in studio_urls():
            print(f"studio url: {url}")
        return 0

    procs = []
    log_handles = []
    caffeinate = None
    try:
        if shutil.which("caffeinate"):
            caffeinate = subprocess.Popen(["caffeinate", "-i"])
        keep_awake_windows(True)
        for name, cmd, log in plans:
            if log:
                handle = open(log, "a")
                log_handles.append(handle)
                procs.append((name, subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT)))
            else:
                procs.append((name, subprocess.Popen(cmd)))

        if want_studio:
            time.sleep(2)
            print("\nStudio is starting. Open it at:")
            for url in studio_urls():
                print(f"   {url}")
            print(f"   (Studio's own output is in {STUDIO_LOG.name})\n")
        print("Ctrl-C stops everything.\n")

        while True:
            for name, proc in procs:
                code = proc.poll()
                if code is not None:
                    print(f"\n{name} exited with code {code}; stopping the rest.")
                    return code or 1
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping…")
        return 0
    finally:
        for _, proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT if os.name != "nt" else signal.SIGTERM)
        for _, proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if caffeinate and caffeinate.poll() is None:
            caffeinate.terminate()
        keep_awake_windows(False)
        for handle in log_handles:
            handle.close()


if __name__ == "__main__":
    sys.exit(main())
