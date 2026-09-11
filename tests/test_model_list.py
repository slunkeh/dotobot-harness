"""Live vendor model catalogs — no baked-in ids."""

from __future__ import annotations

from providers import model_list


def test_openai_payload_drops_non_chat_ids():
    ids = model_list._from_openai(
        {
            "data": [
                {"id": "gpt-5.6-sol"},
                {"id": "text-embedding-3-large"},
                {"id": "whisper-1"},
                {"id": "grok-imagine-image"},
                {"id": "gpt-5.6-terra"},
            ]
        }
    )
    assert ids == ["gpt-5.6-sol", "gpt-5.6-terra"]


def test_chatgpt_codex_payload_reads_slugs_and_efforts():
    ids, efforts = model_list._from_chatgpt_codex(
        {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "supported_reasoning_efforts": ["low", "medium", "high"],
                },
                {
                    "slug": "gpt-5.6-terra",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "xhigh"},
                    ],
                },
            ]
        }
    )
    assert ids == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert efforts == ["low", "medium", "high", "xhigh"]


def test_vendor_efforts_are_not_allow_listed():
    ids, efforts = model_list._parse_catalog(
        {
            "models": [
                {
                    "slug": "vendor-x",
                    "supported_reasoning_efforts": ["low", "galaxy"],
                }
            ]
        }
    )
    assert ids == ["vendor-x"]
    assert efforts == ["low", "galaxy"]


def test_vendor_default_flag_leads_the_list():
    ids, _ = model_list._parse_catalog(
        {
            "data": [
                {"id": "older"},
                {"id": "current", "default": True},
            ]
        }
    )
    assert ids == ["current", "older"]


def test_resolve_model_uses_first_live_id_when_unset(monkeypatch):
    monkeypatch.setattr(
        model_list, "models_for", lambda pid, paths=None: (["live-a", "live-b"], [])
    )
    assert model_list.resolve_model("claude", "") == "live-a"
    assert model_list.resolve_model("claude", "explicit") == "explicit"
    monkeypatch.setattr(model_list, "models_for", lambda pid, paths=None: ([], []))
    assert model_list.resolve_model("claude", "") == ""


def test_unsigned_provider_has_no_models(tmp_path, monkeypatch):
    from harness.paths import HarnessPaths
    from harness.server import PROVIDERS

    # Host ~/.codex login is a real catalog; this test is "no credentials at all".
    monkeypatch.setattr(model_list, "_codex_chatgpt_auth", lambda paths: ("", ""))
    model_list.reset_cache()
    paths = HarnessPaths(home=tmp_path)
    for row in PROVIDERS:
        models, efforts = model_list.models_for(row["id"], paths)
        assert models == [], row["id"]
        assert efforts == [], row["id"]


