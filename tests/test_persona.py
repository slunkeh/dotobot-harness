"""The owner's persona avatar (`harness/persona.py`, `harness/prefs.py`,
`GET|PATCH /api/settings`). A new account is seeded a random persona on
first read; the owner can set any valid persona; anything else is refused
with a named error. The vocabulary is mirrored by the Swift `Persona`
model — the private `test_persona_clients.py` suite keeps them in lockstep.
"""

from __future__ import annotations

import json
import random
import threading
import urllib.error
import urllib.request

import pytest

from harness import persona, prefs
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.server import make_server

# --- vocabulary ------------------------------------------------------------


def test_random_persona_is_valid_and_varies():
    seen = {persona.random_persona(random.Random(i)) for i in range(40)}
    assert len(seen) > 20
    for text in seen:
        parts = persona.parse(text)
        assert set(parts) == set(persona.FIELDS)
        assert persona.encode(parts) == text


def test_random_facial_hair_is_the_exception():
    rng = random.Random(7)
    with_hair = sum(
        persona.parse(persona.random_persona(rng))["facialHair"] != "none" for _ in range(400)
    )
    assert 20 < with_hair < 120  # ~15%


def test_parse_rejects_unknown_fields_values_and_gaps():
    good = persona.random_persona(random.Random(1))
    for bad, why in [
        ("robot", "starts with"),
        (good + ";hat=fez", "unknown persona field"),
        (good.replace("nose=", "nose=huge;nose="), "cannot be"),
        ("persona:skin=1", "missing"),
    ]:
        with pytest.raises(persona.PersonaError, match=why):
            persona.parse(bad)
    assert not persona.is_persona("")
    assert persona.is_persona(good)


def test_normalize_orders_fields_and_strips_spaces():
    text = "persona: clothing=2; skin=0;hair=bald;hairColor=6;eyes=wink;mouth=lips;facialHair=none;nose=smallRound;body=small "
    assert persona.normalize(text) == (
        "persona:skin=0;hair=bald;hairColor=6;eyes=wink;mouth=lips;"
        "facialHair=none;nose=smallRound;body=small;clothing=2"
    )


def test_photo_avatar_is_a_bare_upload_basename():
    assert persona.photo_name("photo:ab12cd34-me.jpg") == "ab12cd34-me.jpg"
    assert persona.normalize_avatar(" photo:ab12cd34-me.jpg ") == "photo:ab12cd34-me.jpg"
    assert persona.is_avatar("photo:ab12cd34-me.jpg")
    for bad in ("photo:", "photo:../etc/passwd", "photo:a/b.jpg", "photo:.hidden", "photo:a b"):
        assert persona.photo_name(bad) is None, bad
        assert not persona.is_avatar(bad), bad
        with pytest.raises(persona.PersonaError):
            persona.normalize_avatar(bad)


# --- prefs -----------------------------------------------------------------


def test_first_read_seeds_a_random_persona_and_persists_it(tmp_path):
    paths = HarnessPaths(tmp_path / "home")
    first = prefs.user_avatar(paths)
    assert persona.is_persona(first)
    assert prefs.user_avatar(paths) == first
    assert json.loads((tmp_path / "home" / "settings.json").read_text())["user_avatar"] == first
    assert prefs.account_prefs(paths)["user_avatar"] == first


def test_set_user_avatar_keeps_the_llm_defaults(tmp_path):
    paths = HarnessPaths(tmp_path / "home")
    prefs.set_llm_defaults(paths, provider="grok", model="grok-4")
    chosen = persona.random_persona(random.Random(3))
    assert prefs.set_user_avatar(paths, chosen) == chosen
    out = prefs.account_prefs(paths)
    assert out["user_avatar"] == chosen
    assert out["default_provider"] == "grok"
    with pytest.raises(persona.PersonaError):
        prefs.set_user_avatar(paths, "persona:hair=fez")
    assert prefs.user_avatar(paths) == chosen
    # A photo replaces the persona and survives a re-read; a bad one is refused.
    assert prefs.set_user_avatar(paths, "photo:ab12cd34-me.jpg") == "photo:ab12cd34-me.jpg"
    assert prefs.user_avatar(paths) == "photo:ab12cd34-me.jpg"
    with pytest.raises(persona.PersonaError):
        prefs.set_user_avatar(paths, "photo:../me.jpg")
    assert prefs.user_avatar(paths) == "photo:ab12cd34-me.jpg"


# --- API -------------------------------------------------------------------


ROSTER = """
[[bots]]
name = "atlas"
provider = "echo"
"""


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        orch.down()


def _req(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def test_settings_api_seeds_then_stores_the_persona(server):
    status, first = _req(f"{server}/api/settings")
    assert status == 200 and persona.is_persona(first["user_avatar"])
    status, again = _req(f"{server}/api/settings")
    assert again["user_avatar"] == first["user_avatar"]

    chosen = persona.random_persona(random.Random(11))
    status, patched = _req(f"{server}/api/settings", "PATCH", {"user_avatar": chosen})
    assert status == 200 and patched["user_avatar"] == chosen
    assert "default_provider" in patched

    status, err = _req(f"{server}/api/settings", "PATCH", {"user_avatar": "persona:hair=fez"})
    assert status == 400 and "persona" in err["error"]
    assert _req(f"{server}/api/settings")[1]["user_avatar"] == chosen

    # A PATCH of only the LLM default leaves the persona alone.
    status, out = _req(f"{server}/api/settings", "PATCH", {"default_provider": "grok"})
    assert status == 200 and out["user_avatar"] == chosen and out["default_provider"] == "grok"

    # A photo: upload the bytes, then point the avatar at the stored basename.
    req = urllib.request.Request(
        f"{server}/api/upload",
        data=b"\x89PNG fake",
        method="POST",
        headers={"Content-Type": "application/octet-stream", "X-Filename": "me.png"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        stored = json.loads(r.read().decode())["path"].rsplit("/", 1)[-1]
    status, out = _req(f"{server}/api/settings", "PATCH", {"user_avatar": f"photo:{stored}"})
    assert status == 200 and out["user_avatar"] == f"photo:{stored}"
    status, _ = _req(f"{server}/api/settings", "PATCH", {"user_avatar": "photo:../x.png"})
    assert status == 400
