"""
Alpaca AI Trading Bot
======================
Paper Trading | US Equities + Crypto

Architecture:
  BotConfig              → all settings and API credentials
  AlpacaClient           → authenticated alpaca-py wrapper
  MarketDataFetcher      → hourly OHLCV bars for stocks and crypto
  AlpacaMarketScanner    → ranks a broad universe → shortlist  (scanner.py)
  BaseStrategy           → ABC — subclass to add new strategies
  EnhancedSMAStrategy    → SMA crossover + RSI + volume + ATR sizing (default)
  SMAcrossoverStrategy   → original SMA-only strategy (kept for reference)
  RiskManager            → ATR-scaled position sizing and exposure guards
  OrderManager           → signal → Alpaca market order
  TradingBot             → main poll loop orchestrator

Cycle flow:
  scanner ranks universe → shortlist (+ held positions) → strategy generates
  signals → risk manager sizes/vetoes → order manager executes

  The scanner only decides WHAT TO LOOK AT. Whether a trade actually fires is
  still entirely the strategy's call, so behaviour stays deterministic and
  every entry remains explainable from the indicator rules alone.

Signal confirmation logic (EnhancedSMAStrategy):
  BUY  = SMA golden cross  AND  RSI < rsi_overbought  AND  volume > 20-bar avg
  SELL = SMA death cross   AND  RSI > rsi_oversold    AND  volume > 20-bar avg

ATR position sizing:
  dollar_risk  = portfolio × risk_per_trade_pct   (default 1%)
  stop_dist    = ATR × atr_risk_multiplier         (default 1.5×)
  notional     = (dollar_risk / stop_dist) × price, capped at max_position_pct

Jules prompt ideas (create GitHub issues with these):
  • "Add a daily drawdown circuit breaker to RiskManager"
  • "Add Slack alerts when a trade executes (SLACK_WEBHOOK_URL)"
  • "Add a /positions FastAPI endpoint showing open positions"
  • "Add a backtesting mode that replays historical bars"
"""

import os
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

import pandas as pd

from dotenv import load_dotenv

from scanner import (
    BaseScanner,
    AlpacaMarketScanner,
    ScanResult,
    DEFAULT_STOCK_UNIVERSE,
    DEFAULT_CRYPTO_UNIVERSE,
)

load_dotenv()

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.data.historical import (
        StockHistoricalDataClient,
        CryptoHistoricalDataClient,
    )
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame
except ImportError:
    raise SystemExit("Run:  pip install -r requirements.txt")


# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger("alpaca_bot")


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class BotConfig:
    """All tunable parameters in one place. Edit here or pull from env vars."""

    # Alpaca credentials — use paper keys until you're ready for live
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("ALPACA_API_SECRET", ""))
    paper: bool = True  # ← switch to False only when you're fully ready

    # Watchlists
    stock_symbols: List[str] = field(
        default_factory=lambda: ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]
    )
    crypto_symbols: List[str] = field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"]
    )

    # Risk controls
    max_position_pct: float = 0.05    # hard cap: max % of portfolio per position
    max_total_exposure: float = 0.80  # max 80% of portfolio allocated at once
    risk_per_trade_pct: float = 0.01  # ATR sizing: risk this % of portfolio per trade
    atr_risk_multiplier: float = 1.5  # stop distance = ATR × this multiplier

    # SMA crossover parameters
    sma_fast: int = 10   # fast moving average period (bars)
    sma_slow: int = 30   # slow moving average period (bars)

    # RSI parameters
    rsi_period: int = 14
    rsi_overbought: float = 70.0   # block BUY if RSI is above this
    rsi_oversold: float = 30.0     # block SELL if RSI is below this

    # Volume confirmation
    volume_sma_period: int = 20    # bars for rolling average volume

    # ATR (volatility-based sizing)
    atr_period: int = 14

    # Polling
    poll_interval_seconds: int = 60   # how often the bot cycles
    bar_limit: int = 60               # must cover sma_slow + atr/rsi periods

    # ── Market scanner ────────────────────────────────────────────────────────
    # The scanner narrows a broad universe to a ranked shortlist each refresh;
    # the strategy still has to fire before any order is placed. Set
    # use_scanner=False to fall back to the fixed stock/crypto_symbols lists.
    use_scanner: bool = True

    scan_universe: List[str] = field(
        default_factory=lambda: list(DEFAULT_STOCK_UNIVERSE)
    )
    scan_crypto_universe: List[str] = field(
        default_factory=lambda: list(DEFAULT_CRYPTO_UNIVERSE)
    )

    scan_top_n: int = 8          # shortlist size handed to the strategy
    scan_refresh_cycles: int = 15  # rescan every N cycles (bars are hourly)

    # Hard filters — a symbol failing any of these is dropped, never scored
    scan_min_price: float = 5.0             # skip penny stocks
    scan_min_dollar_volume: float = 1e6     # skip illiquid names
    scan_max_atr_pct: float = 15.0          # skip anything wildly volatile
    scan_require_uptrend: bool = False      # True = long-only, above slow SMA

    scan_momentum_lookback: int = 12        # bars for the momentum metric

    # Composite score weights (normalised internally, so they need not sum to 1)
    scan_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "volume": 0.30,
            "momentum": 0.40,
            "volatility": 0.15,
            "trend": 0.15,
        }
    )