def test_live_codex_models_fill_the_providers_endpoint(tmp_path, monkeypatch):
    import json
    import threading
    import urllib.request

    from harness.orchestrator import Orchestrator
    from harness.server import make_server

    monkeypatch.setattr(
        "harness.server.models_for",
        lambda pid, paths=None: (
            (["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"], ["low", "high"])
            if pid == "codex"
            else ([], [])
        ),
    )
    monkeypatch.setattr("harness.server.codex_cli_login.login_available", lambda home=None: True)
    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    httpd = make_server(orch, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/providers"
        with urllib.request.urlopen(url, timeout=5) as resp:
            rows = {p["id"]: p for p in json.loads(resp.read().decode())}
        assert rows["codex"]["models"] == ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"]
        assert rows["codex"]["reasoning"] == ["low", "high"]
        assert rows["claude"]["models"] == []
    finally:
        httpd.shutdown()
        orch.down()


def test_providers_endpoint_has_no_baked_in_catalog(tmp_path):
    from harness.server import PROVIDERS

    for row in PROVIDERS:
        assert "models" not in row
        assert "reasoning" not in row


def test_claude_picker_starts_at_five(monkeypatch):
    monkeypatch.setattr(
        model_list,
        "_fetch",
        lambda pid, paths: (
            [
                "claude-fable-5-1",
                "claude-opus-5",
                "claude-sonnet-5",
                "claude-haiku-4-5-20251001",
                "claude-opus-4-8",
                "claude-3-5-sonnet-latest",
            ],
            [],
        ),
    )
    model_list.reset_cache()
    assert model_list.models_for("claude")[0] == [
        "claude-fable-5-1",
        "claude-opus-5",
        "claude-sonnet-5",
    ]


def test_catalog_uses_api_key_before_host_chatgpt_login(monkeypatch):
    monkeypatch.setattr(model_list, "_api_key", lambda name, paths: "api-key")
    monkeypatch.setattr(model_list, "_codex_chatgpt_auth", lambda paths: ("plan-token", "acct"))
    requests = []

    def get(url, headers):
        requests.append((url, headers))
        return {"data": [{"id": "gpt-6-astra"}]}

    monkeypatch.setattr(model_list, "_get", get)
    assert model_list._fetch("codex", None)[0] == ["gpt-6-astra"]
    assert requests[0][0] == "https://api.openai.com/v1/models"
    assert requests[0][1]["authorization"] == "Bearer api-key"


def test_failed_subscription_catalog_never_falls_back_to_api(monkeypatch):
    monkeypatch.setattr(model_list, "_api_key", lambda name, paths: "api-key")
    monkeypatch.setattr(model_list, "_codex_chatgpt_auth", lambda paths: ("plan-token", "acct"))
    requests = []
    monkeypatch.setattr(model_list, "_get", lambda url, headers: requests.append(url))
    assert model_list._fetch("codex-chatgpt", None) == ([], [])
    assert len(requests) == 1 and requests[0].startswith("https://chatgpt.com/")


def test_claude_catalog_pagination_and_oauth_headers(monkeypatch):
    from providers import anthropic_oauth

    monkeypatch.setattr(anthropic_oauth, "access_token", lambda paths: "oauth")
    monkeypatch.setattr(model_list, "_api_key", lambda name, paths: "")
    requests = []

    def get(url, headers):
        requests.append((url, headers))
        if len(requests) == 1:
            return {
                "data": [{"id": "claude-fable-5-1"}],
                "has_more": True,
                "last_id": "claude-fable-5-1",
            }
        return {"data": [{"id": "claude-sonnet-5"}], "has_more": False}

    monkeypatch.setattr(model_list, "_get", get)
    assert model_list._fetch("claude", None)[0] == ["claude-fable-5-1", "claude-sonnet-5"]
    assert "after_id=claude-fable-5-1" in requests[1][0]
    assert requests[0][1]["anthropic-beta"] == ",".join(anthropic_oauth.OAUTH_BETAS)


def test_minimax_catalog_matches_oauth_region(monkeypatch):
    from providers import minimax_oauth

    monkeypatch.setattr(minimax_oauth, "access_token", lambda paths: "oauth")
    monkeypatch.setattr(
        minimax_oauth, "inference_base", lambda paths: "https://api.minimaxi.com/anthropic"
    )
    requests = []
    monkeypatch.setattr(
        model_list,
        "_get",
        lambda url, headers: requests.append(url) or {"data": [{"id": "MiniMax-M3"}]},
    )
    assert model_list._fetch("minimax", None)[0] == ["MiniMax-M3"]
    assert requests == ["https://api.minimaxi.com/anthropic/v1/models"]


def test_non_chat_catalog_excludes_current_media_models():
    ids, _ = model_list._parse_catalog(
        {
            "data": [
                {"id": mid}
                for mid in [
                    "gpt-6-astra",
                    "gpt-audio-2",
                    "grok-imagine-video-1.5",
                    "qwen-image-3.0-pro",
                    "wan3.0-video",
                    "glm-image",
                    "glm-asr-2512",
                    "MiniMax-M3",
                    "speech-2.8",
                    "music-3.0",
                    "glm-5.3-flash",
                ]
            ]
        }
    )
    assert ids == ["gpt-6-astra", "MiniMax-M3", "glm-5.3-flash"]


def test_catalog_cache_follows_credentials_and_recovers_immediately(tmp_path, monkeypatch):
    import io
    import json
    import urllib.error

    from harness.paths import HarnessPaths

    model_list.reset_cache()
    paths = HarnessPaths(home=tmp_path)
    key = [""]
    requests = []
    monkeypatch.setattr(model_list, "_api_key", lambda name, paths: key[0])

    def urlopen(request, timeout):
        bearer = request.get_header("Authorization")
        requests.append(bearer)
        if bearer == "Bearer failing":
            raise urllib.error.URLError("offline")
        return io.BytesIO(json.dumps({"data": [{"id": bearer.removeprefix("Bearer ")}]}).encode())

    monkeypatch.setattr(model_list.urllib.request, "urlopen", urlopen)
    assert model_list.models_for("deepseek", paths) == ([], [])
    key[0] = "first-model"
    first, _ = model_list.models_for("deepseek", paths)
    first.append("mutated")
    assert model_list.models_for("deepseek", paths)[0] == ["first-model"]
    assert requests == ["Bearer first-model"]
    key[0] = "second-model"
    assert model_list.models_for("deepseek", paths)[0] == ["second-model"]
    key[0] = "failing"
    assert model_list.models_for("deepseek", paths) == ([], [])
    assert model_list.models_for("deepseek", paths) == ([], [])
    assert requests.count("Bearer failing") == 2
    key[0] = ""
    assert model_list.models_for("deepseek", paths) == ([], [])


def test_old_models_remain_explicitly_usable_but_not_selectable():
    assert (
        model_list.resolve_model("claude", "claude-3-5-sonnet-latest") == "claude-3-5-sonnet-latest"
    )
    assert not model_list.is_picker_model("claude", "claude-3-5-sonnet-latest")
    assert not model_list.is_picker_model("codex", "gpt-4o")
    assert model_list.is_picker_model("codex", "gpt-6-astra")


def test_pagination_stops_when_vendor_repeats_cursor(monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_list,
        "_get",
        lambda url, headers: (
            calls.append(url)
            or {
                "data": [{"id": "claude-fable-5-1"}],
                "has_more": True,
                "last_id": "same",
            }
        ),
    )
    assert model_list._anthropic_models("https://example.test/models", {})[0] == [
        "claude-fable-5-1"
    ]
    assert len(calls) == 2


def test_subscription_catalog_refreshes_once_after_401(monkeypatch):
    calls = []
    refreshes = []

    def auth(paths, *, force=False):
        refreshes.append(force)
        return ("fresh" if force else "stale", "account")

    def get(url, headers):
        calls.append(headers["authorization"])
        if headers["authorization"] == "Bearer stale":
            raise model_list._Unauthorized()
        return {"models": [{"slug": "gpt-6-astra"}]}

    monkeypatch.setattr(model_list, "_codex_chatgpt_auth", auth)
    monkeypatch.setattr(model_list, "_get", get)
    assert model_list.models_for("codex-chatgpt")[0] == ["gpt-6-astra"]
    assert calls == ["Bearer stale", "Bearer fresh"]
    assert refreshes == [False, True]
