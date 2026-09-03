"""
Start everything with one command.

    python start.py            # scanner in watch mode + Studio
    python start.py --studio   # Studio only
    python start.py --scanner  # scanner only
    python start.py --dry-run  # show what would run, run nothing

Double-click `start.command` on a Mac or `start.bat` on Windows to run
this without opening a terminal yourself.

The scanner prints matches here and sends Pushover alerts; Studio runs
quietly and logs to studio.log. Both stop together on Ctrl-C. On a Mac,
`caffeinate` keeps the computer from idling to sleep while this runs —
a sleeping computer scans nothing and alerts nobody.
"""

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

# Tailscale's CLI lives here on a Mac when installed from the App Store;
# on Linux and Windows it is on PATH.
TAILSCALE_MAC = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


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


def tailscale_ip() -> Optional[str]:
    """This machine's Tailscale address, or None when Tailscale is absent."""
    for exe in (shutil.which("tailscale"), TAILSCALE_MAC):
        if not exe or not Path(exe).exists():
            continue
        try:
            out = subprocess.run(
                [exe, "ip", "-4"], capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        ip = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
        if ip.startswith("100."):
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
    return urls


def missing_keys(env: Dict[str, str]) -> List[str]:
    return [k for k in ("ALPACA_API_KEY", "ALPACA_API_SECRET") if not env.get(k)]


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    want_scanner = "--studio" not in argv
    want_studio = "--scanner" not in argv
    dry_run = "--dry-run" in argv

    os.chdir(ROOT)
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
        for handle in log_handles:
            handle.close()


if __name__ == "__main__":
    sys.exit(main())
