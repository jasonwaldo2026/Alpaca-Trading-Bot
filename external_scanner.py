"""
External Scanner Adapters
=========================
Bridges a scanner that lives OUTSIDE this repo into the bot's BaseScanner
interface, so an existing scanner app can drive symbol selection without
being rewritten.

Pick the adapter matching how your scanner exposes its results:

    FileScanner      → it writes a JSON or CSV file       (most decoupled)
    CallableScanner  → it's importable Python             (same machine)
    HttpScanner      → it serves results over HTTP        (any machine)

Wiring it up:

    from external_scanner import FileScanner
    bot = TradingBot(config, scanner=FileScanner("~/scanner/out/latest.json"))

Field mapping
-------------
Adapters accept records as dicts and map them onto ScanResult. Only a symbol
is strictly required. Common field names are recognised automatically
("symbol"/"ticker", "score"/"rank"/"rating", …); anything unusual can be named
explicitly via the `fields` argument.

Safety
------
An external scanner is an input the bot trades on, so these adapters treat it
as untrusted rather than assuming it is healthy:

  * Staleness — a scanner that silently stopped updating would otherwise have
    the bot trading yesterday's shortlist forever. Results older than
    `max_age_minutes` are refused outright.
  * Size cap — `max_symbols` bounds how much a malformed or runaway file can
    push into the trading path.
  * Per-record isolation — one malformed row is skipped, not fatal.

None of this lets the external scanner place a trade. It only proposes what
to look at; entries still require the strategy to fire and the risk manager
to approve.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from scanner import BaseScanner, ScanResult

log = logging.getLogger("alpaca_bot.external_scanner")


# ── Field mapping ─────────────────────────────────────────────────────────────

# Recognised aliases, checked in order. Override per-instance via `fields`.
DEFAULT_FIELDS: Dict[str, Sequence[str]] = {
    "symbol": ("symbol", "ticker", "Symbol", "Ticker", "SYMBOL", "sym"),
    "score":  ("score", "rank", "rating", "Score", "Rank", "confidence", "strength"),
    "price":  ("price", "last", "close", "Price", "Close"),
    "reason": ("reason", "reasons", "why", "notes", "Reason", "signal"),
    "asset_class": ("asset_class", "class", "type", "AssetClass"),
}


def _pick(record: Dict, names: Sequence[str]):
    for n in names:
        if n in record and record[n] is not None:
            return record[n]
    return None


def _infer_asset_class(symbol: str) -> str:
    """Alpaca crypto pairs carry a slash ("BTC/USD"); equities do not."""
    return "crypto" if "/" in symbol else "stock"


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def records_to_results(
    records: Iterable[Dict],
    fields: Optional[Dict[str, Sequence[str]]] = None,
    max_symbols: int = 200,
) -> List[ScanResult]:
    """Normalise arbitrary dict records into ScanResult objects."""
    mapping = {**DEFAULT_FIELDS, **(fields or {})}
    results: List[ScanResult] = []
    skipped = 0

    for record in records:
        if len(results) >= max_symbols:
            log.warning("External scanner: stopping at max_symbols=%d", max_symbols)
            break
        try:
            if not isinstance(record, dict):
                skipped += 1
                continue

            raw_symbol = _pick(record, mapping["symbol"])
            if not raw_symbol:
                skipped += 1
                continue
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                skipped += 1
                continue

            asset_class = _pick(record, mapping["asset_class"])
            asset_class = (
                str(asset_class).lower()
                if asset_class in ("stock", "crypto", "Stock", "Crypto")
                else _infer_asset_class(symbol)
            )

            # Absent score means "no ranking supplied" — keep ordering as given
            # rather than inventing a number that looks like a real rank.
            raw_score = _pick(record, mapping["score"])
            score = _coerce_float(raw_score, default=0.0)

            reason = _pick(record, mapping["reason"])
            if isinstance(reason, (list, tuple)):
                reasons = [str(r) for r in reason]
            elif reason:
                reasons = [str(reason)]
            else:
                reasons = ["from external scanner"]

            results.append(
                ScanResult(
                    symbol=symbol,
                    asset_class=asset_class,
                    score=score,
                    price=_coerce_float(_pick(record, mapping["price"])),
                    reasons=reasons,
                )
            )
        except Exception as exc:
            skipped += 1
            log.warning("External scanner: skipping malformed record: %s", exc)

    if skipped:
        log.warning("External scanner: skipped %d unusable record(s)", skipped)

    # Only re-sort when scores were actually supplied; otherwise honour the
    # order the external scanner produced.
    if any(r.score for r in results):
        results.sort(key=lambda r: r.score, reverse=True)
    return results


# ── Base adapter ──────────────────────────────────────────────────────────────

class ExternalScanner(BaseScanner):
    """
    Shared behaviour for adapters. Subclasses implement fetch_records().

    Bars are accepted to satisfy the BaseScanner interface but are ignored —
    an external scanner does its own data collection, so the bot skips
    fetching them entirely.
    """

    needs_bars = False

    def __init__(
        self,
        fields: Optional[Dict[str, Sequence[str]]] = None,
        max_symbols: int = 200,
        fail_open: bool = False,
    ):
        self.fields = fields
        self.max_symbols = max_symbols
        # fail_open=False means an unreachable scanner yields an empty
        # shortlist and the bot simply trades nothing new that cycle. That is
        # the safe direction: no data should never imply "buy anything".
        self.fail_open = fail_open
        self._last_good: List[ScanResult] = []

    def fetch_records(self) -> List[Dict]:
        raise NotImplementedError

    def scan(self, stock_bars: pd.DataFrame, crypto_bars: pd.DataFrame,
             config) -> List[ScanResult]:
        try:
            records = self.fetch_records()
        except Exception as exc:
            log.error("External scanner unavailable: %s", exc)
            if self.fail_open and self._last_good:
                log.warning(
                    "Reusing last good shortlist (%d symbols) — fail_open=True",
                    len(self._last_good),
                )
                return self._last_good
            return []

        results = records_to_results(records, self.fields, self.max_symbols)
        if not results:
            log.warning("External scanner returned no usable candidates")
            return []

        log.info(
            "External scanner: %d candidates, top pick %s",
            len(results), results[0].symbol,
        )
        self._last_good = results
        return results


# ── File adapter ──────────────────────────────────────────────────────────────

class FileScanner(ExternalScanner):
    """
    Reads results from a JSON or CSV file the scanner app writes.

    The most decoupled option, and the only one that works when the scanner
    and the bot are not on the same machine — point both at a synced folder
    (Dropbox, iCloud, rsync) or have the scanner commit its output.

    JSON may be either a bare list of records, or an object with the records
    under "results", "symbols", "candidates" or "data".
    """

    LIST_KEYS = ("results", "symbols", "candidates", "data", "rows")

    def __init__(self, path: str, max_age_minutes: Optional[float] = 120, **kwargs):
        super().__init__(**kwargs)
        self.path = Path(os.path.expanduser(path))
        self.max_age_minutes = max_age_minutes

    def _check_age(self):
        if self.max_age_minutes is None:
            return
        age_min = (time.time() - self.path.stat().st_mtime) / 60
        if age_min > self.max_age_minutes:
            raise RuntimeError(
                f"{self.path.name} is {age_min:.0f} min old "
                f"(limit {self.max_age_minutes:.0f}) — refusing to trade stale "
                f"scan results. Is the scanner app still running?"
            )

    def fetch_records(self) -> List[Dict]:
        if not self.path.exists():
            raise FileNotFoundError(f"Scanner output not found: {self.path}")
        self._check_age()

        text = self.path.read_text()
        if self.path.suffix.lower() == ".csv":
            return list(csv.DictReader(text.splitlines()))

        payload = json.loads(text)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in self.LIST_KEYS:
                if isinstance(payload.get(key), list):
                    return payload[key]
            raise ValueError(
                f"JSON object has no record list under any of {self.LIST_KEYS}"
            )
        raise ValueError(f"Unsupported JSON payload type: {type(payload).__name__}")


# ── Callable adapter ──────────────────────────────────────────────────────────

class CallableScanner(ExternalScanner):
    """
    Wraps an importable Python callable returning records.

    Use when the scanner app is a Python package on the same machine:

        from my_scanner import run_scan
        scanner = CallableScanner(run_scan)

    The callable may return a list of dicts, a list of symbol strings, or a
    pandas DataFrame.
    """

    def __init__(self, fn: Callable[[], object], **kwargs):
        super().__init__(**kwargs)
        if not callable(fn):
            raise TypeError("CallableScanner requires a callable")
        self.fn = fn

    def fetch_records(self) -> List[Dict]:
        output = self.fn()

        if isinstance(output, pd.DataFrame):
            return output.to_dict("records")
        if isinstance(output, dict):
            for key in FileScanner.LIST_KEYS:
                if isinstance(output.get(key), list):
                    output = output[key]
                    break
            else:
                raise ValueError("Callable returned a dict with no record list")
        if not isinstance(output, list):
            raise TypeError(
                f"Callable returned {type(output).__name__}, expected list/DataFrame"
            )

        # Accept a plain list of ticker strings.
        return [{"symbol": o} if isinstance(o, str) else o for o in output]


# ── HTTP adapter ──────────────────────────────────────────────────────────────

class HttpScanner(ExternalScanner):
    """
    Fetches results from an HTTP endpoint returning JSON.

    Use when the scanner runs as a service — including on another machine, so
    long as the bot can reach it. Expose the scanner over your LAN or a tunnel
    and point the bot at it.
    """

    def __init__(self, url: str, timeout: float = 10.0,
                 headers: Optional[Dict[str, str]] = None, **kwargs):
        super().__init__(**kwargs)
        self.url = url
        self.timeout = timeout
        self.headers = headers or {}

    def fetch_records(self) -> List[Dict]:
        import urllib.request

        req = urllib.request.Request(self.url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Scanner endpoint returned HTTP {resp.status}")
            payload = json.loads(resp.read().decode())

        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in FileScanner.LIST_KEYS:
                if isinstance(payload.get(key), list):
                    return payload[key]
        raise ValueError("Scanner endpoint returned no recognisable record list")
