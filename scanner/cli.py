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

from dotenv import load_dotenv

from core.client import AlpacaClient, Credentials
from core.data import MarketDataFetcher
from core.rules import RuleError, load_rules
from core.sessions import DEFAULT_CALENDAR, SessionConfig, session_at
from scanner.alerts import AlertNotifier
from scanner.engine import Scanner

DEFAULT_RULE_GLOB = "rules/*.json"


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Scan the market with saved rules.")
    parser.add_argument("rules", nargs="*", help=f"Rule JSON files (default: {DEFAULT_RULE_GLOB})")
    parser.add_argument("--symbols", help="Comma-separated symbols, overriding rule universes")
    parser.add_argument("--bars", type=int, default=300, help="Bars to fetch per symbol")
    parser.add_argument(
        "--bar-minutes", type=int, default=5,
        help="Bar size in minutes (5, 15, 30, 60). Must divide into 1440.",
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

    creds = Credentials.from_env()
    if not creds.is_complete():
        print("Missing ALPACA_API_KEY / ALPACA_API_SECRET.", file=sys.stderr)
        return 1

    feed = None
    if args.feed:
        from alpaca.data.enums import DataFeed
        feed = DataFeed(args.feed)

    sessions = SessionConfig.extended() if args.extended_hours else SessionConfig()
    scanner = Scanner(
        MarketDataFetcher(AlpacaClient(creds), args.bar_minutes, feed=feed),
        bar_limit=args.bars,
        bar_minutes=args.bar_minutes,
        sessions=sessions,
    )
    symbols = args.symbols.split(",") if args.symbols else None
    rules_by_name = {r.name: r for r in rules}
    notifier = AlertNotifier(transport=None) if args.no_alerts else AlertNotifier.from_env()
    if notifier.enabled:
        alerting = sum(1 for r in rules if r.alert is not None)
        print(f"Alerts on for {alerting} of {len(rules)} rule(s).")

    interval = (args.every or args.bar_minutes) * 60

    def one_pass() -> int:
        result = scanner.scan(rules, symbols)
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"\n[{stamp}] Scanned {result.scanned} symbols "
              f"against {len(rules)} rule(s).")

        grouped = result.by_rule()
        if not grouped:
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
