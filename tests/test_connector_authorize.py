"""Regression tests for connectors.authorize — the in-chat Authorize card.

When an OAuth connector is configured but not yet connected, the bot's only
tool is `<type>_connect`, which drops a sign-in card in chat and tells the
model to wait — never to send the user to Settings.
"""

from connectors.authorize import card_payload, connect_stub, emit_sign_in_card
from connectors.base import ConnectorContext
from harness.paths import HarnessPaths


def _ctx(tmp_path, record, emit_card=None):
    return ConnectorContext(
        paths=HarnessPaths(home=tmp_path / "home"),
        bot="atlas",
        record=record,
        emit_card=emit_card,
    )


def test_card_payload_pulls_catalog_metadata():
    payload = card_payload({"id": "c1", "type": "linear", "name": "Linear"})
    assert payload["title"] == "Linear"
    assert payload["type"] == "linear"
    assert payload["connector_id"] == "c1"
    assert payload["description"]  # catalog copy, not empty
    assert payload["icon"]


def test_card_payload_survives_an_unknown_type():
    payload = card_payload({"id": "c2", "type": "notaservice"})
    assert payload["title"] == "notaservice"
    assert payload["description"] == ""
    assert payload["icon"] is None


def test_google_card_copy_uses_the_actual_service():
    gmail = card_payload({"id": "g1", "type": "gmail", "name": "Personal"})
    drive = card_payload({"id": "g2", "type": "google_drive", "name": "Work"})
    assert "email" in gmail["description"].lower()
    assert "google drive" in drive["description"].lower()
    assert drive["title"] == "Work"


def test_connect_stub_emits_the_card_and_tells_the_bot_to_wait(tmp_path):
    emitted = []

    def emit(card_type, payload, card_id=None):
        emitted.append((card_type, payload))
        return "card-1"

    tool = connect_stub("linear", "Linear")
    assert tool.spec.name == "linear_connect"
    ctx = _ctx(tmp_path, {"id": "c1", "type": "linear", "name": "Linear"}, emit_card=emit)
    result = tool.handler(ctx, {})
    assert emitted and emitted[0][0] == "connector"
    assert emitted[0][1]["connector_id"] == "c1"
    assert result.startswith("ok:")
    assert "Authorize" in result and "Settings" in result


def test_connect_stub_is_a_noop_card_without_a_live_stream(tmp_path):
    # Headless runs have no stream; the tool must not crash and the card
    # emit resolves to None.
    ctx = _ctx(tmp_path, {"id": "c1", "type": "linear", "name": "Linear"})
    assert emit_sign_in_card(ctx) is None
    tool = connect_stub("linear", "Linear")
    assert tool.handler(ctx, {}).startswith("ok:")
