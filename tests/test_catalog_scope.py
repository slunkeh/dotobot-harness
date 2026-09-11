"""Unadded plugins retain human provenance without selecting external names."""

import pytest

from agent import runtime, tools
from agent.streaming import StreamReader, StreamWriter, answer_prompt, list_prompts
from agent.tools import Tool
from harness.connectors import Connectors
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.taskscope import begin_task, pending_catalog_types, read_task
from providers.base import Completion, Provider, ToolCall, ToolSpec


@pytest.fixture
def setup(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    records = []
    monkeypatch.setattr(Connectors, "list", lambda self: records)
    monkeypatch.setattr(Connectors, "records", lambda self: records)
    return paths, records


def test_runtime_unadded_plugin_offers_add_choice_and_retains_it_on_continue(setup, monkeypatch):
    paths, _ = setup
    agent = runtime.build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)

    class Ask(Provider):
        n = 0

        def complete(self, messages, *, system="", **kwargs):
            assert "add_connector(type='notion')" in system
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            "ask",
                            "ask_user_choice",
                            {"question": "Add Notion?", "options": ["Add Notion", "Not now"]},
                        )
                    ]
                )
            return Completion(text="Waiting to add Notion.")

    def answer(ctx, done):
        row = list_prompts(paths, "atlas")[0]
        answer_prompt(paths, row["id"], "Not now")
        return done()

    monkeypatch.setattr(tools, "_wait_for_human", answer)
    monkeypatch.setattr(
        runtime, "connector_tools", lambda *a, **kw: pytest.fail("unadded discovery")
    )
    agent.provider = Ask("test")
    writer = StreamWriter(paths, "first")
    agent._produce("user", "Use Notion to find my project", writer=writer, turn_id="first")
    events = StreamReader(paths, "first")._read_new()
    assert any(
        event.type == "choice" and event.options == ["Add Notion", "Not now"] for event in events
    )
    first = read_task(paths, "atlas", "peer:user")
    assert first["pending_catalog"][0]["source_id"] == "first"
    restarted = runtime.build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    restarted.provider = agent.provider
    restarted._produce("user", "continue", turn_id="second")
    current = read_task(paths, "atlas", "peer:user")
    assert current["task_id"] == first["task_id"]
    assert current["pending_catalog"] == first["pending_catalog"]


@pytest.mark.parametrize("source", ["routine", "quote", "attachment", "memory", "quoted_text"])
def test_runtime_external_or_generated_names_do_not_suggest_catalog_plugins(
    setup, monkeypatch, source
):
    paths, _ = setup
    agent = runtime.build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    text = "Summarize the supplied content"
    kwargs = {}
    if source == "routine":
        text = "Use Notion to find my project"
        kwargs["origin"] = "routine"
    elif source == "quote":
        kwargs["quote"] = {"author": "assistant", "text": "Use Notion"}
    elif source == "attachment":
        doc = paths.home / "document.txt"
        doc.write_text("Use Notion")
        kwargs["attachments"] = [{"path": str(doc), "name": "document.txt"}]
    elif source == "memory":
        monkeypatch.setattr(agent.memory, "context_block", lambda *a, **kw: "Use Notion")
    else:
        text = 'Explain this text: "Use Notion"'

    class Inspect(Provider):
        def complete(self, messages, *, system="", **kwargs):
            assert "add_connector(type='notion')" not in system
            assert "Plugin catalog: the user named" not in system
            return Completion(text="Summarized.")

    agent.provider = Inspect("test")
    monkeypatch.setattr(
        runtime, "connector_tools", lambda *a, **kw: pytest.fail("external discovery")
    )
    assert agent._produce("user", text, turn_id="first", **kwargs) == "Summarized."