# ── Signal data type ──────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol: str
    action: str        # "BUY" | "SELL" | "HOLD"
    asset_class: str   # "stock" | "crypto"
    confidence: float  # 0.0 – 1.0
    reason: str = ""
    atr: float = 0.0           # current ATR value (used for position sizing)
    current_price: float = 0.0 # last close price (used for sizing math)


# ── Symbol helpers ────────────────────────────────────────────────────────────

def norm_symbol(symbol: str) -> str:
    """
    Canonical form for symbol comparison.

    Alpaca reports crypto *positions* as "BTCUSD" but accepts and returns
    crypto *bars* as "BTC/USD". Comparing the two raw forms silently fails,
    which would leave held crypto unsellable and let duplicate buys through.
    Always compare via this helper rather than by raw string.
    """
    return symbol.replace("/", "").upper()


def resolve_position_key(symbol: str, positions: Dict) -> Optional[str]:
    """Find the actual positions-dict key matching a signal symbol, or None."""
    target = norm_symbol(symbol)
    for key in positions:
        if norm_symbol(key) == target:
            return key
    return None


# ── Alpaca client ─────────────────────────────────────────────────────────────

class AlpacaClient:
    """Authenticated wrapper around alpaca-py REST clients."""

    def __init__(self, config: BotConfig):
        if not config.api_key or not config.api_secret:
            raise ValueError(
                "Missing credentials — set ALPACA_API_KEY and "
                "ALPACA_API_SECRET in your .env file."
            )
        self.trading = TradingClient(
            config.api_key, config.api_secret, paper=config.paper
        )
        self.stock_data = StockHistoricalDataClient(
            config.api_key, config.api_secret
        )
        self.crypto_data = CryptoHistoricalDataClient(
            config.api_key, config.api_secret
        )
        log.info("Alpaca client ready  (paper=%s)", config.paper)

    def get_account(self):
        return self.trading.get_account()

    def get_portfolio_value(self) -> float:
        return float(self.get_account().portfolio_value)

    def get_positions(self) -> Dict[str, object]:
        return {p.symbol: p for p in self.trading.get_all_positions()}

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        notional: Optional[float] = None,
        qty: Optional[float] = None,
    ):
        """Buy/sell by notional USD or by quantity."""
        if notional:
            req = MarketOrderRequest(
                symbol=symbol,
                notional=round(notional, 2),
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.GTC,
            )
        order = self.trading.submit_order(req)
        log.info(
            "Order submitted ▶ %s %s  notional=%s  qty=%s",
            side, symbol, notional, qty,
        )
        return order

    def close_position(self, symbol: str):
        self.trading.close_position(symbol)
        log.info("Position closed: %s", symbol)


# ── Market data fetcher ───────────────────────────────────────────────────────

