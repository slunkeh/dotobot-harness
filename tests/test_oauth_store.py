"""Regression tests for providers.oauth_store — shared OAuth token persistence.

Every provider PKCE/device-code flow funnels through these helpers for the
on-disk shape: `$HARNESS_HOME/credentials/<SECRET>` as JSON, chmod 600, never
logged. A regression here breaks sign-in for every OAuth provider at once.
"""

import base64
import hashlib
import json
import stat
import time

from harness.paths import HarnessPaths
from providers.oauth_store import (
    clear_tokens,
    cred_path,
    load_tokens,
    pkce_pair,
    save_tokens,
)


def _paths(tmp_path):
    return HarnessPaths(home=tmp_path / "home")


def test_pkce_pair_is_rfc7636_s256():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 96  # RFC 7636 bounds
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert challenge == expected
    # each call is fresh entropy
    assert pkce_pair()[0] != verifier


def test_save_load_roundtrip_with_ttl(tmp_path):
    paths = _paths(tmp_path)
    before = time.time()
    save_tokens(paths, "PROVIDER_X", {"access_token": "tok", "expires_in": 100})
    data = load_tokens(paths, "PROVIDER_X")
    assert data["access_token"] == "tok"
    assert data["refresh_token"] == ""
    assert data["token_type"] == "Bearer"
    assert before + 90 <= data["expires_at"] <= time.time() + 110


def test_tokens_are_private_on_disk(tmp_path):
    paths = _paths(tmp_path)
    save_tokens(paths, "PROVIDER_X", {"access_token": "tok"})
    path = cred_path(paths, "PROVIDER_X")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.credentials.stat().st_mode) == 0o700


def test_explicit_expires_at_and_extra_fields_win(tmp_path):
    paths = _paths(tmp_path)
    save_tokens(
        paths,
        "PROVIDER_X",
        {"access_token": "tok", "expires_at": 12345.0, "refresh_token": " r1 "},
        extra={"account": "me@example.com"},
    )
    data = load_tokens(paths, "PROVIDER_X")
    assert data["expires_at"] == 12345.0
    assert data["refresh_token"] == "r1"
    assert data["account"] == "me@example.com"


def test_garbage_expires_in_falls_back_to_default_ttl(tmp_path):
    paths = _paths(tmp_path)
    before = time.time()
    save_tokens(paths, "PROVIDER_X", {"access_token": "tok", "expires_in": "soon"})
    data = load_tokens(paths, "PROVIDER_X")
    assert data["expires_at"] >= before + 21000  # default_ttl=21600, minus slack


def test_load_is_none_for_missing_or_corrupt_files(tmp_path):
    paths = _paths(tmp_path)
    assert load_tokens(paths, "NOPE") is None
    paths.credentials.mkdir(parents=True, exist_ok=True)
    cred_path(paths, "BAD").write_text("{not json", encoding="utf-8")
    assert load_tokens(paths, "BAD") is None
    cred_path(paths, "LIST").write_text(json.dumps([1, 2]), encoding="utf-8")
    assert load_tokens(paths, "LIST") is None


def test_clear_tokens_reports_whether_anything_was_removed(tmp_path):
    paths = _paths(tmp_path)
    save_tokens(paths, "PROVIDER_X", {"access_token": "tok"})
    assert clear_tokens(paths, "PROVIDER_X") is True
    assert load_tokens(paths, "PROVIDER_X") is None
    assert clear_tokens(paths, "PROVIDER_X") is False
