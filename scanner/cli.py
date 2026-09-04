"""
Scanner CLI.

    python -m scanner.cli                      # run every rule in rules/
    python -m scanner.cli rules/oversold.json  # run specific rules
    python -m scanner.cli --symbols AAPL,MSFT  # override the universe
"""

import argparse
import glob
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from core.client import AlpacaClient, Credentials
from core.data import MarketDataFetcher
from core.rules import RuleError, load_rules
from core import universe
from core.fundamentals import NullFloatProvider, load_provider
from core.sessions import (
    DEFAULT_CALENDAR,
    ET,
    SESSION_BOUNDS,
    SessionConfig,
    session_at,
)
from scanner.alerts import AlertNotifier
from scanner.engine import Scanner

DEFAULT_RULE_GLOB = "rules/*.json"


def scan_window(config: SessionConfig) -> str:
    """The wall-clock span this config scans, e.g. '04:00-20:00 ET'."""
    enabled = [name for name in ("pre", "regular", "after") if name in config.enabled()]
    if not enabled:
        return "no sessions enabled"
    start = SESSION_BOUNDS[enabled[0]][0]
    end = SESSION_BOUNDS[enabled[-1]][1]
    return f"{start:%H:%M}-{end:%H:%M} ET"


def closed_market_note(result, config: SessionConfig) -> str:
    """
    One line explaining a scan that touched nothing because the market is
    shut. 'Scanned 0 symbols' at 10 pm reads like a fault; it is not.
    """
    if result.scanned or not result.skipped:
        return ""
    if not all(why.startswith("market closed") for why in result.skipped.values()):
        return ""
    return (
        f"Market closed — nothing to scan. Scanning resumes when the "
        f"{scan_window(config)} session opens (Mon-Fri). Leave this running."
    )


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Scan the market with saved rules.")
    parser.add_argument("rules", nargs="*", help=f"Rule JSON files (default: {DEFAULT_RULE_GLOB})")
    parser.add_argument("--symbols", help="Comma-separated symbols, overriding rule universes")
    parser.add_argument(
        "--universe", default=None,
        help=(
            "Named universe overriding each rule's own: all_tradable, "
            "sp500_liquid, major_crypto, default_stocks, default_crypto. "
            "'all_tradable' is every US equity Alpaca lists (~11k) — see "
            "docs/specs/scanner/universe-size.md for what that costs."
        ),
    )
    parser.add_argument("--bars", type=int, default=300, help="Bars to fetch per symbol")
    parser.add_argument(
        "--bar-minutes", type=int, default=None,
        help=(
            "Bar size in minutes (5, 15, 30, 60). Default: the size the "
            "rules were saved at. Periods are bar counts, so a rule is only "
            "ever evaluated at the size it was written for; a mismatch is "
            "an error, not a warning."
        ),
    )
    parser.add_argument(
        "--feed", choices=["iex", "sip"], default=None,
        help=(
            "Equity data feed. Default is your account's, which on free and "
            "basic plans is IEX — one venue, thin in pre-market, so many "
            "5-minute windows produce no bar at all. 'sip' is the "
            "consolidated tape and needs a paid Alpaca data plan."
        ),
    )
    parser.add_argument(
        "--extended-hours", action="store_true",
        help="Include pre-market (04:00) and after-hours (to 20:00) sessions.",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Keep scanning on an interval instead of running once.",
    )
    parser.add_argument(
        "--every", type=int, default=None, metavar="MIN",
        help="Minutes between scans in --watch mode (default: the bar size).",
    )
    parser.add_argument(
        "--no-alerts", action="store_true",
        help="Scan and print, but send no phone notifications.",
    )
    parser.add_argument(
        "--floats", default="floats.json", metavar="FILE",
        help=(
            "JSON file of float per symbol, used by low-float conditions. "
            "Copy floats.example.json. Without a float source those "
            "conditions cannot be met and the scenario will not fire."
        ),
    )
    parser.add_argument(
        "--fmp-floats", action="store_true",
        help=(
            "Use Financial Modeling Prep for float (needs FMP_API_KEY). "
            "Automatic when that variable is set. One bulk call covers the "
            "whole market."
        ),
    )
    parser.add_argument(
        "--yahoo-floats", action="store_true",
        help=(
            "Look float up via yfinance when the file has no entry. "
            "Unofficial and rate-limited; verify it works before relying on it."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    paths = args.rules or sorted(glob.glob(DEFAULT_RULE_GLOB))
    if not paths:
        print(f"No rules found. Create one in Studio, or add a file matching {DEFAULT_RULE_GLOB}.")
        return 1

    try:
        rules = load_rules(paths)
    except (RuleError, OSError) as exc:
        print(f"Could not load rules: {exc}", file=sys.stderr)
        return 1

    sizes = sorted({r.params.bar_minutes for r in rules})
    if args.bar_minutes is None:
        if len(sizes) > 1:
            listing = ", ".join(f"{r.name}: {r.params.bar_minutes} min" for r in rules)
            print(
                f"These rules were saved at different bar sizes ({listing}). "
                f"Run them separately — periods are bar counts, so one "
                f"fetch cannot serve both.",
                file=sys.stderr,
            )
            return 1
        args.bar_minutes = sizes[0]
    elif sizes != [args.bar_minutes]:
        wrong = ", ".join(
            f"{r.name} ({r.params.bar_minutes} min)"
            for r in rules if r.params.bar_minutes != args.bar_minutes
        )
        print(
            f"--bar-minutes {args.bar_minutes} does not match {wrong}. A rule "
            f"is evaluated only at the bar size it was written for; re-save "
            f"it in Studio at {args.bar_minutes} minutes to change that.",
            file=sys.stderr,
        )
        return 1

    creds = Credentials.from_env()
    if not creds.is_complete():
        print("Missing ALPACA_API_KEY / ALPACA_API_SECRET.", file=sys.stderr)
        return 1

    client = AlpacaClient(creds)

    feed = None
    if args.feed:
        from alpaca.data.enums import DataFeed
        feed = DataFeed(args.feed)

    sessions = SessionConfig.extended() if args.extended_hours else SessionConfig()
    floats = load_provider(
        Path(args.floats),
        use_yahoo=args.yahoo_floats,
        use_fmp=args.fmp_floats or None,
    )
    print(f"Float source: {type(floats).__name__}")
    needs_float = any(r.needs_float() for r in rules)
    if needs_float and isinstance(floats, NullFloatProvider):
        print(
            "\nNo float source configured, and a rule needs one. Those "
            "conditions cannot be met, so that scenario will never fire. "
            f"Create {args.floats} (see floats.example.json) or pass "
            "--yahoo-floats.\n"
        )

    scanner = Scanner(
        MarketDataFetcher(client, args.bar_minutes, feed=feed),
        bar_limit=args.bars,
        bar_minutes=args.bar_minutes,
        sessions=sessions,
        fundamentals=floats,
    )
    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.universe:
        symbols = (
            universe.all_tradable(client) if args.universe == "all_tradable"
            else universe.resolve(args.universe)
        )

    # Rules may name all_tradable themselves; resolve that here, where a
    # client exists, rather than in core.
    if symbols is None and any(r.universe == "all_tradable" for r in rules):
        symbols = universe.all_tradable(client)

    if symbols:
        warning = universe.warn_if_large(symbols, args.bar_minutes)
        if warning:
            print(f"\n{warning}\n")
    rules_by_name = {r.name: r for r in rules}
    notifier = AlertNotifier(transport=None) if args.no_alerts else AlertNotifier.from_env()
    if notifier.enabled:
        alerting = sum(1 for r in rules if r.alert is not None)
        print(f"Alerts on for {alerting} of {len(rules)} rule(s).")

    interval = (args.every or args.bar_minutes) * 60

    def one_pass() -> int:
        result = scanner.scan(rules, symbols)
        stamp = datetime.now(ET).strftime("%H:%M:%S ET")
        print(f"\n[{stamp}] Scanned {result.scanned} symbols "
              f"against {len(rules)} rule(s).")

        note = closed_market_note(result, sessions)
        grouped = result.by_rule()
        if note:
            print(note)
        elif not grouped:
            print("No matches.")
        for rule_name, matches in grouped.items():
            print(f"── {rule_name}  ({len(matches)} match{'es' if len(matches) != 1 else ''})")
            for match in matches:
                print(f"   {match}")

        session = ""
        if result.matches:
            session = session_at(
                datetime.now(timezone.utc), result.matches[0].symbol, DEFAULT_CALENDAR
            )
        sent = notifier.notify(result.matches, rules_by_name, session)
        if sent:
            print(f"   → {sent} alert(s) sent")
        return result, sent

    result, _ = one_pass()

    if args.watch:
        print(f"\nWatching — rescanning every {interval // 60} minute(s). Ctrl-C to stop.")
        try:
            while True:
                time.sleep(interval)
                result, _ = one_pass()
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0

    if result.missing_float and args.verbose:
        print(f"No float data for {len(result.missing_float)} symbol(s).")

    if result.sparse:
        print(
            f"Note: {len(result.sparse)} symbol(s) returned sparse bars. Alpaca "
            f"builds bars from trades, so a window with no trades yields no bar "
            f"— common in pre-market, especially on the IEX feed. Rolling "
            f"indicators there span more wall-clock time than the bar count "
            f"suggests."
        )
        if args.verbose:
            for sym, detail in result.sparse.items():
                print(f"   {sym}: {detail}")

    if args.verbose and result.skipped:
        print("Skipped:")
        for sym, why in result.skipped.items():
            print(f"   {sym}: {why}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