class MarketDataFetcher:
    """
    Fetches hourly OHLCV bars for stocks and crypto.

    Both getters take an optional explicit symbol list so the scanner can pull
    a wide universe while the strategy pulls only its shortlist. Passing None
    falls back to the configured watchlists.

    Requests are chunked because a scanner universe can run to hundreds of
    symbols, which is more than one request should carry.
    """

    CHUNK_SIZE = 200

    def __init__(self, client: AlpacaClient, config: BotConfig):
        self.client = client
        self.config = config

    @staticmethod
    def _chunks(symbols: List[str], size: int):
        for i in range(0, len(symbols), size):
            yield symbols[i:i + size]

    def _fetch(self, symbols: List[str], is_crypto: bool) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()

        frames: List[pd.DataFrame] = []
        for chunk in self._chunks(list(symbols), self.CHUNK_SIZE):
            try:
                if is_crypto:
                    req = CryptoBarsRequest(
                        symbol_or_symbols=chunk,
                        timeframe=TimeFrame.Hour,
                        limit=self.config.bar_limit,
                    )
                    bars = self.client.crypto_data.get_crypto_bars(req)
                else:
                    req = StockBarsRequest(
                        symbol_or_symbols=chunk,
                        timeframe=TimeFrame.Hour,
                        limit=self.config.bar_limit,
                    )
                    bars = self.client.stock_data.get_stock_bars(req)

                df = bars.df if hasattr(bars, "df") else pd.DataFrame()
                if not df.empty:
                    frames.append(df)
            except Exception as exc:
                # One bad chunk shouldn't blind the whole cycle.
                log.warning(
                    "Bar fetch failed for %d %s symbols: %s",
                    len(chunk), "crypto" if is_crypto else "stock", exc,
                )

        if not frames:
            return pd.DataFrame()
        return frames[0] if len(frames) == 1 else pd.concat(frames)

    def get_stock_bars(self, symbols: Optional[List[str]] = None) -> pd.DataFrame:
        return self._fetch(
            self.config.stock_symbols if symbols is None else symbols,
            is_crypto=False,
        )

    def get_crypto_bars(self, symbols: Optional[List[str]] = None) -> pd.DataFrame:
        return self._fetch(
            self.config.crypto_symbols if symbols is None else symbols,
            is_crypto=True,
        )


# ── Strategy base class ───────────────────────────────────────────────────────

class BaseStrategy(ABC):
    """
    Subclass this to add a new strategy.

    Jules issue template:
        Implement a [RSI/VWAP/momentum] strategy by subclassing BaseStrategy.
        generate_signals() must return List[Signal].
    """

    @abstractmethod
    def generate_signals(
        self,
        stock_bars: pd.DataFrame,
        crypto_bars: pd.DataFrame,
        config: BotConfig,
    ) -> List[Signal]:
        ...


# ── Enhanced strategy: SMA + RSI + Volume + ATR ───────────────────────────────

