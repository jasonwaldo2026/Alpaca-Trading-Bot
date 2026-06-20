"""
Alpaca AI Trading Bot
======================
Paper Trading | US Equities + Crypto

Architecture:
  BotConfig              → all settings and API credentials
  AlpacaClient           → authenticated alpaca-py wrapper
  MarketDataFetcher      → hourly OHLCV bars for stocks and crypto
  BaseStrategy           → ABC — subclass to add new strategies
  EnhancedSMAStrategy    → SMA crossover + RSI + volume + ATR sizing (default)
  SMAcrossoverStrategy   → original SMA-only strategy (kept for reference)
  RiskManager            → ATR-scaled position sizing and exposure guards
  OrderManager           → signal → Alpaca market order
  TradingBot             → main poll loop orchestrator

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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from dotenv import load_dotenv
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
    """Fetches hourly OHLCV bars for stocks and crypto."""

    def __init__(self, client: AlpacaClient, config: BotConfig):
        self.client = client
        self.config = config

    def get_stock_bars(self) -> pd.DataFrame:
        if not self.config.stock_symbols:
            return pd.DataFrame()
        req = StockBarsRequest(
            symbol_or_symbols=self.config.stock_symbols,
            timeframe=TimeFrame.Hour,
            limit=self.config.bar_limit,
        )
        bars = self.client.stock_data.get_stock_bars(req)
        return bars.df if hasattr(bars, "df") else pd.DataFrame()

    def get_crypto_bars(self) -> pd.DataFrame:
        if not self.config.crypto_symbols:
            return pd.DataFrame()
        req = CryptoBarsRequest(
            symbol_or_symbols=self.config.crypto_symbols,
            timeframe=TimeFrame.Hour,
            limit=self.config.bar_limit,
        )
        bars = self.client.crypto_data.get_crypto_bars(req)
        return bars.df if hasattr(bars, "df") else pd.DataFrame()


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

        if signal.action == "SELL":
            return signal.symbol in positions, 0.0

        # BUY checks
        total_invested = sum(float(p.market_value) for p in positions.values())
        exposure = total_invested / portfolio_value if portfolio_value > 0 else 0

        if exposure >= self.config.max_total_exposure:
            log.info(
                "Risk block (exposure): %s — portfolio %.0f%% invested",
                signal.symbol, exposure * 100,
            )
            return False, 0.0

        if signal.symbol in positions:
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
            elif signal.action == "SELL" and signal.symbol in positions:
                self.client.close_position(signal.symbol)
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

    def __init__(self, config: BotConfig, strategy: Optional[BaseStrategy] = None):
        self.config = config
        self.client = AlpacaClient(config)
        self.data = MarketDataFetcher(self.client, config)
        self.strategy = strategy or SMAcrossoverStrategy()
        self.risk = RiskManager(config)
        self.orders = OrderManager(self.client)

    def run_once(self):
        log.info("── cycle ──────────────────────")
        portfolio_value = self.client.get_portfolio_value()
        positions = self.client.get_positions()
        log.info(
            "Portfolio: $%.2f  |  Open positions: %d",
            portfolio_value, len(positions),
        )

        stock_bars = self.data.get_stock_bars()
        crypto_bars = self.data.get_crypto_bars()

        signals = self.strategy.generate_signals(stock_bars, crypto_bars, self.config)
        action_signals = [s for s in signals if s.action != "HOLD"]
        if action_signals:
            log.info("Active signals: %s", [(s.symbol, s.action) for s in action_signals])

        for signal in signals:
            approved, notional = self.risk.evaluate(signal, portfolio_value, positions)
            if approved:
                log.info(
                    "Executing: %s %s  $%.2f  (%s)",
                    signal.action, signal.symbol, notional, signal.reason,
                )
                self.orders.execute(signal, notional, positions)

    def run(self):
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
        stock_symbols=["AAPL", "MSFT", "NVDA", "SPY", "QQQ"],
        crypto_symbols=["BTC/USD", "ETH/USD", "SOL/USD"],
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
