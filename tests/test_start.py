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


def test_tailscale_addresses_are_the_cgnat_range_only():
    assert start.is_tailscale_address("100.101.102.103")
    assert start.is_tailscale_address("100.64.0.1")
    assert start.is_tailscale_address("100.127.255.254")
    assert not start.is_tailscale_address("100.128.0.1")
    assert not start.is_tailscale_address("192.168.1.23")
    assert not start.is_tailscale_address("10.0.0.5")


def test_tailscale_ip_is_found_from_interfaces_without_the_cli(monkeypatch):
    monkeypatch.setattr(start.socket, "getaddrinfo", lambda *a, **k: [
        (None, None, None, None, ("192.168.1.23", 0)),
        (None, None, None, None, ("100.90.1.2", 0)),
    ])
    assert start.tailscale_ip() == "100.90.1.2"


def test_missing_tailscale_is_explained_not_silent(monkeypatch):
    monkeypatch.setattr(start, "tailscale_ip", lambda: None)
    urls = start.studio_urls(8501)
    assert any("localhost:8501" in u for u in urls)
    assert any("Tailscale not detected" in u for u in urls)


def test_update_is_skipped_cleanly_without_git(monkeypatch):
    monkeypatch.setattr(start, "git_executable", lambda: None)
    assert start.self_update().startswith("Update skipped")


def test_update_pulls_fast_forward_only_and_reports(monkeypatch):
    calls = []

    class R:
        def __init__(self, out="", code=0):
            self.stdout, self.stderr, self.returncode = out, "", code

    heads = iter(["aaa", "bbb"])

    def fake_run(cmd, timeout=90):
        calls.append(cmd)
        if cmd[1:] == ["rev-parse", "HEAD"]:
            return R(next(heads) + "\n")
        if cmd[1:] == ["pull", "--ff-only"]:
            return R("Updating aaa..bbb\n")
        if cmd[1:3] == ["diff", "--name-only"]:
            return R("start.py\nREADME.md\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(start, "git_executable", lambda: "git")
    monkeypatch.setattr(start, "_run", fake_run)
    note = start.self_update(python="py")
    assert ["git", "pull", "--ff-only"] in calls
    assert "2 file(s) changed" in note
    assert "Requirements reinstalled" not in note


def test_update_reinstalls_when_requirements_change(monkeypatch):
    calls = []

    class R:
        def __init__(self, out="", code=0):
            self.stdout, self.stderr, self.returncode = out, "", code

    heads = iter(["aaa", "bbb"])

    def fake_run(cmd, timeout=90):
        calls.append(cmd)
        if cmd[1:] == ["rev-parse", "HEAD"]:
            return R(next(heads))
        if cmd[1:] == ["pull", "--ff-only"]:
            return R("ok")
        if cmd[1:3] == ["diff", "--name-only"]:
            return R("requirements.txt")
        if cmd[1:4] == ["-m", "pip", "install"]:
            return R("done")
        raise AssertionError(cmd)

    monkeypatch.setattr(start, "git_executable", lambda: "git")
    monkeypatch.setattr(start, "_run", fake_run)
    assert "Requirements reinstalled" in start.self_update(python="py")


def test_a_diverged_local_copy_is_not_clobbered(monkeypatch):
    class R:
        def __init__(self, out="", err="", code=0):
            self.stdout, self.stderr, self.returncode = out, err, code

    def fake_run(cmd, timeout=90):
        if cmd[1:] == ["rev-parse", "HEAD"]:
            return R("aaa")
        if cmd[1:] == ["pull", "--ff-only"]:
            return R(err="fatal: Not possible to fast-forward, aborting.", code=1)
        raise AssertionError(cmd)

    monkeypatch.setattr(start, "git_executable", lambda: "git")
    monkeypatch.setattr(start, "_run", fake_run)
    note = start.self_update()
    assert note.startswith("Update skipped") and "already here" in note


def test_a_studio_already_running_is_reported_not_crashed(capsys, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    monkeypatch.setattr(start, "port_in_use", lambda port: True)
    monkeypatch.setattr(start, "self_update", lambda: "Already up to date.")
    assert start.main(["--studio"]) == 0
    assert "already running" in capsys.readouterr().out
