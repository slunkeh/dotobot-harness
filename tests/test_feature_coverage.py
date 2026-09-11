"""Every runtime module must be exercised by at least one test.

A feature that lands without a regression test is invisible to CI: it works
on the day it merges and nothing goes red when a later change breaks it.
This guard walks the runtime packages and fails when a module is never
referenced by any test file — so a new feature either ships with its
regression test, or is exempted HERE with a written-down reason that a
reviewer will see.

This is the mechanical half of the policy in AGENTS.md / CLAUDE.md
("every feature ships with regression tests"). It proves a module is
touched by the suite, not that the tests are good — that part is still on
the author and the reviewer.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

# The runtime packages (mirrors pyproject's [tool.setuptools] packages).
PACKAGES = ("agent", "channels", "connectors", "harness", "isolation", "providers")

# Modules allowed to have no direct test, each with the reason. Keep this
# empty unless a module truly cannot be tested here; an entry without a
# real reason is a review flag, and test_exemptions_are_honest fails when
# an exempted module disappears or the reason is blank.
EXEMPT: dict[str, str] = {}


def _runtime_modules() -> list[str]:
    mods = []
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).glob("*.py")):
            if path.stem.startswith("__"):  # __init__ / __main__
                continue
            mods.append(f"{pkg}.{path.stem}")
    return mods


def _test_corpus() -> str:
    # Everything under tests/ except this guard, which must not satisfy
    # itself by naming modules in its own exemption table.
    me = Path(__file__).name
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(TESTS.glob("*.py")) if p.name != me
    )


def _is_referenced(module: str, corpus: str) -> bool:
    pkg, name = module.split(".", 1)
    patterns = (
        rf"from {pkg}\.{name} import",
        rf"from {pkg} import [^\n]*\b{name}\b",
        rf"import {pkg}\.{name}\b",
        rf"\b{pkg}\.{name}\b",
    )
    return any(re.search(p, corpus) for p in patterns)


def test_every_runtime_module_is_covered_by_some_test():
    corpus = _test_corpus()
    missing = [
        mod for mod in _runtime_modules() if mod not in EXEMPT and not _is_referenced(mod, corpus)
    ]
    assert not missing, (
        "Runtime modules with no test coverage at all:\n  "
        + "\n  ".join(missing)
        + "\n\nEvery feature ships with regression tests (see AGENTS.md). Add a\n"
        "tests/test_<module>.py exercising the module's contract, or — only if\n"
        "it genuinely cannot be tested here — add it to EXEMPT in\n"
        "tests/test_feature_coverage.py with the reason."
    )


def test_exemptions_are_honest():
    for mod, reason in EXEMPT.items():
        assert reason.strip(), f"{mod}: exemption needs a reason"
        pkg, name = mod.split(".", 1)
        assert (ROOT / pkg / f"{name}.py").is_file(), (
            f"{mod}: exempted module no longer exists — remove the entry"
        )


def test_every_runtime_module_imports_cleanly():
    # A module nothing imports can carry a syntax error or a third-party
    # import to main unnoticed; the stdlib-only CI job imports a curated
    # list, this imports everything.
    for mod in _runtime_modules():
        importlib.import_module(mod)
