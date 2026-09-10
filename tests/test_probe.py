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


# ── Freshness ────────────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def test_fmp_stamps_are_read_as_eastern():
    stamp = probe.parse_fmp_stamp("2026-09-04 15:55:00")
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() == ET.utcoffset(datetime(2026, 9, 4, 15, 55))
    assert probe.parse_fmp_stamp("nonsense") is None
    assert probe.parse_fmp_stamp(None) is None


def test_a_just_closed_bar_reads_as_current_not_late():
    """A bar is stamped with its start, so a 5-minute bar that closed this
    second is already 5 minutes old. That is arithmetic, not lag."""
    now = datetime(2026, 9, 4, 19, 0, tzinfo=timezone.utc)
    just_closed = now - timedelta(minutes=5)
    line = probe.describe_age(just_closed, 5, now)
    assert "no lag beyond the bar itself" in line
    assert "5 min old" in line


def test_a_provider_running_behind_is_named_as_such():
    now = datetime(2026, 9, 4, 19, 0, tzinfo=timezone.utc)
    line = probe.describe_age(now - timedelta(minutes=8), 5, now)
    assert "3 min behind the bar close" in line

    late = probe.describe_age(now - timedelta(minutes=17), 5, now)
    assert "over one whole bar late" in late


def test_no_bars_is_said_plainly():
    assert probe.describe_age(None, 5) == "no bars returned"


def test_alpaca_freshness_reports_missing_credentials_rather_than_raising(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    rows = probe.alpaca_freshness()
    assert len(rows) == 1
    assert "not set in .env" in rows[0][1]


def test_fmp_freshness_reports_a_refusal_per_interval(monkeypatch):
    monkeypatch.setattr(probe, "fetch", lambda *a, **k: (None, "refused (HTTP 402, not in your plan)"))
    rows = probe.fmp_freshness("k")
    assert len(rows) == 4
    assert all("refused" in line for _, line in rows)


def test_fmp_freshness_takes_the_newest_stamp(monkeypatch):
    rows_out = [{"date": "2026-09-04 09:35:00"}, {"date": "2026-09-04 15:55:00"},
                {"date": "2026-09-04 12:00:00"}]
    monkeypatch.setattr(probe, "fetch", lambda *a, **k: (rows_out, "ok"))
    label, line = probe.fmp_freshness("k")[0]
    assert label == "FMP 5-minute"
    assert "15:55" in line or "min old" in line
