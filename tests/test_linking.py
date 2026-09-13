import json
import threading
import urllib.error
import urllib.request

import pytest

from harness.linking import decode_link_code, get_or_create_key, link_code
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.server import make_server

ROSTER = '[[bots]]\nname = "atlas"\nprovider = "echo"\n'


def test_key_is_stable_and_rotatable(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    k1 = get_or_create_key(paths)
    k2 = get_or_create_key(paths)
    assert k1 == k2 and len(k1) > 20
    k3 = get_or_create_key(paths, rotate=True)
    assert k3 != k1


def test_link_code_roundtrip():
    code = link_code("http://192.168.1.5:8765", "abc123")
    assert code.startswith("dotobot_")
    decoded = decode_link_code(code)
    assert decoded == {"url": "http://192.168.1.5:8765", "key": "abc123"}


def test_server_requires_linking_key(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    key = get_or_create_key(orch.paths)
    httpd = make_server(orch, "127.0.0.1", 0, key)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # no key -> 401
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5)
        assert exc.value.code == 401

        # with the bearer header -> ok
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/health", headers={"Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read().decode())["ok"] is True

        # ?token= is for the WebSocket upgrade only (see
        # tests/test_security_hardening.py); on a REST route it is refused so
        # the linking key never travels in a URL that proxies and shells log.
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health?token={key}", timeout=5)
        assert exc.value.code == 401
    finally:
        httpd.shutdown()
        orch.down()


def test_public_url_precedence_and_persisted_install_setting(tmp_path, monkeypatch):
    from harness.linking import advertised_url

    monkeypatch.delenv("HARNESS_PUBLIC_URL", raising=False)
    assert advertised_url("127.0.0.1", 8765, home=tmp_path) == "http://127.0.0.1:8765"
    (tmp_path / "public-url").write_text("https://saved.example.com/\n")
    assert advertised_url("127.0.0.1", 8765, home=tmp_path) == "https://saved.example.com"
    monkeypatch.setenv("HARNESS_PUBLIC_URL", "https://env.example.com")
    assert advertised_url("127.0.0.1", 8765, home=tmp_path) == "https://env.example.com"
    assert (
        advertised_url("127.0.0.1", 8765, "https://arg.example.com/", home=tmp_path)
        == "https://arg.example.com"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://public.example.com",
        "https://user:secret@example.com",
        "https://x.example/path",
        "https://x.example/?token=secret",
        "https://x.example/#token",
        "https://x.example:70000",
        "https://x.example\n",
        "https://x.example\\bad",
    ],
)
def test_invalid_public_url_is_rejected_without_fallback(value, monkeypatch):
    from harness.linking import advertised_url

    monkeypatch.setenv("HARNESS_PUBLIC_URL", value)
    with pytest.raises(ValueError):
        advertised_url("127.0.0.1", 8765)


def test_link_command_uses_installed_url_and_original_key(tmp_path, capsys, monkeypatch):
    from argparse import Namespace

    from harness.cli import cmd_link

    monkeypatch.delenv("HARNESS_PUBLIC_URL", raising=False)
    (tmp_path / "public-url").write_text("https://owned.example.com\n")
    paths = HarnessPaths.resolve(tmp_path)
    key = get_or_create_key(paths)
    assert (
        cmd_link(
            Namespace(
                home=str(tmp_path), host="127.0.0.1", port=8765, rotate=False, public_url=None
            )
        )
        == 0
    )
    code = capsys.readouterr().out.splitlines()[0].split()[-1]
    assert decode_link_code(code) == {"url": "https://owned.example.com", "key": key}


def test_server_public_url_does_not_replace_loopback_tools_address(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_PUBLIC_URL", raising=False)
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER)
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    server = make_server(orch, "127.0.0.1", 0, "secret", public_url="https://owned.example.com")
    try:
        assert server.public_url == "https://owned.example.com"
        local = json.loads((orch.paths.home / "serve.json").read_text())
        assert local["url"] == f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.server_close()
        orch.down()


def test_dotobot_code_and_legacy_code_decode_to_same_connection():
    import base64

    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"url": "https://bots.example.com", "key": "test-key"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    expected = {"url": "https://bots.example.com", "key": "test-key"}
    for prefix in ("dotobot_", "harness_", ""):
        assert decode_link_code(prefix + payload) == expected
    assert link_code(**expected).startswith("dotobot_")


def test_both_branded_and_legacy_codes_are_scrubbed():
    from harness.redaction import scrub

    code = link_code("https://bots.example.com", "redaction-test-key")
    legacy = "harness_" + code.removeprefix("dotobot_")
    assert code not in scrub(code)
    assert legacy not in scrub(legacy)
