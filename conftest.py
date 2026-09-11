"""Public runtime suite, independent of the private monorepo's test floor."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
MINIMUM_TESTS = 3046


def pytest_collection_modifyitems(config, items):
    if config.option.keyword or config.option.markexpr or config.option.collectonly:
        return
    if set(config.args or []) not in ({"tests"}, {str(config.rootpath)}):
        return
    if len(items) < MINIMUM_TESTS:
        raise pytest.UsageError(
            f"Public runtime suite collected {len(items)} tests; expected at least {MINIMUM_TESTS}."
        )
