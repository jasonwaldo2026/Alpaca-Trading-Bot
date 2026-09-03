"""
Studio must start the way the README says to start it.

`streamlit run studio/app.py` puts studio/ on sys.path, not the repository
root, so without a path shim at the top of app.py `import core` fails and
the page never renders — on every browser, but first noticed on a phone,
where a blank Streamlit shell is all you see. This test reproduces that
sys.path condition in-process.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "studio" / "app.py"


@pytest.fixture
def without_repo_root_on_path(monkeypatch):
    """The interpreter state `streamlit run` produces: the script's own
    directory on sys.path, the repository root absent, nothing from the
    project imported yet."""
    stripped = [p for p in sys.path if Path(p or ".").resolve() != ROOT]
    monkeypatch.setattr(sys, "path", [str(APP.parent)] + stripped)
    for name in list(sys.modules):
        if name == "core" or name.startswith("core.") or name == "studio" or name.startswith("studio."):
            monkeypatch.delitem(sys.modules, name)
    yield


def test_studio_starts_without_the_repo_root_on_sys_path(without_repo_root_on_path):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]


def test_studio_rules_and_floats_paths_do_not_depend_on_cwd(monkeypatch, tmp_path):
    """Launched from another directory, Studio must still find rules/."""
    monkeypatch.chdir(tmp_path)
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    options = [s.options for s in at.selectbox if s.label == "Rule file"]
    assert options and "strong-above-vwap.json" in options[0]
