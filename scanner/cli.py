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

from dotenv import load_dotenv

from core.client import AlpacaClient, Credentials
from core.data import MarketDataFetcher
from core.rules import RuleError, load_rules
from scanner.engine import Scanner

DEFAULT_RULE_GLOB = "rules/*.json"


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Scan the market with saved rules.")
    parser.add_argument("rules", nargs="*", help=f"Rule JSON files (default: {DEFAULT_RULE_GLOB})")
    parser.add_argument("--symbols", help="Comma-separated symbols, overriding rule universes")
    parser.add_argument("--bars", type=int, default=120, help="Bars to fetch per symbol")
    parser.add_argument(
        "--bar-minutes", type=int, default=60,
        help="Bar size in minutes (5, 15, 30, 60). Must divide into 1440.",
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

    scanner = Scanner(
        MarketDataFetcher(AlpacaClient(creds), args.bar_minutes),
        bar_limit=args.bars,
        bar_minutes=args.bar_minutes,
    )
    symbols = args.symbols.split(",") if args.symbols else None
    result = scanner.scan(rules, symbols)

    print(f"\nScanned {result.scanned} symbols against {len(rules)} rule(s).\n")
    grouped = result.by_rule()
    if not grouped:
        print("No matches.")
    for rule_name, matches in grouped.items():
        print(f"── {rule_name}  ({len(matches)} match{'es' if len(matches) != 1 else ''})")
        for match in matches:
            print(f"   {match}")
        print()

    if args.verbose and result.skipped:
        print("Skipped:")
        for sym, why in result.skipped.items():
            print(f"   {sym}: {why}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
