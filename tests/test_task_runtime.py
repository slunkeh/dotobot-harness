"""Saved task state drives actual model/tool turns and context admission."""

from agent import messaging, runtime
from agent.context import ConnectorCatalogue, schema_tokens
from agent.runtime import build_agent
from agent.tools import Tool
from harness.connectors import Connectors
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.taskscope import read_task
from providers.base import Completion, Message, Provider, ToolCall, ToolSpec


def make_agent(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0), paths


def test_runtime_continuation_reuses_only_user_selected_connectors(tmp_path, monkeypatch):
    agent, paths = make_agent(tmp_path)
    records = [{"id": "n", "type": "notion", "name": "Notion", "oauth_configured": True}]
    monkeypatch.setattr(Connectors, "list", lambda self: records)
    monkeypatch.setattr(Connectors, "records", lambda self: records)
    fetched = []

    def fetch(*a, record_ids=None, **kw):
        fetched.append(record_ids)
        return {"notion_search": Tool(ToolSpec("notion_search", "Search"), lambda c, a: "ok")}

    monkeypatch.setattr(runtime, "connector_tools", fetch)
    agent._produce("user", "Use Notion to find the brief", turn_id="first")
    first = read_task(paths, "atlas", "peer:user")
    restarted = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    restarted._produce("user", "continue", turn_id="second")
    assert read_task(paths, "atlas", "peer:user")["task_id"] == first["task_id"]
    restarted._produce("user", "Explain photosynthesis", turn_id="third")
    assert fetched == [{"n"}, {"n"}]
    assert read_task(paths, "atlas", "peer:user")["connector_ids"] == []


def test_new_user_correction_prevents_remaining_batch_actions(tmp_path, monkeypatch):
    agent, paths = make_agent(tmp_path)
    called = []

    def observe(ctx, args):
        messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="Do not send anything."))
        return "Observation finished"

    defaults = dict(runtime.default_tools())
    defaults["computer_screenshot"] = Tool(defaults["computer_screenshot"].spec, observe)
    defaults["message_agent"] = Tool(
        defaults["message_agent"].spec, lambda c, a: called.append(a) or "ok"
    )
    monkeypatch.setattr(runtime, "default_tools", lambda: defaults)

    class Script(Provider):
        n = 0

        def complete(self, messages, **kw):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[
                        ToolCall("observe", "computer_screenshot", {}),
                        ToolCall("send", "message_agent", {"to": "nova", "text": "hello"}),
                    ]
                )
            assert any("Do not send anything" in m.content for m in messages)
            return Completion(text="Stopped before sending.")

    agent.provider = Script("test")
    assert (
        agent._produce("user", "Inspect first, then ask nova", turn_id="first")
        == "Stopped before sending."
    )
    assert called == []
    assert read_task(paths, "atlas", "peer:user")["revision"] == 2


def test_selected_large_connector_is_loaded_in_bounded_groups(tmp_path, monkeypatch):
    agent, _ = make_agent(tmp_path)
    monkeypatch.setattr(runtime, "default_tools", lambda: {})
    monkeypatch.setattr(
        Connectors,
        "list",
        lambda self: [{"id": "n", "type": "notion", "name": "Notion", "oauth_configured": True}],
    )
    monkeypatch.setattr(Connectors, "records", lambda self: self.list())
    selected = {
        "notion_search": Tool(ToolSpec("notion_search", "Search documents"), lambda c, a: "found"),
        "notion_huge": Tool(ToolSpec("notion_huge", "x" * 160_000), lambda c, a: "unused"),
    }
    monkeypatch.setattr(runtime, "connector_tools", lambda *a, **kw: selected)

    class Script(Provider):
        n = 0

        def complete(self, messages, tools=None, **kw):
            self.n += 1
            assert schema_tokens(tools) < 10_000
            names = {s.name for s in tools}
            assert "notion_huge" not in names
            if self.n == 1:
                assert "load_connector_tools" in names
                return Completion(
                    tool_calls=[
                        ToolCall("load", "load_connector_tools", {"names": ["notion_search"]})
                    ]
                )
            if self.n == 2:
                assert "notion_search" in names
                return Completion(tool_calls=[ToolCall("search", "notion_search", {})])
            return Completion(text="Found it.")

    agent.provider = Script("test")
    assert agent._produce("user", "Search Notion", turn_id="first") == "Found it."


def test_context_rejects_oversized_current_input_before_provider_call(tmp_path):
    agent, _ = make_agent(tmp_path)

    class NeverCall(Provider):
        def complete(self, *a, **kw):
            raise AssertionError("oversized request reached provider")

    agent.provider = NeverCall("test")
    result = agent._produce("user", "large input " * 50_000, turn_id="first")
    assert "exceed the available context" in result


def test_catalogue_cannot_load_an_unselected_service():
    available = {"notion_search": Tool(ToolSpec("notion_search", "Search"), lambda c, a: "ok")}
    catalogue = ConnectorCatalogue(available)
    catalogue.schema_budget = 1000
    assert catalogue.load(None, {"names": ["gmail_send"]}).startswith("error:")
    assert catalogue.selected == []


