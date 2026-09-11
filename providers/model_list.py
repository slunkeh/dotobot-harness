"""Live model catalogs from each vendor — never a baked-in list.

GET /api/providers fills `models` (and `reasoning` when the vendor publishes
it) from the provider's own models endpoint. Unsigned or failed fetches
return empty lists; the client still offers a Default / free-text model.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_TTL = 300.0
_TIMEOUT = 6
_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()

#: Capability classes, not a model catalog. Dropped so /v1/models is usable
#: as a chat picker (embeddings, TTS, image, realtime, …).
_SKIP_ID = re.compile(
    r"(embedding|whisper|tts|dall-e|dall_e|transcribe|moderation|babbage|"
    r"ada-00|davinci|sora|gpt-image|imagine-image|omni-moderation|realtime|"
    r"audio|voice|imagine-video|qwen-image|wan[0-9]|glm-image|cogview|cogvideo|"
    r"(?:^|[-_])(asr|speech|music|rerank|ocr)(?:[-_]|$)|hailuo)",
    re.I,
)


class _Unauthorized(Exception):
    """A catalog rejected its bearer; the subscription route can refresh once."""


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def models_for(provider_id: str, paths: Any = None) -> tuple[list[str], list[str]]:
    """`(models, reasoning_efforts)` from the vendor, or empty lists."""
    pid = (provider_id or "").strip().lower()
    if not pid:
        return [], []
    try:
        models, efforts = _fetch(pid, paths)
        return [mid for mid in models if is_picker_model(pid, mid)], efforts
    except Exception:
        return [], []


def is_picker_model(provider_id: str, model: str) -> bool:
    """Picker policy only: explicit saved model ids still resolve unchanged."""
    if _SKIP_ID.search(model):
        return False
    if provider_id in {"claude", "anthropic"}:
        version = re.match(r"^claude-(?:[a-z]+-)?([0-9]+)", model.lower())
        return bool(version and int(version.group(1)) >= 5)
    if provider_id in {"codex", "openai", "codex-chatgpt", "chatgpt"}:
        # Older GPT/o-series and deprecated Codex/chat snapshots are no
        # longer offered for new selections; unknown future ids pass through.
        if re.match(r"^(?:gpt-[1-4](?:o|[.-]|$)|o[1-4](?:-|$)|chatgpt-4o|codex-mini)", model):
            return False
        if re.match(r"^gpt-5(?:\.[12])?-(?:codex|chat)", model) or model == "gpt-5.3-chat-latest":
            return False
    return True


def resolve_model(provider_id: str, model: str | None, paths: Any = None) -> str:
    """Explicit roster id wins; otherwise the vendor's first live model.

    Empty string means the adapter's own last-ditch default (used only when
    the catalog is unsigned or unreachable — never shown as a picker list).
    """
    mid = (model or "").strip()
    if mid:
        return mid
    live, _ = models_for(provider_id, paths)
    return live[0] if live else ""


def _fetch(pid: str, paths: Any) -> tuple[list[str], list[str]]:
    if pid in {"codex", "openai", "codex-chatgpt", "chatgpt"}:
        return _codex(paths, subscription_only=pid in {"codex-chatgpt", "chatgpt"})
    if pid in {"claude", "anthropic"}:
        return _claude(paths)
    if pid in {"grok", "xai", "xai-oauth"}:
        return _openai_compat("https://api.x.ai/v1/models", _grok_bearer(paths))
    if pid == "deepseek":
        return _openai_compat(
            "https://api.deepseek.com/v1/models", _api_key("DEEPSEEK_API_KEY", paths)
        )
    if pid == "qwen":
        return _openai_compat(
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
            _api_key("DASHSCOPE_API_KEY", paths),
        )
    if pid in {"glm", "zai"}:
        return _openai_compat(
            "https://api.z.ai/api/paas/v4/models",
            _api_key("GLM_API_KEY", paths) or _api_key("ZAI_API_KEY", paths),
        )
    if pid == "kimi":
        return _openai_compat("https://api.moonshot.ai/v1/models", _api_key("KIMI_API_KEY", paths))
    if pid in {"minimax", "minimax-oauth"}:
        return _minimax(paths)
    return [], []


def _api_key(env_name: str, paths: Any) -> str:
    from harness.secrets import get_secret

    return (get_secret(env_name, paths) or "").strip()


def _grok_bearer(paths: Any) -> str:
    from providers.xai_oauth import access_token

    token = (access_token(paths) or "").strip()
    return token or _api_key("XAI_API_KEY", paths)


def _codex(paths: Any, *, subscription_only: bool = False) -> tuple[list[str], list[str]]:
    # Match build_agent: an API key wins over subscription/host login. Never
    # advertise another route's models when the active route fails.
    key = "" if subscription_only else _api_key("OPENAI_API_KEY", paths)
    if key:
        return _openai_compat("https://api.openai.com/v1/models", key)
    token, account = _codex_chatgpt_auth(paths)
    if not token or not account:
        return [], []
    url = "https://chatgpt.com/backend-api/codex/models?client_version=99.99.99"
    headers = {
        "authorization": f"Bearer {token}",
        "chatgpt-account-id": account,
        "accept": "application/json",
    }
    try:
        payload = _get(url, headers)
    except _Unauthorized:
        token, account = _codex_chatgpt_auth(paths, force=True)
        if not token or not account:
            return [], []
        headers.update({"authorization": f"Bearer {token}", "chatgpt-account-id": account})
        payload = _get(url, headers)
    return _parse_catalog(payload) if payload else ([], [])


def _codex_chatgpt_auth(paths: Any, *, force: bool = False) -> tuple[str, str]:
    from providers import codex_login, codex_oauth

    if codex_oauth.oauth_configured(paths):
        token = (codex_oauth.access_token(paths, force=force) or "").strip()
        account = (codex_oauth.account_id(paths) or "").strip()
        return token, account
    try:
        creds = codex_login.load_credentials()
        if force:
            creds = codex_login.refresh_credentials(creds)
    except Exception:
        return "", ""
    return creds.access_token, creds.account_id


def _claude(paths: Any) -> tuple[list[str], list[str]]:
    from providers import anthropic_oauth

    headers = {
        "anthropic-version": "2023-06-01",
        "accept": "application/json",
    }
    bearer = (anthropic_oauth.access_token(paths) or "").strip()
    key = _api_key("ANTHROPIC_API_KEY", paths)
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
        headers["anthropic-beta"] = ",".join(anthropic_oauth.OAUTH_BETAS)
        headers["user-agent"] = anthropic_oauth.TOKEN_USER_AGENT
        headers["x-app"] = "cli"
    elif key:
        headers["x-api-key"] = key
    else:
        return [], []
    return _anthropic_models("https://api.anthropic.com/v1/models", headers)


def _minimax(paths: Any) -> tuple[list[str], list[str]]:
    from providers import minimax_oauth

    bearer = (minimax_oauth.access_token(paths) or "").strip()
    token = bearer or _api_key("MINIMAX_API_KEY", paths)
    if not token:
        return [], []
    base = minimax_oauth.inference_base(paths) if bearer else minimax_oauth.GLOBAL_INFERENCE
    return _anthropic_models(
        base.rstrip("/") + "/v1/models",
        {
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
        },
    )


def _anthropic_models(url: str, headers: dict[str, str]) -> tuple[list[str], list[str]]:
    rows: list[Any] = []
    seen: set[str] = set()
    next_url = url
    # Bound work even if a vendor returns broken/repeated pagination cursors.
    for _ in range(5):
        payload = _get(next_url, headers)
        if not payload or not isinstance(payload.get("data"), list):
            break
        rows.extend(payload["data"])
        cursor = payload.get("last_id")
        if (
            not payload.get("has_more")
            or not isinstance(cursor, str)
            or not cursor
            or cursor in seen
        ):
            break
        seen.add(cursor)
        next_url = url + "?" + urllib.parse.urlencode({"after_id": cursor})
    return _parse_catalog({"data": rows})


def _openai_compat(url: str, token: str) -> tuple[list[str], list[str]]:
    if not token:
        return [], []
    payload = _get(url, {"authorization": f"Bearer {token}", "accept": "application/json"})
    return _parse_catalog(payload) if payload else ([], [])


def _get(url: str, headers: dict[str, str]) -> dict[str, Any] | None:
    # Cache by request identity, not provider name: changing credentials or
    # homes must immediately discover that account's catalog. Store only a
    # digest of headers, and never cache a failure or unsigned result.
    identity = hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest()
    key = (url, identity)
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _TTL:
            return copy.deepcopy(hit[1])
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise _Unauthorized from exc
        return None
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
    ):
        return None
    if isinstance(payload, list):
        payload = {"data": payload}
    if not isinstance(payload, dict):
        return None
    with _LOCK:
        for old in [k for k, v in _CACHE.items() if now - v[0] >= _TTL]:
            del _CACHE[old]
        _CACHE[key] = (now, copy.deepcopy(payload))
    return payload


def _parse_catalog(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Ids + reasoning efforts exactly as the vendor published them."""
    rows = payload.get("data")
    if not isinstance(rows, list):
        rows = payload.get("models") if isinstance(payload.get("models"), list) else []
    models: list[str] = []
    defaults: list[str] = []
    efforts: list[str] = []
    seen_m: set[str] = set()
    seen_e: set[str] = set()
    for row in rows:
        mid = ""
        flagged = False
        raw_efforts: Any = None
        if isinstance(row, str):
            mid = row.strip()
        elif isinstance(row, dict):
            if row.get("visibility") == "hide" or row.get("deprecated") is True:
                continue
            mid = str(
                row.get("slug") or row.get("id") or row.get("model") or row.get("name") or ""
            ).strip()
            flagged = bool(row.get("default") or row.get("is_default") or row.get("isDefault"))
            raw_efforts = (
                row.get("supported_reasoning_efforts")
                or row.get("supportedReasoningEfforts")
                or row.get("supported_reasoning_levels")
                or row.get("supportedReasoningLevels")
                or row.get("reasoning_efforts")
                or row.get("reasoning")
            )
        if mid and mid not in seen_m and not _SKIP_ID.search(mid):
            seen_m.add(mid)
            (defaults if flagged else models).append(mid)
        if isinstance(raw_efforts, dict):
            raw_efforts = raw_efforts.get("efforts") or raw_efforts.get("supported") or []
        if isinstance(raw_efforts, list):
            for item in raw_efforts:
                effort = str(
                    item
                    if not isinstance(item, dict)
                    else item.get("effort") or item.get("id") or item.get("name") or ""
                ).strip()
                if effort and effort not in seen_e:
                    seen_e.add(effort)
                    efforts.append(effort)
    return defaults + models, efforts


def _from_openai(payload: dict[str, Any]) -> list[str]:
    return _parse_catalog(payload)[0]


def _from_chatgpt_codex(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    return _parse_catalog(payload)
