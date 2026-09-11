"""Typing a secret into a field, without the value passing through the model.

A bot that reached a login wall had two options: give up, or ask for a full
desktop takeover so a person could type six characters. This closes that.

Almost every test here is the same assertion from a different angle — that the
value does not appear anywhere a person or a model could later read it. That is
the whole feature; the typing part is one line.
"""

from __future__ import annotations

import agent.govern as govern
import agent.policy as policy
import agent.tools as tools
from harness.audit import read as read_audit
from harness.paths import HarnessPaths

SECRET = "hunter2-do-not-leak-42"


class _Computer:
    def __init__(self) -> None:
        self.typed: list[str] = []

    def act(self, action: str, **params) -> str:
        if action == "type":
            self.typed.append(str(params.get("text") or ""))
            return "ok: typed"
        return f"ok: {action}"


class _Writer:
    def __init__(self) -> None:
        self.emitted: list[str] = []

    def __getattr__(self, name):
        def capture(*args, **kwargs):
            self.emitted.append(repr((name, args, kwargs)))
            return None

        return capture


def _ctx(tmp_path, *, computer=None, writer=None, sender="user", timeout=0.2):
    from agent.memory import Memory

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return tools.ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        computer=computer or _Computer(),
        writer=writer,
        sender=sender,
        user_input_timeout=timeout,
    )


def _store(ctx, name="GITHUB_PAT", value=SECRET):
    (ctx.paths.home / "credentials").mkdir(parents=True, exist_ok=True)
    (ctx.paths.home / "credentials" / name).write_text(value, encoding="utf-8")


# -- the value does not escape ---------------------------------------------


def test_the_secret_is_typed_but_not_returned(tmp_path):
    ctx = _ctx(tmp_path)
    _store(ctx)
    out = tools._computer_type_secret(ctx, {"name": "GITHUB_PAT"})
    assert ctx.computer.typed == [SECRET]
    assert SECRET not in out
    assert out.startswith("ok:")


def test_the_result_names_the_length_not_the_value(tmp_path):
    ctx = _ctx(tmp_path)
    _store(ctx)
    out = tools._computer_type_secret(ctx, {"name": "GITHUB_PAT"})
    assert f"{len(SECRET)} characters" in out
    assert SECRET not in out


def test_nothing_is_emitted_onto_the_stream(tmp_path):
    """A card carrying the value would put it in the transcript, which is the
    thing this exists to avoid."""
    writer = _Writer()
    ctx = _ctx(tmp_path, writer=writer)
    _store(ctx)
    tools._computer_type_secret(ctx, {"name": "GITHUB_PAT"})
    assert not any(SECRET in e for e in writer.emitted)


def test_a_computer_error_does_not_carry_the_value_back(tmp_path):
    class Failing:
        def act(self, action, **params):
            return "error: type failed"

    ctx = _ctx(tmp_path, computer=Failing())
    _store(ctx)
    assert SECRET not in tools._computer_type_secret(ctx, {"name": "GITHUB_PAT"})


def test_a_raising_computer_does_not_carry_the_value_back(tmp_path):
    from agent.gate import GateRefusal

    class Refusing:
        def act(self, action, **params):
            raise GateRefusal("GUI actions are paused while the human has control")

    ctx = _ctx(tmp_path, computer=Refusing())
    _store(ctx)
    out = tools._computer_type_secret(ctx, {"name": "GITHUB_PAT"})
    assert out.startswith("error:")
    assert SECRET not in out


# -- it is governed --------------------------------------------------------


def test_it_has_its_own_intent_separate_from_reading(tmp_path):
    """`read_secret` tells a bot whether a credential exists; this puts it in a
    login box. Somebody who wants the second confirmed every time should not
    have to confirm the first."""
    assert govern.classify("computer_type_secret", {"name": "X"})[0] == govern.INTENT_TYPE_SECRET
    assert govern.classify("get_secret", {"name": "X"})[0] == govern.INTENT_READ_SECRET


def test_a_policy_can_forbid_typing_while_still_allowing_reading():
    pol = policy.parse({"deny": [{"intent": "type_secret"}]})
    ctx = {"tool": "computer_type_secret", "intent": "type_secret", "target": "X", "bot": "a"}
    assert pol.evaluate(ctx).allowed is False
    ctx2 = {"tool": "get_secret", "intent": "read_secret", "target": "X", "bot": "a"}
    assert pol.evaluate(ctx2).allowed is True


def test_a_deployment_can_require_a_confirm_every_time():
    """openbot's exact posture, expressed as one rule."""
    pol = policy.parse({"ask": [{"intent": "type_secret"}]})
    decision = pol.evaluate(
        {"tool": "computer_type_secret", "intent": "type_secret", "target": "X", "bot": "a"}
    )
    assert decision.allowed is True
    assert decision.ask is True


def test_the_audit_row_carries_the_name_and_never_the_value(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")

    class Ctx:
        tool_call_id = "c1"

    govern.govern(Ctx(), "computer_type_secret", {"name": "GITHUB_PAT"}, paths=paths, bot="atlas")
    rows = read_audit(paths, "atlas")
    assert rows[0]["intent"] == govern.INTENT_TYPE_SECRET
    assert rows[0]["target"] == "GITHUB_PAT"
    assert SECRET not in str(rows)


# -- guards ----------------------------------------------------------------


def test_a_colleague_turn_cannot_type_secrets(tmp_path):
    """Same rule as request_secret: the user is not on the other end."""
    ctx = _ctx(tmp_path, sender="nova")
    _store(ctx)
    out = tools._computer_type_secret(ctx, {"name": "GITHUB_PAT"})
    assert out.startswith("error:")
    assert ctx.computer.typed == []


def test_no_computer_is_an_error_not_a_crash(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.computer = None
    assert tools._computer_type_secret(ctx, {"name": "X"}).startswith("error:")


def test_a_missing_name_is_an_error(tmp_path):
    assert tools._computer_type_secret(_ctx(tmp_path), {}).startswith("error:")


def test_an_unavailable_secret_does_not_type_anything(tmp_path):
    """When nothing is stored and the human never supplies it, the field must
    be left alone rather than filled with an empty string."""
    ctx = _ctx(tmp_path, timeout=0.05)
    out = tools._computer_type_secret(ctx, {"name": "NOT_STORED"})
    assert out.startswith("error:")
    assert ctx.computer.typed == []


def test_the_tool_is_registered_and_classified():
    from agent.tools import default_tools

    assert "computer_type_secret" in default_tools()
    assert "computer_type_secret" in govern.EFFECTS