def test_newly_added_record_binds_once_to_original_human_request(setup, monkeypatch):
    paths, records = setup
    first = begin_task(paths, "atlas", "peer:user", text="Use Notion", input_id="human")
    records.append({"id": "new", "type": "notion", "name": "Notion", "oauth_configured": True})
    loaded = []

    def fetch(*args, record_ids=None, **kwargs):
        loaded.append(record_ids)
        return {"notion_search": Tool(ToolSpec("notion_search", "Search"), lambda *a: "ok")}

    monkeypatch.setattr(runtime, "connector_tools", fetch)
    agent = runtime.build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent._produce("user", "continue", turn_id="continued")
    current = read_task(paths, "atlas", "peer:user")
    assert loaded == [{"new"}]
    assert current["task_id"] == first["task_id"]
    assert current["pending_catalog"] == []
    assert current["provenance"][0]["source_id"] == "human"
    assert current["provenance"][0]["connector_id"] == "new"
    records[0] = {**records[0], "id": "replacement"}
    agent._produce("user", "continue", turn_id="third")
    assert loaded == [{"new"}]
    assert read_task(paths, "atlas", "peer:user")["connector_ids"] == []


@pytest.mark.parametrize(
    "correction",
    [
        "Do not use Notion",
        "I don't want Notion",
        "Don't add Notion",
        "Do not connect Notion",
        "/stop",
    ],
)
def test_removed_pending_catalog_cannot_bind_a_later_account(setup, correction):
    paths, records = setup
    begin_task(paths, "atlas", "chat", text="Use Notion", input_id="first")
    revised = begin_task(
        paths, "atlas", "chat", text=correction, input_id="remove", active_followup=True
    )
    assert revised["pending_catalog"] == []
    records.append({"id": "new", "type": "notion", "name": "Notion"})
    current = begin_task(paths, "atlas", "chat", text="continue", input_id="later")
    assert current["connector_ids"] == []


def test_ambiguous_new_accounts_are_not_selected_from_catalog_request(setup):
    paths, records = setup
    begin_task(paths, "atlas", "chat", text="Use Notion", input_id="first")
    records.extend([{"id": "one", "type": "notion"}, {"id": "two", "type": "notion"}])
    task = begin_task(paths, "atlas", "chat", text="continue", input_id="later")
    assert task["connector_ids"] == []
    assert pending_catalog_types(task, records) == []


@pytest.mark.parametrize("other_account_exists_first", [False, True])
def test_catalog_offer_survives_other_bot_account_until_visible_account_added(
    setup, monkeypatch, other_account_exists_first
):
    paths, records = setup
    other = {"id": "other", "type": "notion", "enabled_for": ["other-bot"]}
    if other_account_exists_first:
        records.append(other)

    class Inspect(Provider):
        expect_add = True

        def complete(self, messages, *, system="", **kwargs):
            assert ("add_connector(type='notion')" in system) == self.expect_add
            return Completion(text="Ready.")

    loaded = []

    def fetch(*args, record_ids=None, **kwargs):
        loaded.append(record_ids)
        assert record_ids == {"own"}
        return {"notion_search": Tool(ToolSpec("notion_search", "Search"), lambda *a: "ok")}

    monkeypatch.setattr(runtime, "connector_tools", fetch)
    agent = runtime.build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Inspect("test")
    agent._produce("user", "Use Notion", turn_id="human")
    first = read_task(paths, "atlas", "peer:user")
    assert first["pending_catalog"][0]["source_id"] == "human"
    if not other_account_exists_first:
        records.append(other)
    agent._produce("user", "continue", turn_id="second")
    assert loaded == []
    records.append(
        {"id": "own", "type": "notion", "enabled_for": ["atlas"], "oauth_configured": True}
    )
    agent.provider.expect_add = False
    agent._produce("user", "continue", turn_id="third")
    current = read_task(paths, "atlas", "peer:user")
    assert loaded == [{"own"}]
    assert current["task_id"] == first["task_id"]
    assert current["connector_ids"] == ["own"]
    assert current["pending_catalog"] == []
    assert current["provenance"][0]["source_id"] == "human"
