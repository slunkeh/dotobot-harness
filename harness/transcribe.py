"""Speech-to-text via the configured LLM provider (Grok STT, else OpenAI Whisper).

The Mac records locally and POSTs the clip here so the API key never leaves
the harness. Stdlib only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any

from .paths import HarnessPaths
from .secrets import get_secret

XAI_STT = "https://api.x.ai/v1/stt"
OPENAI_STT = "https://api.openai.com/v1/audio/transcriptions"


class TranscribeError(ValueError):
    """No provider, bad audio, or upstream STT failure."""


def transcribe(
    paths: HarnessPaths,
    data: bytes,
    *,
    filename: str = "clip.wav",
    content_type: str = "audio/wav",
    provider: str | None = None,
) -> dict[str, Any]:
    if not data:
        raise TranscribeError("audio clip is empty")
    kinds = [k for k in _order(provider, paths) if _available(paths, k)]
    if not kinds:
        raise TranscribeError(
            "Voice needs a Grok (xAI) or OpenAI key in Manage. Sign in with OAuth or add an API key."
        )
    last: TranscribeError | None = None
    for kind in kinds:
        try:
            if kind == "grok":
                return _xai(paths, data, filename, content_type)
            return _openai(paths, data, filename, content_type)
        except TranscribeError as exc:
            last = exc
    raise last or TranscribeError("speech API failed")


def _available(paths: HarnessPaths, kind: str) -> bool:
    if kind == "grok":
        return bool(_xai_token(paths))
    if kind == "openai":
        return bool(get_secret("openai", paths) or get_secret("OPENAI_API_KEY", paths))
    return False


def _order(provider: str | None, paths: HarnessPaths | None = None) -> list[str]:
    p = (provider or "").lower()
    if p in {"openai", "codex"}:
        return ["openai", "grok"]
    if p in {"grok", "xai", "xai-oauth"}:
        return ["grok", "openai"]
    pref = ""
    if paths is not None:
        try:
            data = json.loads((paths.home / "settings.json").read_text(encoding="utf-8"))
            pref = str(data.get("voice_fallback") or "").strip().lower()
        except (json.JSONDecodeError, OSError):
            pref = ""
    if pref == "openai":
        return ["openai", "grok"]
    return ["grok", "openai"]


def _xai_token(paths: HarnessPaths) -> str | None:
    from providers.xai_oauth import access_token

    return access_token(paths) or get_secret("grok", paths) or get_secret("XAI_API_KEY", paths)


def _xai(paths: HarnessPaths, data: bytes, filename: str, content_type: str) -> dict[str, Any]:
    token = _xai_token(paths)
    if not token:
        raise TranscribeError("Grok speech is not configured")
    bound, body = _multipart(
        [("format", "true"), ("language", "en")],
        filename,
        data,
        content_type,
    )
    raw = _post(XAI_STT, token, bound, body)
    text = str(raw.get("text") or "").strip()
    return {"text": text, "provider": "grok", "duration": raw.get("duration")}


def _openai(paths: HarnessPaths, data: bytes, filename: str, content_type: str) -> dict[str, Any]:
    token = get_secret("openai", paths) or get_secret("OPENAI_API_KEY", paths)
    if not token:
        raise TranscribeError("OpenAI speech is not configured")
    bound, body = _multipart(
        [("model", "whisper-1"), ("language", "en")],
        filename,
        data,
        content_type,
    )
    raw = _post(OPENAI_STT, token, bound, body)
    text = str(raw.get("text") or "").strip()
    if not text:
        raise TranscribeError("OpenAI returned no transcript")
    return {"text": text, "provider": "openai", "duration": raw.get("duration")}


def _multipart(
    fields: list[tuple[str, str]],
    filename: str,
    data: bytes,
    content_type: str,
) -> tuple[str, bytes]:
    bound = uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            (
                f"--{bound}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    safe = filename.replace('"', "") or "clip.wav"
    chunks.append(
        (
            f"--{bound}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    chunks.append(data)
    chunks.append(f"\r\n--{bound}--\r\n".encode())
    return bound, b"".join(chunks)


def _post(url: str, token: str, bound: str, body: bytes) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={bound}",
            "User-Agent": "dotobot/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise TranscribeError(f"speech API {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise TranscribeError(f"speech API failed: {exc}") from exc
    if not isinstance(raw, dict):
        raise TranscribeError("speech API returned an unexpected payload")
    return raw
