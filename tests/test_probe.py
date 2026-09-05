"""The FMP capability check reports shapes without a network."""

import io
import urllib.error
import urllib.request

from scanner import probe


def test_pre_market_bars_are_detected():
    rows = [{"date": "2026-09-04 08:05:00"}, {"date": "2026-09-04 09:30:00"},
            {"date": "2026-09-04 16:05:00"}]
    line = probe.describe("5-minute bars", rows)
    assert "pre-market bars: 1" in line and "after-hours bars: 1" in line
    assert "INCLUDED" in line


def test_regular_hours_only_is_said_plainly():
    rows = [{"date": "2026-09-04 09:35:00"}, {"date": "2026-09-04 15:55:00"}]
    assert "regular hours only" in probe.describe("5-minute bars", rows)


def test_bulk_float_counts_rows_with_a_figure():
    rows = [{"symbol": "A", "floatShares": 1}, {"symbol": "B", "floatShares": None}]
    assert probe.describe("Bulk float, page 0", rows) == "2 rows, 1 with a float figure"


def test_fmp_json_error_body_is_reported_as_refused():
    assert probe.describe("Quote", {"Error Message": "Premium endpoint"}).startswith("refused")


def test_http_refusal_is_explained_and_key_scrubbed(monkeypatch):
    def refuse(url, timeout=None):
        raise urllib.error.HTTPError(url, 402, "Payment Required", {}, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    data, status = probe.fetch("quote", {"symbol": "AAPL"}, key="SECRET")
    assert data is None and "not in your plan" in status and "SECRET" not in status


def test_network_error_never_leaks_the_key(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.URLError("dns failed for ...apikey=SECRET...")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    _, status = probe.fetch("quote", {}, key="SECRET")
    assert "SECRET" not in status
