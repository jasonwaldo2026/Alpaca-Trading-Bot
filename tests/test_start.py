"""The one-click launcher assembles the right commands without running them."""

import start


def test_scanner_watches_the_full_session_and_uses_fmp_when_keyed():
    cmd = start.scanner_command({"FMP_API_KEY": "x"}, python="py")
    assert cmd[:3] == ["py", "-m", "scanner.cli"]
    assert "--watch" in cmd and "--extended-hours" in cmd and "--fmp-floats" in cmd


def test_scanner_skips_fmp_without_a_key():
    assert "--fmp-floats" not in start.scanner_command({}, python="py")


def test_studio_is_headless_on_a_fixed_port():
    cmd = start.studio_command(python="py", port=8501)
    assert cmd[:4] == ["py", "-m", "streamlit", "run"]
    assert cmd[4].endswith("app.py")
    assert "--server.headless" in cmd and "8501" in cmd


def test_missing_keys_are_named():
    assert start.missing_keys({}) == ["ALPACA_API_KEY", "ALPACA_API_SECRET"]
    assert start.missing_keys({"ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}) == []


def test_dry_run_runs_nothing_and_prints_the_plan(capsys, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    assert start.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "scanner:" in out and "studio:" in out and "localhost:8501" in out


def test_studio_only_and_scanner_only(capsys, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    start.main(["--dry-run", "--studio"])
    assert "scanner:" not in capsys.readouterr().out
    start.main(["--dry-run", "--scanner"])
    assert "studio:" not in capsys.readouterr().out


def test_keep_awake_is_a_no_op_off_windows():
    # On Mac/Linux caffeinate does the job; this must not raise there.
    start.keep_awake_windows(True)
    start.keep_awake_windows(False)