class EnhancedSMAStrategy(BaseStrategy):
    """
    Three-confirmation strategy — all three must agree before a trade fires.

    BUY:  SMA golden cross  +  RSI < rsi_overbought  +  volume > 20-bar avg
    SELL: SMA death cross   +  RSI > rsi_oversold    +  volume > 20-bar avg

    Signals carry atr and current_price so RiskManager can size positions
    proportionally to volatility instead of using a flat percentage.
    """

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        prev_close = df["close"].shift()
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return tr.ewm(com=period - 1, min_periods=period).mean()

    def _signals_for_bars(
        self,
        bars: pd.DataFrame,
        symbols: List[str],
        asset_class: str,
        config: BotConfig,
    ) -> List[Signal]:
        min_bars = max(config.sma_slow, config.rsi_period,
                       config.volume_sma_period, config.atr_period) + 2
        signals: List[Signal] = []

        for sym in symbols:
            try:
                df = (
                    bars.xs(sym, level="symbol")
                    if isinstance(bars.index, pd.MultiIndex)
                    else bars
                ).copy()

                if len(df) < min_bars:
                    log.debug("Not enough bars for %s (%d < %d)", sym, len(df), min_bars)
                    continue

                df["sma_fast"] = df["close"].rolling(config.sma_fast).mean()
                df["sma_slow"] = df["close"].rolling(config.sma_slow).mean()
                df["rsi"]      = self._rsi(df["close"], config.rsi_period)
                df["vol_sma"]  = df["volume"].rolling(config.volume_sma_period).mean()
                df["atr"]      = self._atr(df, config.atr_period)
                df.dropna(inplace=True)

                if len(df) < 2:
                    continue

                prev, curr = df.iloc[-2], df.iloc[-1]
                atr   = float(curr["atr"])
                price = float(curr["close"])
                rsi   = float(curr["rsi"])

                golden = (prev["sma_fast"] <= prev["sma_slow"]
                          and curr["sma_fast"] > curr["sma_slow"])
                death  = (prev["sma_fast"] >= prev["sma_slow"]
                          and curr["sma_fast"] < curr["sma_slow"])
                high_vol = curr["volume"] > curr["vol_sma"]

                if golden:
                    if not high_vol:
                        reason = f"Golden cross but low volume — skipping"
                        signals.append(Signal(sym, "HOLD", asset_class, 0.35, reason, atr, price))
                    elif rsi >= config.rsi_overbought:
                        reason = f"Golden cross but RSI overbought ({rsi:.1f}) — skipping"
                        signals.append(Signal(sym, "HOLD", asset_class, 0.35, reason, atr, price))
                    else:
                        reason = f"SMA golden cross | RSI {rsi:.1f} | volume confirmed"
                        signals.append(Signal(sym, "BUY", asset_class, 0.85, reason, atr, price))

                elif death:
                    if not high_vol:
                        reason = f"Death cross but low volume — skipping"
                        signals.append(Signal(sym, "HOLD", asset_class, 0.35, reason, atr, price))
                    elif rsi <= config.rsi_oversold:
                        reason = f"Death cross but RSI oversold ({rsi:.1f}) — skipping"
                        signals.append(Signal(sym, "HOLD", asset_class, 0.35, reason, atr, price))
                    else:
                        reason = f"SMA death cross | RSI {rsi:.1f} | volume confirmed"
                        signals.append(Signal(sym, "SELL", asset_class, 0.85, reason, atr, price))

                else:
                    signals.append(Signal(sym, "HOLD", asset_class, 0.50, "No crossover", atr, price))

            except Exception as exc:
                log.warning("Signal error for %s: %s", sym, exc)

        return signals

    def generate_signals(
        self, stock_bars: pd.DataFrame, crypto_bars: pd.DataFrame, config: BotConfig
    ) -> List[Signal]:
        signals: List[Signal] = []
        if not stock_bars.empty:
            signals += self._signals_for_bars(
                stock_bars, config.stock_symbols, "stock", config
            )
        if not crypto_bars.empty:
            signals += self._signals_for_bars(
                crypto_bars, config.crypto_symbols, "crypto", config
            )
        return signals


# ── Original strategy (SMA only — kept for reference / A-B testing) ───────────

class SMAcrossoverStrategy(BaseStrategy):
    """Original SMA-only strategy. Pass to TradingBot(strategy=SMAcrossoverStrategy())
    to compare against EnhancedSMAStrategy."""

    def _signals_for_bars(self, bars, symbols, asset_class, config):
        signals: List[Signal] = []
        for sym in symbols:
            try:
                df = (
                    bars.xs(sym, level="symbol")
                    if isinstance(bars.index, pd.MultiIndex)
                    else bars
                ).copy()
                if len(df) < config.sma_slow + 2:
                    continue
                df["sma_fast"] = df["close"].rolling(config.sma_fast).mean()
                df["sma_slow"] = df["close"].rolling(config.sma_slow).mean()
                df.dropna(inplace=True)
                prev, curr = df.iloc[-2], df.iloc[-1]
                if prev["sma_fast"] <= prev["sma_slow"] and curr["sma_fast"] > curr["sma_slow"]:
                    signals.append(Signal(sym, "BUY", asset_class, 0.75, "SMA golden cross"))
                elif prev["sma_fast"] >= prev["sma_slow"] and curr["sma_fast"] < curr["sma_slow"]:
                    signals.append(Signal(sym, "SELL", asset_class, 0.75, "SMA death cross"))
                else:
                    signals.append(Signal(sym, "HOLD", asset_class, 0.50, "No crossover"))
            except Exception as exc:
                log.warning("Signal error for %s: %s", sym, exc)
        return signals

    def generate_signals(self, stock_bars, crypto_bars, config):
        signals: List[Signal] = []
        if not stock_bars.empty:
            signals += self._signals_for_bars(stock_bars, config.stock_symbols, "stock", config)
        if not crypto_bars.empty:
            signals += self._signals_for_bars(crypto_bars, config.crypto_symbols, "crypto", config)
        return signals


