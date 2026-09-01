"""
Architectural guardrails.

These are the invariants from CLAUDE.md, enforced rather than merely
documented — a comment does not survive a refactor, a failing test does.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
APP_PACKAGES = {"bot", "scanner", "studio", "dashboard", "trading_bot"}


def _imported_modules(path: Path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


CORE_FILES = sorted((REPO / "core").glob("*.py"))


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_core_does_not_import_apps(path):
    """core/ is the base of the dependency graph. If it imports an app, the
    apps can no longer be developed or tested independently."""
    offenders = [
        mod for mod in _imported_modules(path)
        if mod.split(".")[0] in APP_PACKAGES
    ]
    assert not offenders, (
        f"core/{path.name} imports {offenders} — core must not depend on apps."
    )


def test_core_files_were_actually_found():
    """Guards against the parametrized test above silently passing on an
    empty file list."""
    assert len(CORE_FILES) >= 5


@pytest.mark.parametrize(
    "path",
    sorted(
        p for p in REPO.rglob("*.py")
        if p.parts[len(REPO.parts)] in {"bot", "scanner", "studio"}
        or p.name in {"dashboard.py", "trading_bot.py"}
    ),
    ids=lambda p: str(p.relative_to(REPO)),
)
def test_apps_do_not_reimplement_indicator_math(path):
    """
    Indicator math belongs in core/indicators.py only.

    `.rolling(...).mean()` and `.ewm(...)` outside core are how the RSI
    duplication crept in originally; catching it here keeps the bot's
    signals and the dashboard's chart in agreement by construction.
    """
    source = path.read_text()
    assert ".ewm(" not in source, (
        f"{path.name} uses .ewm() directly — use core.indicators instead."
    )
    assert ".rolling(" not in source, (
        f"{path.name} uses .rolling() directly — use core.indicators instead."
    )


def test_nothing_flips_paper_trading_off():
    """paper=False would point this repo at a live brokerage account."""
    offenders = []
    for path in REPO.rglob("*.py"):
        if ".git" in path.parts or path.parts[len(REPO.parts)] == "tests":
            continue
        if "paper=False" in path.read_text():
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"paper=False found in {offenders}"
