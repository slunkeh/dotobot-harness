"""Storage hardening: private state on disk, and Codex PKCE state != verifier.

Two contracts:

- `$HARNESS_HOME` holds every transcript (state.sqlite), queued message,
  live stream and staged screenshot. `HarnessPaths.ensure_layout` makes the
  home and its private subtrees 0700 and `StateStore` creates the database
  0600 (born that way, no chmod window), tightening a wider file it owns on
  open — so another local unix account cannot read the conversation store.
- The Codex OAuth `state` that rides the authorize URL and comes back on the
  plaintext localhost redirect is an independent random value, never the
  PKCE `code_verifier`; the exchange requires it and still redeems the code
  with the verifier kept server-side.
"""

from __future__ import annotations

import os
import stat
from urllib.parse import parse_qs, urlparse

import pytest

from harness.paths import HarnessPaths
from harness.statestore import StateStore
from providers import codex_oauth
from providers.codex_oauth import OAuthError, exchange, reset_sessions, start_login


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


# -- private home + state.sqlite --------------------------------------------


def test_fresh_home_and_private_subtrees_are_0700(tmp_path, umask_022):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    assert _mode(paths.home) == 0o700
    for d in (
        paths.credentials,
        paths.memory,
        paths.streams,
        paths.messages,
        paths.prompts,
        paths.run,
        paths.browser_sessions,
        paths.screenshots.parent,
    ):
        assert _mode(d) == 0o700, d


def test_existing_wide_home_is_tightened_by_ensure_layout(tmp_path, umask_022):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    paths.home.chmod(0o755)
    paths.memory.chmod(0o755)
    paths.ensure_layout(["atlas"])
    assert _mode(paths.home) == 0o700
    assert _mode(paths.memory) == 0o700


def test_fresh_state_sqlite_is_born_0600(tmp_path, umask_022, monkeypatch):
    # A post-hoc chmod leaves a window in which the file is 0644; make
    # that window permanent so only a born-private file passes.
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    store = StateStore(paths)
    store.check()
    assert _mode(paths.state_db) == 0o600
    # SQLite mirrors the main file's mode onto its WAL siblings.
    for sibling in ("state.sqlite-wal", "state.sqlite-shm"):
        p = paths.home / sibling
        if p.exists():
            assert _mode(p) & 0o077 == 0, sibling


def test_existing_wide_state_sqlite_is_tightened_on_open(tmp_path, umask_022):
    db = tmp_path / "home" / "state.sqlite"
    db.parent.mkdir(parents=True)
    StateStore(db).check()
    db.chmod(0o644)
    db.parent.chmod(0o755)
    StateStore(db).check()
    assert _mode(db) == 0o600
    assert _mode(db.parent) == 0o700


def test_state_store_still_works_after_tightening(tmp_path, umask_022):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    store = StateStore(paths)
    store.check()
    store.check()  # a second open is idempotent
    assert paths.state_db.is_file()
    assert _mode(paths.state_db) == 0o600


# -- Codex PKCE: state is not the verifier ------------------------------------


class _Transport:
    def __init__(self):
        self.posts: list[dict] = []

    def post_form(self, url, data):
        self.posts.append(dict(data))
        return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}


def _started(tmp_path):
    reset_sessions()
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    started = start_login(paths)
    url = started["verification_uri"]
    state = parse_qs(urlparse(url).query)["state"][0]
    return paths, url, state


def test_authorize_url_state_is_not_the_code_verifier(tmp_path):
    _, url, state = _started(tmp_path)
    verifier = codex_oauth._sessions["codex"]["code_verifier"]
    assert state
    assert state != verifier
    # The verifier must not ride the URL in any form.
    assert verifier not in url
    assert len(state) >= 32


def test_exchange_with_issued_state_redeems_with_stored_verifier(tmp_path):
    paths, _, state = _started(tmp_path)
    verifier = codex_oauth._sessions["codex"]["code_verifier"]
    t = _Transport()
    out = exchange(paths, "the-code", state=state, transport=t)
    assert out["status"] == "complete"
    assert t.posts[0]["code_verifier"] == verifier
    assert t.posts[0]["code_verifier"] != state
    assert (paths.credentials / "CODEX_OAUTH").is_file()


@pytest.mark.parametrize("bad", [None, "", "wrong"])
def test_exchange_without_the_issued_state_is_refused(tmp_path, bad):
    paths, _, _ = _started(tmp_path)
    t = _Transport()
    with pytest.raises(OAuthError):
        exchange(paths, "the-code", state=bad, transport=t)
    assert t.posts == []  # the code is never redeemed
    assert not (paths.credentials / "CODEX_OAUTH").exists()


def test_exchange_with_the_verifier_as_state_is_refused(tmp_path):
    """The pre-fix contract (state == verifier) must not keep working."""
    paths, _, _ = _started(tmp_path)
    verifier = codex_oauth._sessions["codex"]["code_verifier"]
    with pytest.raises(OAuthError):
        exchange(paths, "the-code", state=verifier, transport=_Transport())


def test_each_login_gets_a_fresh_state(tmp_path):
    _, _, first = _started(tmp_path)
    _, _, second = _started(tmp_path)
    assert first != second