# ── Risk manager ──────────────────────────────────────────────────────────────

class RiskManager:
    """
    Validates signals and sizes positions before they reach the order manager.

    Position sizing (ATR-based):
        dollar_risk = portfolio × risk_per_trade_pct
        stop_dist   = ATR × atr_risk_multiplier
        notional    = (dollar_risk / stop_dist) × price
        — capped at portfolio × max_position_pct

    High-ATR (volatile) assets automatically receive smaller positions;
    low-ATR assets can grow toward the cap. Falls back to flat max_position_pct
    when ATR data is unavailable.

    Jules issue: "Add a daily drawdown circuit breaker — block all BUY orders
    if portfolio PnL today falls below -3%."
    """

    def __init__(self, config: BotConfig):
        self.config = config

    def _size_notional(self, signal: Signal, portfolio_value: float) -> float:
        max_notional = portfolio_value * self.config.max_position_pct

        if signal.atr > 0 and signal.current_price > 0:
            dollar_risk  = portfolio_value * self.config.risk_per_trade_pct
            stop_dist    = signal.atr * self.config.atr_risk_multiplier
            shares       = dollar_risk / stop_dist
            notional     = shares * signal.current_price
            notional     = min(notional, max_notional)
            log.debug(
                "ATR sizing %s: atr=%.4f stop=%.4f shares=%.3f notional=$%.2f",
                signal.symbol, signal.atr, stop_dist, shares, notional,
            )
        else:
            notional = max_notional
            log.debug("ATR unavailable for %s — using flat sizing $%.2f",
                      signal.symbol, notional)

        return round(notional, 2)

    def evaluate(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: Dict,
    ) -> Tuple[bool, float]:
        """Returns (approved, notional_usd). SELL notional is always 0."""
        if signal.action == "HOLD":
            return False, 0.0

        # Symbol matching goes through norm_symbol so crypto ("BTC/USD" in
        # signals vs "BTCUSD" in positions) compares correctly.
        if signal.action == "SELL":
            return resolve_position_key(signal.symbol, positions) is not None, 0.0

        # BUY checks
        total_invested = sum(float(p.market_value) for p in positions.values())
        exposure = total_invested / portfolio_value if portfolio_value > 0 else 0

        if exposure >= self.config.max_total_exposure:
            log.info(
                "Risk block (exposure): %s — portfolio %.0f%% invested",
                signal.symbol, exposure * 100,
            )
            return False, 0.0

        if resolve_position_key(signal.symbol, positions) is not None:
            log.info("Risk block (duplicate): already holding %s", signal.symbol)
            return False, 0.0

        notional = self._size_notional(signal, portfolio_value)
        return True, notional


# ── Order manager ─────────────────────────────────────────────────────────────

class OrderManager:
    """Translates approved signals into Alpaca API calls."""

    def __init__(self, client: AlpacaClient):
        self.client = client

    def execute(self, signal: Signal, notional: float, positions: Dict):
        try:
            if signal.action == "BUY":
                self.client.place_market_order(
                    signal.symbol, OrderSide.BUY, notional=notional
                )
            elif signal.action == "SELL":
                # Close using the broker's own key for the position, which for
                # crypto differs from the signal symbol ("BTCUSD" vs "BTC/USD").
                key = resolve_position_key(signal.symbol, positions)
                if key:
                    self.client.close_position(key)
                else:
                    log.warning(
                        "SELL for %s but no matching position found", signal.symbol
                    )
        except Exception as exc:
            log.error("Order failed for %s: %s", signal.symbol, exc)


