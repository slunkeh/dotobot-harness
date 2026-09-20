"""ElevenLabs audio credentials and outbound connections; never runs an LLM."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .redaction import register_secret
from .secrets import delete_secret, get_secret, secret_source, set_secret
from .voice import VoiceError

API = "https://api.elevenlabs.io"
KEY = "ELEVENLABS_API_KEY"
TOKEN_TYPES = {"realtime_scribe", "tts_websocket"}
_LOCK = threading.RLock()
_CALLS: dict[tuple[str, str], dict] = {}


def request(paths, route: str, *, method="GET", key=None) -> dict:
    key = key or get_secret(KEY, paths)
    if not key:
        raise VoiceError("Connect ElevenLabs in Voice providers with your API key.")
    register_secret(key, KEY)
    req = urllib.request.Request(
        API + route,
        method=method,
        data=b"{}" if method == "POST" else None,
        headers={"xi-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read(2_000_000))
    except urllib.error.HTTPError as exc:
        # Only translate known codes to local text; upstream messages can contain secrets.
        code = None
        try:
            body = json.loads(exc.read(65_536))
            error = body.get("detail") if isinstance(body, dict) else None
            if isinstance(error, dict):
                code = error.get("code")
                if code not in {"invalid_api_key", "missing_permissions", "quota_exceeded"}:
                    code = error.get("status")
        except (OSError, ValueError):
            pass
        if not isinstance(code, str):
            code = None
        detail = {
            402: "Quota exhausted. Check your ElevenLabs account and retry.",
            401: "Authentication failed. Check the API key and its permissions.",
            403: "API key lacks permission. Check its voices, transcription and speech permissions.",
            404: "Voice no longer exists. Choose another voice in Voice providers.",
            429: "Quota or rate limit reached. Check your ElevenLabs account and retry.",
        }.get(exc.code, f"Request failed (HTTP {exc.code}). Retry the connection.")
        detail = {
            "invalid_api_key": "API key rejected. Reconnect ElevenLabs in Voice providers.",
            "missing_permissions": (
                "API key lacks permission. Enable Voices: Read to connect and choose a voice; "
                "enable Speech to Text and Text to Speech for voice conversations."
            ),
            "quota_exceeded": "Quota exhausted. Check your ElevenLabs account and retry.",
        }.get(code, detail)
        raise VoiceError("ElevenLabs: " + detail) from None
    except (OSError, ValueError, urllib.error.URLError):
        raise VoiceError("ElevenLabs connection failed. Check the network and retry.") from None
    if not isinstance(result, dict):
        raise VoiceError("ElevenLabs returned an invalid response. Retry the connection.")
    return result


def status(paths) -> dict:
    return {"configured": bool(get_secret(KEY, paths)), "source": secret_source(KEY, paths)}


def connect(paths, key: str) -> dict:
    if not isinstance(key, str) or not key.strip():
        raise VoiceError("Enter an ElevenLabs API key.")
    request(paths, "/v2/voices?page_size=1", key=key.strip())
    set_secret(KEY, key.strip(), paths)
    return status(paths)


def disconnect(paths) -> dict:
    if secret_source(KEY, paths) == "env":
        raise VoiceError("Remove ELEVENLABS_API_KEY from the server environment to disconnect.")
    delete_secret(KEY, paths)
    return status(paths)


def voice_id(value) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not all(c.isascii() and (c.isalnum() or c in "_-") for c in value)
    ):
        raise VoiceError("Choose an ElevenLabs voice in Voice providers.")
    return value


def validate_voice(paths, value: str) -> str:
    value = voice_id(value)
    result = request(paths, "/v1/voices/" + value)
    if result.get("voice_id") != value:
        raise VoiceError("ElevenLabs voice unavailable. Choose another voice in Voice providers.")
    return value


def voices(paths, search="", page="") -> dict:
    query = {"page_size": 100}
    if search:
        query["search"] = search[:200]
    if page:
        query["next_page_token"] = page[:500]
    result = request(paths, "/v2/voices?" + urllib.parse.urlencode(query))
    return {
        "voices": [
            {
                "voice_id": row["voice_id"],
                "name": str(row.get("name") or row["voice_id"]),
                "preview_url": row.get("preview_url") if isinstance(row.get("preview_url"), str) else None,
                "labels": {
                    key: value for key, value in (row.get("labels") or {}).items()
                    if key in {"accent", "age", "gender", "description", "use_case", "language"}
                    and isinstance(value, str)
                } if isinstance(row.get("labels"), dict) else {},
            }
            for row in result.get("voices", [])
            if isinstance(row, dict) and isinstance(row.get("voice_id"), str)
        ],
        "has_more": bool(result.get("has_more")),
        "next_page_token": result.get("next_page_token"),
    }


def mint_token(paths, token_type: str) -> str:
    if token_type not in TOKEN_TYPES:
        raise VoiceError("Unsupported ElevenLabs token type.")
    result = request(paths, "/v1/single-use-token/" + token_type, method="POST")
    token = result.get("token")
    if not isinstance(token, str) or not token:
        raise VoiceError("ElevenLabs returned no connection token. Retry the connection.")
    register_secret(token, "elevenlabs_single_use_token")
    return token


def _scope(paths) -> str:
    return str(paths.home.resolve())


def session(paths, bot: str, selected_voice: str) -> dict:
    selected_voice = validate_voice(paths, selected_voice)
    token = mint_token(paths, "realtime_scribe")
    call_id = uuid.uuid4().hex
    with _LOCK:
        now = time.monotonic()
        for key, value in list(_CALLS.items()):
            if now - value["created"] > 24 * 3600:
                del _CALLS[key]
        if sum(key[0] == _scope(paths) for key in _CALLS) >= 1024:
            raise VoiceError("Too many voice sessions. Retry after existing sessions expire.")
        _CALLS[(_scope(paths), call_id)] = {
            "bot": bot,
            "voice": selected_voice,
            "created": now,
            "events": {},
            "closed": False,
        }
    return {
        "ok": True,
        "backend": "elevenlabs",
        "bot": bot,
        "call_id": call_id,
        "voice": selected_voice,
        "model": "scribe_v2_realtime",
        "token": token,
        "ws_url": "wss://api.elevenlabs.io/v1/speech-to-text/realtime",
        "instructions": "",
        "auth": "single-use-token",
    }


def call(paths, bot: str, call_id: str, *, allow_closed=False) -> dict:
    with _LOCK:
        found = _CALLS.get((_scope(paths), call_id))
        if not found or found["bot"] != bot or time.monotonic() - found["created"] > 24 * 3600:
            raise VoiceError("Voice call expired. Close it and start a new call.")
        if found["closed"] and not allow_closed:
            raise VoiceError("Voice call has ended. Start a new call.")
        return dict(found)


def connection(paths, bot: str, call_id: str, token_type: str) -> dict:
    active = call(paths, bot, call_id)
    return {"token": mint_token(paths, token_type), "voice": active["voice"], "call_id": call_id}


def event(paths, bot: str, call_id: str, name: str) -> dict:
    from .voice import log_voice_event

    if name not in {"started", "ended"}:
        raise VoiceError("voice event must be started or ended")
    with _LOCK:
        active = call(paths, bot, call_id, allow_closed=True)
        if name in active["events"]:
            return {**active["events"][name], "duplicate": True}
        if active["closed"]:
            raise VoiceError("Voice call has ended.")
        result = log_voice_event(paths, bot, name, call_id=call_id)
        stored = _CALLS[(_scope(paths), call_id)]
        stored["events"][name] = result
        stored["closed"] = name == "ended"
        return result
