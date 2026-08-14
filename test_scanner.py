"""
Scanner tests — synthetic bars, no API calls.

Run:  python test_scanner.py
"""

import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

from scanner import AlpacaMarketScanner, ScanResult
from trading_bot import BotConfig, TradingBot, norm_symbol, resolve_position_key


PASS, FAIL = 0, 0


def check(name, cond, detail=""):  # noqa: D103
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_bars(symbols, n=80, seed=0, trend=0.0, vol_spike=None, base_price=100.0):
    """Build a MultiIndex (symbol, timestamp) OHLCV frame like alpaca-py returns."""
    rng = np.random.default_rng(seed)
    frames = []
    for i, sym in enumerate(symbols):
        drift = trend if not isinstance(trend, dict) else trend.get(sym, 0.0)
        steps = rng.normal(drift, 0.5, n).cumsum()
        close = base_price + steps
        close = np.maximum(close, 1.0)
        high = close * 1.01
        low = close * 0.99
        volume = rng.uniform(9e5, 1.1e6, n)
        if vol_spike and sym in vol_spike:
            volume[-1] *= vol_spike[sym]
        idx = pd.MultiIndex.from_product(
            [[sym], pd.date_range("2026-01-01", periods=n, freq="h")],
            names=["symbol", "timestamp"],
        )
        frames.append(pd.DataFrame(
            {"open": close, "high": high, "low": low,
             "close": close, "volume": volume}, index=idx))
    return pd.concat(frames)


