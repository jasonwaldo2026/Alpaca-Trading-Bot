"""
patch_live_feed.py -- make Scanner Studio's Live Feed read live (2026-10-16).

Live Feed (Incoming Alerts) read Studio's snapshot copy, studio_cache.db,
which only changes when Refresh is pressed AT THE COMPUTER -- a phone's
Refresh is refused, by design, because every write is loopback-only. The
snapshot was last loaded 2026-10-03, so the feed stayed on 10-02 while
Market Scanner kept writing fast_volume, news and sec_filing signals.

This replaces ONE function in server.py, api_feed, so the feed reads
Market Scanner's scanner.db directly, read-only -- the way the Hot List,
Pre-Market Watchlist and L2 already do -- with times in New York time.
If scanner.db cannot be read, it falls back to the old snapshot query.

It refuses to touch the file unless api_feed is exactly the version it
was written against, keeps a timestamped backup, keeps the file's own
line endings, and puts the backup back if the result does not compile.

    cd C:\\Dev\\Scanner-Studio
    python patch_live_feed.py              # patches server.py beside it
    python patch_live_feed.py --check      # says what it would do, writes nothing

Display only. Nothing here can place an order or write to scanner.db.
"""

from __future__ import annotations

import hashlib
import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

START = "def api_feed(q: dict) -> dict:\n"
END = "\n\ndef api_premarket("
ORIGINAL_SHA256 = "d9cc4e043fb9de498bcf2a68b47a5431d93781dc9241f1f580dba71c7621f343"
MARKER = "LIVE FEED (2026-10-16)"

NEW_BLOCK = '''def _feed_snapshot(limit: int) -> list:
    """The old feed: Studio's snapshot copy. Kept only as the fallback for
    when scanner.db cannot be read."""
    conn = store.connect()
    try:
        items = []
        for r in conn.execute(
                "SELECT ts, symbol, ratio, price_move_pct, zscore FROM fast_volume "
                "ORDER BY ts DESC LIMIT ?", (limit,)):
            items.append({"kind": "fast_volume", "ts": r["ts"], "symbol": r["symbol"],
                          "headline": f"{(r['ratio'] or 0):.1f}x volume",
                          "detail": f"price {r['price_move_pct']:+.2f}% - z {r['zscore'] or 0:.1f}",
                          "value": r["price_move_pct"]})
        for r in conn.execute(
                "SELECT ts, symbol, title, publisher, link FROM news "
                "ORDER BY ts DESC LIMIT ?", (limit,)):
            items.append({"kind": "news", "ts": r["ts"], "symbol": r["symbol"],
                          "headline": r["title"], "detail": r["publisher"],
                          "link": r["link"]})
        for r in conn.execute(
                "SELECT ts, symbol, form_type, link FROM filings "
                "ORDER BY ts DESC LIMIT ?", (limit,)):
            items.append({"kind": "filing", "ts": r["ts"], "symbol": r["symbol"],
                          "headline": f"SEC {r['form_type']}",
                          "detail": "EDGAR filing", "link": r["link"]})
        items.sort(key=lambda d: d["ts"] or "", reverse=True)
        return items[:limit]
    finally:
        conn.close()


def _feed_live(conn: sqlite3.Connection, limit: int) -> list:
    """Newest fast-volume spikes (rising only, as the snapshot loader kept
    them), news and SEC filings, straight from Market Scanner's signals.

    Walked newest-first by id, which is the order they were written, so
    the walk stops as soon as it has enough rather than sorting the whole
    table. Times come back as New York time, 'YYYY-MM-DD HH:MM:SS'."""
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")

    def when(created_at):
        try:
            dt = datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            return created_at
        if dt.tzinfo is None:                      # scanner.db writes UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ny).strftime("%Y-%m-%d %H:%M:%S")

    def newest(signal_type, keep, want, look=None):
        out = []
        cur = conn.execute(
            "SELECT s.created_at, s.score, s.detail, a.symbol FROM signals s "
            "JOIN assets a ON a.id = s.asset_id WHERE s.signal_type = ? "
            "ORDER BY s.id DESC", (signal_type,))
        for n, r in enumerate(cur):
            if look is not None and n >= look:
                break
            try:
                d = json.loads(r["detail"]) if r["detail"] else {}
            except (ValueError, TypeError):
                d = {}
            if keep(d):
                out.append((r, d))
                if len(out) >= want:
                    break
        cur.close()
        return out

    items = []
    for r, d in newest("fast_volume", lambda d: (d.get("price_move_pct") or 0) > 0,
                       limit, look=limit * 20):
        pm = d.get("price_move_pct") or 0
        items.append({"kind": "fast_volume", "ts": when(r["created_at"]), "symbol": r["symbol"],
                      "headline": f"{(r['score'] or 0):.1f}x volume",
                      "detail": f"price {pm:+.2f}% - z {d.get('zscore') or 0:.1f}",
                      "value": pm})
    for r, d in newest("news", lambda d: True, limit):
        items.append({"kind": "news", "ts": when(r["created_at"]), "symbol": r["symbol"],
                      "headline": d.get("title"), "detail": d.get("publisher"),
                      "link": d.get("url")})
    for r, d in newest("sec_filing", lambda d: True, limit):
        items.append({"kind": "filing", "ts": when(r["created_at"]), "symbol": r["symbol"],
                      "headline": f"SEC {d.get('form_type')}",
                      "detail": "EDGAR filing", "link": d.get("link")})
    items.sort(key=lambda d: d["ts"] or "", reverse=True)
    return items[:limit]


def api_feed(q: dict) -> dict:
    """LIVE FEED (2026-10-16): read LIVE from Market Scanner's scanner.db
    (mode=ro, never written), like the Hot List and Pre-Market Watchlist,
    instead of from the snapshot - which only changes when Refresh is
    pressed at the computer, and had sat on 2026-10-02 for two weeks.
    Falls back to the snapshot if scanner.db cannot be read."""
    limit = min(int(q.get("limit", ["120"])[0]), 500)
    try:
        conn = _scanner_ro()
    except (FileNotFoundError, sqlite3.Error):
        return {"items": _feed_snapshot(limit), "source": "snapshot"}
    try:
        return {"items": _feed_live(conn, limit), "source": "live"}
    except sqlite3.Error:
        traceback.print_exc()
        return {"items": _feed_snapshot(limit), "source": "snapshot"}
    finally:
        conn.close()
'''


def main() -> int:
    check = "--check" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = Path(args[0]) if args else Path(__file__).with_name("server.py")
    if not path.is_file():
        print(f"STOP: {path} not found. Run this from C:\\Dev\\Scanner-Studio.")
        return 1

    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")

    if MARKER in text:
        print("already patched - nothing to do")
        return 0
    try:
        start = text.index(START)
        end = text.index(END, start)
    except ValueError:
        print("STOP: could not find api_feed in server.py - nothing changed")
        return 1
    if hashlib.sha256(text[start:end].encode("utf-8")).hexdigest() != ORIGINAL_SHA256:
        print("STOP: api_feed is not the version this was written for - nothing changed")
        return 1
    if check:
        print("ready: api_feed matches; run without --check to patch")
        return 0

    new = text[:start] + NEW_BLOCK.rstrip("\n") + text[end:]
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    out = new.replace("\n", "\r\n") if crlf else new
    path.write_bytes(out.encode("utf-8"))
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        shutil.copy2(backup, path)
        print(f"STOP: the patched file did not compile, original restored ({exc.msg})")
        return 1
    print(f"patched - backup is {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
