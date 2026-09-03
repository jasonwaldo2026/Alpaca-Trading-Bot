"""
Shared core for every app in this repo.

  core.client      — Credentials + AlpacaClient
  core.data        — MarketDataFetcher, bar normalization
  core.indicators  — SMA / RSI / ATR — the single source of truth
  core.universe    — symbol lists and stock-vs-crypto routing
  core.rules       — scan rule model shared by Studio and Scanner

Nothing in core imports from bot/, dashboard/, scanner/, or studio/.
The dependency arrow points one way; keep it that way.
"""
