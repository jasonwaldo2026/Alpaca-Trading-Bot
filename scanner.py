"""
Market Scanner
==============
Screens a broad symbol universe down to a ranked shortlist of candidates,
which the strategy layer then evaluates for actual entry signals.

Design contract
---------------
The scanner NARROWS, it does not DECIDE. It answers "what deserves a closer
look this cycle?" — never "what should I buy?". Entry signals remain the
exclusive job of BaseStrategy, so every trade is still gated by the same
deterministic SMA + RSI + volume rules and stays backtestable.

    BaseScanner        → ABC; implement scan() to plug in any data source
    AlpacaMarketScanner→ default; ranks on volume surge, momentum, volatility
    ScanResult         → one ranked candidate + the metrics behind its score

Swapping in an external scanner
-------------------------------
Subclass BaseScanner, return List[ScanResult], and pass it to TradingBot:

    bot = TradingBot(config, strategy=..., scanner=MyExternalScanner())

Scoring
-------
Each metric is percentile-ranked across the surviving universe (0..1), then
combined with configurable weights. Percentile ranking makes the score
relative to today's market rather than to hardcoded absolute thresholds,
so the scanner adapts to quiet and volatile regimes alike.

    score = w_volume   × pct_rank(volume_ratio)
          + w_momentum × pct_rank(momentum_pct)
          + w_volatility × pct_rank(atr_pct)
          + w_trend    × trend_bonus
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import pandas as pd

log = logging.getLogger("alpaca_bot.scanner")


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    """One ranked candidate, with the metrics that produced its score."""

    symbol: str
    asset_class: str          # "stock" | "crypto"
    score: float              # 0.0 – 1.0 composite rank
    price: float
    volume_ratio: float       # latest volume ÷ rolling average volume
    momentum_pct: float       # % price change over the lookback window
    atr_pct: float            # ATR as % of price (normalised volatility)
    dollar_volume: float      # latest volume × price
    trend_ok: bool            # price above the slow SMA
    reasons: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.symbol:<10} score={self.score:.3f}  "
            f"vol×{self.volume_ratio:.2f}  mom={self.momentum_pct:+.2f}%  "
            f"atr={self.atr_pct:.2f}%  {'trend↑' if self.trend_ok else 'trend↓'}"
        )


# ── Base class ────────────────────────────────────────────────────────────────

class BaseScanner(ABC):
    """Subclass to plug in any candidate source (external app, API, CSV)."""

    @abstractmethod
    def scan(
        self,
        stock_bars: pd.DataFrame,
        crypto_bars: pd.DataFrame,
        config,
    ) -> List[ScanResult]:
        """Return candidates ranked best-first."""
        ...


# ── Default implementation ────────────────────────────────────────────────────

class AlpacaMarketScanner(BaseScanner):
    """
    Ranks symbols using only Alpaca OHLCV bars — no extra credentials needed.

    Hard filters run first (price floor, liquidity floor, volatility ceiling);
    survivors are then percentile-ranked and scored. A symbol failing any hard
    filter is dropped outright rather than scored low, so junk can never place
    into the shortlist just because the rest of the universe looks worse.
    """

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

    def _metrics_for_symbol(
        self,
        df: pd.DataFrame,
        symbol: str,
        asset_class: str,
        config,
    ) -> Optional[Dict]:
        """Compute raw (unranked) metrics, or None if the symbol is unusable."""
        lookback = config.scan_momentum_lookback
        min_bars = max(config.sma_slow, config.atr_period, lookback,
                       config.volume_sma_period) + 2

        if len(df) < min_bars:
            log.debug("Scanner: %s has %d bars, needs %d", symbol, len(df), min_bars)
            return None

        df = df.copy()
        df["vol_sma"] = df["volume"].rolling(config.volume_sma_period).mean()
        df["sma_slow"] = df["close"].rolling(config.sma_slow).mean()
        df["atr"] = self._atr(df, config.atr_period)
        df.dropna(inplace=True)

        if len(df) < lookback + 1:
            return None

        curr = df.iloc[-1]
        price = float(curr["close"])
        if price <= 0:
            return None

        avg_vol = float(curr["vol_sma"])
        volume_ratio = float(curr["volume"]) / avg_vol if avg_vol > 0 else 0.0

        past_price = float(df["close"].iloc[-(lookback + 1)])
        momentum_pct = ((price - past_price) / past_price * 100) if past_price > 0 else 0.0

        atr_pct = float(curr["atr"]) / price * 100
        dollar_volume = float(curr["volume"]) * price
        trend_ok = price > float(curr["sma_slow"])

        # ── Hard filters ──────────────────────────────────────────────────────
        if price < config.scan_min_price:
            log.debug("Scanner filter: %s price $%.2f below floor", symbol, price)
            return None
        if dollar_volume < config.scan_min_dollar_volume:
            log.debug("Scanner filter: %s illiquid ($%.0f)", symbol, dollar_volume)
            return None
        if atr_pct > config.scan_max_atr_pct:
            log.debug("Scanner filter: %s too volatile (ATR %.2f%%)", symbol, atr_pct)
            return None
        if config.scan_require_uptrend and not trend_ok:
            log.debug("Scanner filter: %s below slow SMA", symbol)
            return None

        return {
            "symbol": symbol,
            "asset_class": asset_class,
            "price": price,
            "volume_ratio": volume_ratio,
            "momentum_pct": momentum_pct,
            "atr_pct": atr_pct,
            "dollar_volume": dollar_volume,
            "trend_ok": trend_ok,
        }

    def _collect(
        self,
        bars: pd.DataFrame,
        symbols: Sequence[str],
        asset_class: str,
        config,
    ) -> List[Dict]:
        rows: List[Dict] = []
        if bars is None or bars.empty:
            return rows

        for sym in symbols:
            try:
                df = (
                    bars.xs(sym, level="symbol")
                    if isinstance(bars.index, pd.MultiIndex)
                    else bars
                )
                metrics = self._metrics_for_symbol(df, sym, asset_class, config)
                if metrics:
                    rows.append(metrics)
            except KeyError:
                log.debug("Scanner: no bars returned for %s", sym)
            except Exception as exc:
                log.warning("Scanner error for %s: %s", sym, exc)
        return rows

    def scan(
        self,
        stock_bars: pd.DataFrame,
        crypto_bars: pd.DataFrame,
        config,
    ) -> List[ScanResult]:
        rows: List[Dict] = []
        rows += self._collect(stock_bars, config.scan_universe, "stock", config)
        rows += self._collect(crypto_bars, config.scan_crypto_universe, "crypto", config)

        if not rows:
            log.info("Scanner: no symbols survived filtering")
            return []

        df = pd.DataFrame(rows)

        # Percentile-rank each metric across survivors (relative, not absolute).
        # With a single survivor rank(pct=True) yields 1.0, which is correct —
        # it is trivially the best of what is available.
        df["r_volume"] = df["volume_ratio"].rank(pct=True)
        df["r_momentum"] = df["momentum_pct"].rank(pct=True)
        df["r_volatility"] = df["atr_pct"].rank(pct=True)
        df["r_trend"] = df["trend_ok"].astype(float)

        w = config.scan_weights
        total_w = sum(w.values()) or 1.0
        df["score"] = (
            w.get("volume", 0.0) * df["r_volume"]
            + w.get("momentum", 0.0) * df["r_momentum"]
            + w.get("volatility", 0.0) * df["r_volatility"]
            + w.get("trend", 0.0) * df["r_trend"]
        ) / total_w

        df.sort_values("score", ascending=False, inplace=True)

        results: List[ScanResult] = []
        for _, row in df.iterrows():
            reasons = []
            if row["volume_ratio"] >= 1.5:
                reasons.append(f"volume {row['volume_ratio']:.1f}× average")
            if row["momentum_pct"] >= 1.0:
                reasons.append(f"momentum {row['momentum_pct']:+.1f}%")
            elif row["momentum_pct"] <= -1.0:
                reasons.append(f"weakness {row['momentum_pct']:+.1f}%")
            if row["trend_ok"]:
                reasons.append("above slow SMA")

            results.append(
                ScanResult(
                    symbol=row["symbol"],
                    asset_class=row["asset_class"],
                    score=float(row["score"]),
                    price=float(row["price"]),
                    volume_ratio=float(row["volume_ratio"]),
                    momentum_pct=float(row["momentum_pct"]),
                    atr_pct=float(row["atr_pct"]),
                    dollar_volume=float(row["dollar_volume"]),
                    trend_ok=bool(row["trend_ok"]),
                    reasons=reasons or ["ranked on composite score"],
                )
            )

        log.info(
            "Scanner: %d/%d symbols passed filters, top pick %s",
            len(results),
            len(config.scan_universe) + len(config.scan_crypto_universe),
            results[0].symbol if results else "—",
        )
        return results


# ── Default universes ─────────────────────────────────────────────────────────
# Liquid, widely-held names. Override via BotConfig.scan_universe. Keep the
# list to a few hundred symbols at most — every entry costs bar data per cycle.

DEFAULT_STOCK_UNIVERSE: List[str] = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD", "NFLX",
    # Semis / hardware
    "INTC", "MU", "QCOM", "TXN", "ARM", "LRCX", "AMAT", "SMCI",
    # Software / internet
    "CRM", "ORCL", "ADBE", "NOW", "PANW", "SNOW", "UBER", "ABNB", "SHOP", "PLTR",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP", "SCHW", "COIN",
    # Healthcare
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "AMGN",
    # Consumer / industrial / energy
    "WMT", "COST", "HD", "MCD", "NKE", "SBUX", "DIS", "CAT", "BA", "GE",
    "XOM", "CVX", "COP",
    # Broad ETFs
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "SMH",
]

DEFAULT_CRYPTO_UNIVERSE: List[str] = [
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD",
    "LINK/USD", "LTC/USD", "DOT/USD", "AAVE/USD",
]
