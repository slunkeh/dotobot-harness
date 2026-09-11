"""Regression tests for channels.desktop — the Tkinter desktop channel.

The GUI itself is manual-test territory (all logic lives in the unit-tested
`channels/viewmodel.py`), but two contracts are testable headless: the module
imports without tkinter present, and a missing tkinter fails with the message
that names the fix (`python3-tk`), not a bare ImportError.
"""

import sys

import pytest


def test_module_imports_headless():
    # Importing the channel must not import tkinter (deferred to _require_tk),
    # so `harness channels` and the CLI work on servers with no python3-tk.
    import channels.desktop  # noqa: F401


def test_require_tk_names_the_missing_system_package(monkeypatch):
    from channels.desktop import _require_tk

    # sys.modules[name] = None makes `import tkinter` raise ImportError.
    monkeypatch.setitem(sys.modules, "tkinter", None)
    with pytest.raises(RuntimeError, match="python3-tk"):
        _require_tk()


def test_require_tk_returns_the_toolkit_when_present():
    # exc_type=ImportError: a Python built with the tkinter package but no
    # libtk on the box (CI runners) raises plain ImportError, not
    # ModuleNotFoundError — that box is a "not present" box too.
    pytest.importorskip("tkinter", exc_type=ImportError)
    from channels.desktop import _require_tk

    tk, scrolledtext, simpledialog, messagebox = _require_tk()
    assert hasattr(tk, "Tk")