# ── TradingBot orchestrator ───────────────────────────────────────────────────

class TradingBot:
    """
    Main orchestrator — wires all components and runs the poll loop.

    Default strategy is EnhancedSMAStrategy (SMA + RSI + volume + ATR sizing).
    To compare against the original:
        bot = TradingBot(config, strategy=SMAcrossoverStrategy())

    Jules issue ideas:
        • "Add Slack/email alerts when a trade executes"
        • "Add a /positions FastAPI endpoint"
        • "Schedule run_once() with APScheduler instead of sleep loop"
        • "Add backtesting mode using get_stock_bars(start=..., end=...)"
    """

    def __init__(
        self,
        config: BotConfig,
        strategy: Optional[BaseStrategy] = None,
        scanner: Optional[BaseScanner] = None,
    ):
        self.config = config
        self.client = AlpacaClient(config)
        self.data = MarketDataFetcher(self.client, config)
        self.strategy = strategy or EnhancedSMAStrategy()
        self.risk = RiskManager(config)
        self.orders = OrderManager(self.client)

        self.scanner: Optional[BaseScanner] = None
        if config.use_scanner:
            self.scanner = scanner or AlpacaMarketScanner()

        self.shortlist: List[ScanResult] = []
        self._cycle = 0

    # ── Scanner plumbing ─────────────────────────────────────────────────────

    def refresh_shortlist(self) -> List[ScanResult]:
        """Pull the full universe, rank it, and cache the top candidates."""
        if not self.scanner:
            return []

        if getattr(self.scanner, "needs_bars", True):
            log.info(
                "Scanning universe: %d stocks, %d crypto",
                len(self.config.scan_universe), len(self.config.scan_crypto_universe),
            )
            universe_stock = self.data.get_stock_bars(self.config.scan_universe)
            universe_crypto = self.data.get_crypto_bars(self.config.scan_crypto_universe)
        else:
            # Scanner sources its own data — don't pay for bars it will discard.
            log.info("Querying %s", type(self.scanner).__name__)
            universe_stock = universe_crypto = pd.DataFrame()

        ranked = self.scanner.scan(universe_stock, universe_crypto, self.config)
        self.shortlist = ranked[: self.config.scan_top_n]

        for res in self.shortlist:
            log.info("  shortlist  %s  — %s", res.summary(), "; ".join(res.reasons))
        return self.shortlist

    def _active_symbols(self, positions: Dict) -> Tuple[List[str], List[str]]:
        """
        Symbols the strategy evaluates this cycle:
        the scanner shortlist PLUS everything currently held.

        Held positions are non-negotiable. If a symbol rotates out of the
        shortlist while we still own it, dropping it here would mean its SELL
        signal never gets generated and the position is stranded with no exit.
        """
        if self.scanner:
            stocks = [r.symbol for r in self.shortlist if r.asset_class == "stock"]
            crypto = [r.symbol for r in self.shortlist if r.asset_class == "crypto"]
        else:
            stocks = list(self.config.stock_symbols)
            crypto = list(self.config.crypto_symbols)

        # Re-attach held positions, restoring the slashed form for crypto.
        crypto_lookup = {
            norm_symbol(s): s
            for s in (*self.config.scan_crypto_universe, *self.config.crypto_symbols)
        }
        held_stock = {norm_symbol(s) for s in stocks}
        held_crypto = {norm_symbol(s) for s in crypto}

        for key in positions:
            n = norm_symbol(key)
            if n in crypto_lookup:
                if n not in held_crypto:
                    crypto.append(crypto_lookup[n])
                    held_crypto.add(n)
            elif n not in held_stock:
                stocks.append(key)
                held_stock.add(n)

        return stocks, crypto

    # ── Main cycle ───────────────────────────────────────────────────────────

    def run_once(self):
        log.info("── cycle ──────────────────────")
        portfolio_value = self.client.get_portfolio_value()
        positions = self.client.get_positions()
        log.info(
            "Portfolio: $%.2f  |  Open positions: %d",
            portfolio_value, len(positions),
        )

        # Rescan periodically rather than every cycle — bars are hourly, so a
        # 60s rescan would re-rank identical data at real API cost.
        if self.scanner and (
            not self.shortlist or self._cycle % self.config.scan_refresh_cycles == 0
        ):
            self.refresh_shortlist()
        self._cycle += 1

        stock_symbols, crypto_symbols = self._active_symbols(positions)
        if not stock_symbols and not crypto_symbols:
            log.info("Nothing to evaluate this cycle.")
            return

        log.info(
            "Evaluating %d symbols: %s",
            len(stock_symbols) + len(crypto_symbols),
            ", ".join([*stock_symbols, *crypto_symbols]),
        )

        stock_bars = self.data.get_stock_bars(stock_symbols)
        crypto_bars = self.data.get_crypto_bars(crypto_symbols)

        # Hand the strategy a config view scoped to this cycle's symbols, so
        # BaseStrategy's documented signature stays unchanged.
        cycle_config = replace(
            self.config,
            stock_symbols=stock_symbols,
            crypto_symbols=crypto_symbols,
        )

        signals = self.strategy.generate_signals(stock_bars, crypto_bars, cycle_config)
        action_signals = [s for s in signals if s.action != "HOLD"]
        if action_signals:
            log.info("Active signals: %s", [(s.symbol, s.action) for s in action_signals])

        scores = {norm_symbol(r.symbol): r.score for r in self.shortlist}

        for signal in signals:
            approved, notional = self.risk.evaluate(signal, portfolio_value, positions)
            if approved:
                score = scores.get(norm_symbol(signal.symbol))
                log.info(
                    "Executing: %s %s  $%.2f  (%s)%s",
                    signal.action, signal.symbol, notional, signal.reason,
                    f"  [scan {score:.3f}]" if score is not None else "",
                )
                self.orders.execute(signal, notional, positions)

    def run(self):
        if self.scanner:
            log.info(
                "Bot starting  paper=%s  interval=%ds  scanner=%s  "
                "universe=%d  top_n=%d",
                self.config.paper,
                self.config.poll_interval_seconds,
                type(self.scanner).__name__,
                len(self.config.scan_universe) + len(self.config.scan_crypto_universe),
                self.config.scan_top_n,
            )
        else:
            log.info(
                "Bot starting  paper=%s  interval=%ds  stocks=%s  crypto=%s",
                self.config.paper,
                self.config.poll_interval_seconds,
                self.config.stock_symbols,
                self.config.crypto_symbols,
            )
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                log.info("Bot stopped.")
                break
            except Exception as exc:
                log.error("Cycle error: %s", exc, exc_info=True)
            time.sleep(self.config.poll_interval_seconds)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = BotConfig(
        paper=True,
        # Fallback watchlists — used only when use_scanner=False
        stock_symbols=["AAPL", "MSFT", "NVDA", "SPY", "QQQ"],
        crypto_symbols=["BTC/USD", "ETH/USD", "SOL/USD"],
        # Market scanner — narrows the universe to a ranked shortlist
        use_scanner=True,
        scan_top_n=8,
        scan_refresh_cycles=15,
        # SMA
        sma_fast=10,
        sma_slow=30,
        # RSI confirmation
        rsi_period=14,
        rsi_overbought=70.0,
        rsi_oversold=30.0,
        # Volume confirmation
        volume_sma_period=20,
        # ATR position sizing
        atr_period=14,
        risk_per_trade_pct=0.01,   # risk 1% of portfolio per trade
        atr_risk_multiplier=1.5,
        max_position_pct=0.05,     # hard cap regardless of ATR sizing
        max_total_exposure=0.80,
        poll_interval_seconds=60,
    )
    TradingBot(config, strategy=EnhancedSMAStrategy()).run()