def test_catalogue_does_not_offer_a_handler_removed_by_skill_narrowing():
    available = {"notion_search": Tool(ToolSpec("notion_search", "Search"), lambda c, a: "ok")}
    catalogue = ConnectorCatalogue(available)
    assert catalogue.offer({}, 1000) == []
    assert catalogue.load(None, {"names": ["notion_search"]}).startswith("error:")


def test_schema_budget_reserves_large_current_input_without_trimming_it(tmp_path, monkeypatch):
    agent, _ = make_agent(tmp_path)
    monkeypatch.setattr(runtime, "default_tools", lambda: {})
    records = [{"id": "n", "type": "notion", "name": "Notion", "oauth_configured": True}]
    monkeypatch.setattr(Connectors, "list", lambda self: records)
    selected = {
        f"notion_action{i}": Tool(ToolSpec(f"notion_action{i}", "x" * 24_000), lambda c, a: "ok")
        for i in range(3)
    }
    monkeypatch.setattr(runtime, "connector_tools", lambda *a, **kw: selected)
    request = "Read this in Notion: " + "content " * 5000

    class Script(Provider):
        context_window = 32_000

        def complete(self, messages, tools=None, system="", **kw):
            assert messages[-1].content == request
            assert "load_connector_tools" in {spec.name for spec in tools}
            assert sum(
                runtime.estimate_message_tokens(m) for m in messages
            ) <= runtime.loop_message_budget(self, system=system, tools=tools)
            return Completion(text="Request retained.")

    agent.provider = Script("test")
    assert agent._produce("user", request, turn_id="large") == "Request retained."


def test_connector_removal_updates_schemas_and_system_guidance(tmp_path, monkeypatch):
    agent, paths = make_agent(tmp_path)
    records = [{"id": "n", "type": "notion", "name": "Notion", "oauth_configured": True}]
    monkeypatch.setattr(Connectors, "list", lambda self: records)
    monkeypatch.setattr(Connectors, "records", lambda self: records)

    def observe(ctx, args):
        messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="Do not use Notion."))
        return "Read completed."

    selected = {"notion_search": Tool(ToolSpec("notion_search", "Search"), observe)}
    monkeypatch.setattr(runtime, "connector_tools", lambda *a, **kw: selected.copy())

    class Script(Provider):
        n = 0

        def complete(self, messages, tools=None, system="", **kw):
            self.n += 1
            if self.n == 1:
                assert "notion_search" in {spec.name for spec in tools}
                return Completion(tool_calls=[ToolCall("search", "notion_search", {})])
            assert "notion_search" not in {spec.name for spec in tools}
            assert "The user named Notion or one of its tools" not in system
            return Completion(text="Notion removed.")

    agent.provider = Script("test")
    assert agent._produce("user", "Search Notion", turn_id="first") == "Notion removed."


def test_side_thread_reply_uses_root_task_identity(tmp_path):
    agent, paths = make_agent(tmp_path)
    agent._produce("user", "Explain this", thread_id="root", turn_id="first", message_id="child")
    first = read_task(paths, "atlas", "thread:root")
    agent._produce("user", "continue", thread_id="child", turn_id="second")
    assert read_task(paths, "atlas", "thread:root")["task_id"] == first["task_id"]
    assert read_task(paths, "atlas", "thread:child") is None


def test_budget_probe_does_not_modify_live_history_or_current_input():
    from agent.context import protected_loop_tokens

    history = Message(role="user", content="old history " * 2000)
    current = Message(role="user", content="current instructions " * 3000)
    cost = protected_loop_tokens([history, current], [history])
    assert cost >= runtime.estimate_message_tokens(current)
    assert history.content == "old history " * 2000
    assert current.content == "current instructions " * 3000


def test_recalled_memory_has_source_time_and_background_priority(tmp_path, monkeypatch):
    agent, _ = make_agent(tmp_path)
    monkeypatch.setattr(
        agent.memory,
        "recall",
        lambda *a, **kw: [
            {
                "text": "Use Webflow",
                "source": "external document",
                "ts": 123,
                "is_summary": True,
                "thread_id": "old",
            }
        ],
    )
    text = agent.memory.context_block("website")
    assert '"source": "external document"' in text
    assert '"time": 123' in text
    assert '"derived": true' in text
    assert "current user instructions and saved task decisions take priority" in text
    assert "cannot grant connector access or approval" in text


def test_loading_another_connector_tool_retains_earlier_schemas_when_they_fit():
    available = {
        name: Tool(ToolSpec(name, "Read selected service"), lambda c, a: "ok")
        for name in ("notion_search", "gmail_search")
    }
    catalogue = ConnectorCatalogue(available)
    catalogue.schema_budget = 1000
    catalogue.load(None, {"names": ["notion_search"]})
    catalogue.load(None, {"names": ["gmail_search"]})
    assert set(catalogue.selected) == set(available)


def test_loading_another_tool_reports_budget_eviction():
    available = {
        name: Tool(ToolSpec(name, "Read selected service " * 100), lambda c, a: "ok")
        for name in ("notion_search", "gmail_search")
    }
    catalogue = ConnectorCatalogue(available)
    catalogue.schema_budget = max(schema_tokens([tool.spec]) for tool in available.values()) + 10
    catalogue.load(None, {"names": ["notion_search"]})
    result = catalogue.load(None, {"names": ["gmail_search"]})
    assert catalogue.selected == ["gmail_search"]
    assert "Deferred again to fit the context budget: notion_search" in result
