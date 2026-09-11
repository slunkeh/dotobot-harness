"""Connected GitHub accounts can read the source files needed for bot setup."""

import base64

import pytest

from connectors import github
from connectors.base import ConnectorContext
from harness.connectors import Connectors
from harness.paths import HarnessPaths


def context(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    record = Connectors(paths).add("github", "GitHub", secret="test-private-token")
    return ConnectorContext(paths=paths, bot="atlas", record=record)


def tool():
    return next(t for t in github.tools() if t.spec.name == "github_get_file")


def test_read_repo_file_and_continue_without_losing_text(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    text = "a" * 8000 + "\nprint size: A3 ✓\n"
    calls = []

    def request(token, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {
            "type": "file",
            "encoding": "base64",
            "sha": "file-sha",
            "content": base64.b64encode(text.encode()).decode(),
        }

    monkeypatch.setattr(github, "_request", request)
    first = tool().handler(
        ctx, {"repo": "example-owner/routines", "path": "src/order names.py", "ref": "stable"}
    )
    second = tool().handler(
        ctx,
        {
            "repo": "example-owner/routines",
            "path": "src/order names.py",
            "ref": "stable",
            "offset": 8000,
        },
    )
    assert "next_offset=8000" in first
    assert "print size: A3 ✓" in second
    assert "test-private-token" not in first + second
    assert calls[0] == (
        "GET",
        "/repos/example-owner/routines/contents/src/order%20names.py",
        {"query": {"ref": "stable"}},
    )


def test_directory_listing_exposes_a_continuation(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    monkeypatch.setattr(
        github,
        "_request",
        lambda *a, **kw: [
            {"type": "file", "path": f"script-{n}.py", "sha": str(n)} for n in range(51)
        ],
    )
    out = tool().handler(ctx, {"repo": "example-owner/routines"})
    assert "next_offset=50" in out
    assert "script-49.py" in out
    assert "script-50.py" not in out
    assert "script-50.py" in tool().handler(ctx, {"repo": "example-owner/routines", "offset": 50})


@pytest.mark.parametrize("path", ["../secrets", "/etc/passwd", "src/../../bad", "a\x00b"])
def test_path_escape_is_rejected_before_network(tmp_path, monkeypatch, path):
    ctx = context(tmp_path)
    monkeypatch.setattr(github, "_request", lambda *a, **kw: pytest.fail("must not request"))
    assert (
        tool().handler(ctx, {"repo": "example-owner/routines", "path": path}).startswith("error:")
    )


def test_binary_or_large_file_is_explicitly_unavailable(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    for response in [
        {"type": "file", "encoding": "none", "content": ""},
        {"type": "file", "encoding": "base64", "content": "/w=="},
    ]:
        monkeypatch.setattr(github, "_request", lambda *a, response=response, **kw: response)
        assert (
            tool()
            .handler(ctx, {"repo": "example-owner/routines", "path": "source.bin"})
            .startswith("error:")
        )


def test_long_directory_names_continue_before_tool_output_limit(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    rows = [{"type": "file", "path": f"{n:02d}-" + "a" * 1000} for n in range(20)]
    monkeypatch.setattr(github, "_request", lambda *a, **kw: rows)
    first = tool().handler(ctx, {"repo": "example-owner/routines"})
    offset = int(first.split("next_offset=")[1].splitlines()[0])
    assert 0 < offset < 20
    assert len(first) < 9000
    assert rows[offset - 1]["path"] in first
    assert rows[offset]["path"] not in first
    second = tool().handler(ctx, {"repo": "example-owner/routines", "offset": offset})
    assert rows[offset]["path"] in second