def main():
    print("\n=== Scanner ranking ===")

    cfg = BotConfig(
        api_key="x", api_secret="y",
        scan_universe=["AAA", "BBB", "CCC", "DDD"],
        scan_crypto_universe=[],
        scan_min_dollar_volume=1_000,
    )
    scanner = AlpacaMarketScanner()

    # BBB gets strong upward drift + a volume spike -> should rank top.
    bars = make_bars(
        ["AAA", "BBB", "CCC", "DDD"],
        trend={"AAA": 0.0, "BBB": 0.30, "CCC": -0.05, "DDD": 0.0},
        vol_spike={"BBB": 4.0},
        seed=7,
    )
    results = scanner.scan(bars, pd.DataFrame(), cfg)

    check("returns results", len(results) > 0, f"got {len(results)}")
    check("all results are ScanResult", all(isinstance(r, ScanResult) for r in results))
    check("sorted best-first",
          all(results[i].score >= results[i + 1].score for i in range(len(results) - 1)))
    check("high-momentum + volume symbol ranks first",
          results[0].symbol == "BBB", f"got {results[0].symbol}")
    check("scores within 0..1", all(0.0 <= r.score <= 1.0 for r in results))
    for r in results:
        print(f"        {r.summary()}")


    print("\n=== Hard filters drop, never merely down-rank ===")

    cheap_cfg = BotConfig(api_key="x", api_secret="y",
                          scan_universe=["AAA", "PENNY"], scan_crypto_universe=[],
                          scan_min_price=5.0, scan_min_dollar_volume=1_000)
    mixed = pd.concat([
        make_bars(["AAA"], seed=1, base_price=100.0),
        make_bars(["PENNY"], seed=2, base_price=2.0),
    ])
    res = scanner.scan(mixed, pd.DataFrame(), cheap_cfg)
    check("sub-$5 symbol filtered out",
          all(r.symbol != "PENNY" for r in res), [r.symbol for r in res])

    illiq_cfg = BotConfig(api_key="x", api_secret="y",
                          scan_universe=["AAA"], scan_crypto_universe=[],
                          scan_min_dollar_volume=1e12)
    check("illiquid universe yields empty shortlist",
          scanner.scan(make_bars(["AAA"], seed=3), pd.DataFrame(), illiq_cfg) == [])

    vol_cfg = BotConfig(api_key="x", api_secret="y",
                        scan_universe=["AAA"], scan_crypto_universe=[],
                        scan_min_dollar_volume=1_000, scan_max_atr_pct=0.001)
    check("over-volatile symbol filtered out",
          scanner.scan(make_bars(["AAA"], seed=4), pd.DataFrame(), vol_cfg) == [])


    print("\n=== Insufficient data is skipped, not crashed on ===")
    short = make_bars(["AAA"], n=5, seed=5)
    check("too-few-bars symbol skipped",
          scanner.scan(short, pd.DataFrame(), cfg) == [])
    check("empty frame handled",
          scanner.scan(pd.DataFrame(), pd.DataFrame(), cfg) == [])


    print("\n=== Crypto symbol normalisation ===")
    check("norm strips slash", norm_symbol("BTC/USD") == "BTCUSD")
    check("norm is case-insensitive", norm_symbol("btc/usd") == "BTCUSD")

    positions = {"BTCUSD": SimpleNamespace(market_value="100"),
                 "AAPL": SimpleNamespace(market_value="200")}
    check("resolves slashed crypto to position key",
          resolve_position_key("BTC/USD", positions) == "BTCUSD")
    check("resolves plain stock", resolve_position_key("AAPL", positions) == "AAPL")
    check("returns None when absent", resolve_position_key("MSFT", positions) is None)


    print("\n=== Held positions always stay in the evaluated set ===")

    bot = TradingBot.__new__(TradingBot)   # bypass __init__ (no API credentials)
    bot.config = BotConfig(
        api_key="x", api_secret="y",
        scan_crypto_universe=["BTC/USD", "ETH/USD"],
        crypto_symbols=["BTC/USD"],
    )
    bot.scanner = scanner
    bot.shortlist = [
        ScanResult("NVDA", "stock", 0.9, 100, 2.0, 5.0, 2.0, 1e8, True),
        ScanResult("ETH/USD", "crypto", 0.8, 3000, 1.5, 3.0, 3.0, 1e8, True),
    ]

    held = {"TSLA": SimpleNamespace(market_value="500"),
            "BTCUSD": SimpleNamespace(market_value="300")}
    stocks, crypto = bot._active_symbols(held)

    check("shortlist stock present", "NVDA" in stocks, stocks)
    check("held stock NOT in shortlist is re-added", "TSLA" in stocks, stocks)
    check("shortlist crypto present", "ETH/USD" in crypto, crypto)
    check("held crypto re-added in slashed form", "BTC/USD" in crypto, crypto)
    check("held crypto not duplicated as BTCUSD", "BTCUSD" not in crypto, crypto)

    # A held symbol that is also on the shortlist must not appear twice.
    bot.shortlist = [ScanResult("TSLA", "stock", 0.9, 100, 2.0, 5.0, 2.0, 1e8, True)]
    stocks2, _ = bot._active_symbols({"TSLA": SimpleNamespace(market_value="500")})
    check("no duplicate when held symbol is also shortlisted",
          stocks2.count("TSLA") == 1, stocks2)

    # Scanner disabled -> falls back to the static watchlists.
    bot.scanner = None
    bot.shortlist = []
    s3, c3 = bot._active_symbols({})
    check("falls back to configured watchlist when scanner off",
          s3 == bot.config.stock_symbols and c3 == bot.config.crypto_symbols, (s3, c3))


    print("\n=== Risk manager honours normalised symbols ===")
    from trading_bot import RiskManager, Signal

    rm = RiskManager(BotConfig(api_key="x", api_secret="y"))
    sell_btc = Signal("BTC/USD", "SELL", "crypto", 0.9)
    approved, _ = rm.evaluate(sell_btc, 10_000, {"BTCUSD": SimpleNamespace(market_value="300")})
    check("SELL on held crypto is approved (was broken before)", approved)

    buy_btc = Signal("BTC/USD", "BUY", "crypto", 0.9, atr=50, current_price=3000)
    approved2, _ = rm.evaluate(buy_btc, 10_000, {"BTCUSD": SimpleNamespace(market_value="300")})
    check("duplicate BUY on held crypto is blocked", not approved2)


    print(f"\n{'─' * 46}\n  {PASS} passed, {FAIL} failed\n")
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
