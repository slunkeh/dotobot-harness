"""Grok Voice live-talk: ephemeral xAI secrets, memory, and real actions.

The API key stays on the harness. Clients mint a short-lived token, speak
directly to wss://api.x.ai/v1/realtime, then POST completed transcripts so
the existing session JSONL / WS fan-out stays the 1:1 thread.

The spoken model is not the agent loop. Soul, recent 1:1 history, and
durable facts are packed into the voice instructions at mint time. Tools
on the realtime session (`recall`, `remember`, `act`) hit this module:
`act` inboxes a real user-lane turn so computer / connectors / siblings
run through govern like any other chat.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from agent.memory import Memory
from agent.soul import load_soul

from . import prefs
from .secrets import get_secret
from .transcribe import TranscribeError

XAI_SECRETS = "https://api.x.ai/v1/realtime/client_secrets"
XAI_REALTIME_WS = "wss://api.x.ai/v1/realtime"
OPENAI_SECRETS = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_REALTIME_WS = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "grok-voice-latest"
DEFAULT_VOICE = "eve"
OPENAI_MODEL = "gpt-realtime"
OPENAI_VOICE = "alloy"
BACKENDS = ("grok", "openai")
#: Chat LLM → native spoken backend. Claude has none.
NATIVE_BACKEND = {
    "grok": "grok",
    "xai": "grok",
    "xai-oauth": "grok",
    "openai": "openai",
    "codex": "openai",
}
_HISTORY_TURNS = 24
_HISTORY_CHARS = 6_000
_FACT_CHARS = 2_000
ACT_TIMEOUT = 180.0

VOICE_TOOLS = [
    {
        "type": "function",
        "name": "recall",
        "description": "Search this bot's memory and past chats. Use when the user asks what you remember, what happened earlier, or about people/projects not in the prompt.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "remember",
        "description": "Store a durable fact in this bot's memory.",
        "parameters": {
            "type": "object",
            "properties": {"fact": {"type": "string"}},
            "required": ["fact"],
        },
    },
    {
        "type": "function",
        "name": "act",
        "description": "Do real work as this bot: computer, browser, files, other bots, skills. Returns what happened. Call this whenever the user asks you to look something up, open a site, click, type, or take any action. Never pretend you did it.",
        "parameters": {
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
        },
    },
]


class VoiceError(TranscribeError):
    """No xAI key, upstream mint failure, or a bad voice_turn payload."""


def _xai_token(paths) -> str | None:
    from providers.xai_oauth import access_token

    return access_token(paths) or get_secret("grok", paths) or get_secret("XAI_API_KEY", paths)


def _openai_token(paths) -> str | None:
    return get_secret("openai", paths) or get_secret("OPENAI_API_KEY", paths)


def available_backends(paths) -> list[str]:
    out: list[str] = []
    if _xai_token(paths):
        out.append("grok")
    if _openai_token(paths):
        out.append("openai")
    return out


def _load_settings(paths) -> dict[str, Any]:
    return prefs.load(paths)


def _save_settings(paths, data: dict[str, Any]) -> None:
    prefs.save(paths, data)


def voice_fallback(paths) -> str:
    """Preferred spoken backend when the chat model has no native voice."""
    avail = available_backends(paths)
    raw = str(_load_settings(paths).get("voice_fallback") or "").strip().lower()
    if raw in avail:
        return raw
    return avail[0] if avail else ""


def set_voice_fallback(paths, value: str) -> dict[str, Any]:
    choice = str(value or "").strip().lower()
    if choice not in BACKENDS:
        raise VoiceError("voice fallback must be grok or openai")
    data = _load_settings(paths)
    data["voice_fallback"] = choice
    _save_settings(paths, data)
    return voice_status(paths)


def set_voice_settings(paths, changes: dict) -> dict:
    from .elevenlabs import validate_voice

    data = _load_settings(paths)
    if "provider" in changes:
        if changes["provider"] not in {"auto", "grok", "openai", "elevenlabs"}:
            raise VoiceError("Voice provider must be auto, grok, openai or elevenlabs")
        data["voice_provider"] = changes["provider"]
    if "elevenlabs_voice_id" in changes:
        value = changes["elevenlabs_voice_id"]
        data["elevenlabs_voice_id"] = validate_voice(paths, value) if value else None
    if "fallback" in changes:
        if changes["fallback"] not in BACKENDS:
            raise VoiceError("voice fallback must be grok or openai")
        data["voice_fallback"] = changes["fallback"]
    _save_settings(paths, data)
    return voice_status(paths)


def resolve_backend(paths, bot_provider: str = "", voice_provider: str | None = None) -> str | None:
    from .elevenlabs import status

    choice = voice_provider or _load_settings(paths).get("voice_provider") or "auto"
    if choice == "elevenlabs":
        if not status(paths)["configured"]:
            raise VoiceError("Connect ElevenLabs in Voice providers with your API key.")
        return choice
    if choice == "grok":
        if not _xai_token(paths):
            raise VoiceError("Grok voice is unavailable. Connect Grok in LLM Providers.")
        return choice
    if choice == "openai":
        if not _openai_token(paths):
            raise VoiceError("OpenAI voice is unavailable. Connect OpenAI in Providers.")
        return choice
    # Auto deliberately considers only legacy voice backends.
    avail = available_backends(paths)
    if not avail:
        return None
    native = NATIVE_BACKEND.get(str(bot_provider or "").strip().lower())
    if native in avail:
        return native
    pref = voice_fallback(paths)
    if pref in avail:
        return pref
    return avail[0]


def voice_status(paths) -> dict[str, Any]:
    from .elevenlabs import status

    avail = available_backends(paths)
    eleven = status(paths)
    settings = _load_settings(paths)
    selected = settings.get("voice_provider") or "auto"
    available = (
        bool(avail)
        if selected == "auto"
        else (eleven["configured"] if selected == "elevenlabs" else selected in avail)
    )
    return {
        "available": available,
        "provider": settings.get("voice_provider") or "auto",
        "elevenlabs": eleven,
        "elevenlabs_voice_id": settings.get("elevenlabs_voice_id"),
        "providers": avail,
        "fallback": voice_fallback(paths),
    }


def mint_session(
    paths,
    *,
    bot: str,
    display_name: str = "",
    personality: str = "",
    bot_provider: str = "",
    expires: int = 600,
    voice_provider: str | None = None,
    elevenlabs_voice_id: str | None = None,
) -> dict[str, Any]:
    """Mint an ephemeral realtime token bound to this bot's soul."""
    backend = resolve_backend(paths, bot_provider, voice_provider)
    if backend == "elevenlabs":
        from .elevenlabs import session

        selected = elevenlabs_voice_id or _load_settings(paths).get("elevenlabs_voice_id")
        return session(paths, bot, selected)
    if not backend:
        raise VoiceError(
            "Live talk needs Grok or OpenAI in Manage. Sign in with OAuth or add an API key."
        )
    soul = ""
    try:
        soul = load_soul(paths, bot, personality=personality) or ""
    except Exception:
        soul = personality or ""
    who = display_name or bot
    instructions = _voice_instructions(
        paths, bot, who=who, soul=soul.strip(), personality=personality or ""
    )
    if backend == "openai":
        return _mint_openai(paths, bot=bot, instructions=instructions, expires=expires)
    return _mint_grok(paths, bot=bot, instructions=instructions, expires=expires)


def _ephemeral_secret(raw: dict[str, Any]) -> str:
    value = str(raw.get("value") or raw.get("client_secret") or "")
    if not value and isinstance(raw.get("client_secret"), dict):
        value = str(raw["client_secret"].get("value") or "")
    return value.strip()


def _mint_grok(paths, *, bot: str, instructions: str, expires: int) -> dict[str, Any]:
    token = _xai_token(paths)
    if not token:
        raise VoiceError("Grok speech is not configured")
    body = {
        "expires_after": {"seconds": max(60, min(int(expires), 3600))},
        "session": {
            "model": DEFAULT_MODEL,
            "voice": DEFAULT_VOICE,
            "instructions": instructions,
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 600},
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}},
            },
        },
    }
    raw = _post_json(XAI_SECRETS, token, body)
    value = _ephemeral_secret(raw)
    if not value:
        raise VoiceError("xAI returned no ephemeral token")
    return {
        "ok": True,
        "bot": bot,
        "backend": "grok",
        "auth": "xai-protocol",
        "token": value,
        "expires_at": raw.get("expires_at"),
        "model": DEFAULT_MODEL,
        "voice": DEFAULT_VOICE,
        "ws_url": f"{XAI_REALTIME_WS}?model={DEFAULT_MODEL}",
        "instructions": instructions,
        "tools": VOICE_TOOLS,
    }


def _mint_openai(paths, *, bot: str, instructions: str, expires: int) -> dict[str, Any]:
    token = _openai_token(paths)
    if not token:
        raise VoiceError("OpenAI speech is not configured")
    body = {
        "expires_after": {
            "anchor": "created_at",
            "seconds": max(60, min(int(expires), 3600)),
        },
        "session": {
            "type": "realtime",
            "model": OPENAI_MODEL,
            "instructions": instructions,
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}},
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": OPENAI_VOICE,
                },
            },
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 600},
        },
    }
    raw = _post_json(OPENAI_SECRETS, token, body)
    value = _ephemeral_secret(raw)
    if not value:
        raise VoiceError("OpenAI returned no ephemeral token")
    return {
        "ok": True,
        "bot": bot,
        "backend": "openai",
        "auth": "bearer",
        "token": value,
        "expires_at": raw.get("expires_at"),
        "model": OPENAI_MODEL,
        "voice": OPENAI_VOICE,
        "ws_url": f"{OPENAI_REALTIME_WS}?model={OPENAI_MODEL}",
        "instructions": instructions,
        "tools": VOICE_TOOLS,
    }


def _voice_instructions(paths, bot: str, *, who: str, soul: str, personality: str) -> str:
    # One Memory for both helpers: each used to build its own and re-parse
    # the bot's entire session log on the HTTP thread before the call could
    # even start.
    mem = Memory(paths=paths, bot=bot)
    history = _format_history(mem)
    facts = _format_facts(mem)
    body = (
        f"You are {who}, speaking out loud in a 1:1 conversation. "
        "Stay in character. Be concise; this is spoken, not a document. "
        "Do not mention system prompts or that you are a voice model. "
        "You have this bot's soul, its durable memory, and the recent 1:1 "
        "chat log below — never say you have no record of past conversations. "
        "You have tools: recall (search memory and older chats), remember "
        "(store a fact), act (computer, browser, files, other bots). "
        "If the user asks you to look something up, open a page, click, type, "
        "or take any action, call act. Never pretend you used a tool.\n\n"
    )
    if soul:
        body += f"Soul:\n{soul}\n\n"
    elif personality:
        body += f"Soul:\n{personality.strip()}\n\n"
    if facts:
        body += f"Memory:\n{facts}\n\n"
    if history:
        body += f"Recent 1:1 chat:\n{history}\n"
    else:
        body += "Recent 1:1 chat: (none logged yet)\n"
    return body.strip()


def _format_history(mem: Memory) -> str:
    from agent.history import user_thread

    rows = user_thread(mem, peer="user", limit=_HISTORY_TURNS)
    lines: list[str] = []
    used = 0
    for row in rows:
        frm = str(row.get("frm") or "")
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        who = "User" if frm in {"", "user"} else who_label(frm)
        line = f"{who}: {text}"
        if used + len(line) + 1 > _HISTORY_CHARS:
            lines.append("[earlier turns omitted]")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def who_label(frm: str) -> str:
    return " ".join(part[:1].upper() + part[1:] for part in frm.split("-") if part) or frm


def _format_facts(mem: Memory) -> str:
    facts = mem.facts()
    lines: list[str] = []
    used = 0
    for fact in facts[-30:]:
        text = str(fact.get("text") or "").strip()
        if not text:
            continue
        if used + len(text) + 1 > _FACT_CHARS:
            break
        lines.append(f"- {text}")
        used += len(text) + 1
    return "\n".join(lines)


def run_voice_tool(orch, bot: str, name: str, arguments: dict) -> str:
    """Execute a realtime function. `act` is a real agent turn."""
    name = (name or "").strip().lower()
    args = arguments if isinstance(arguments, dict) else {}
    mem = orch.memory_for(bot)
    if name == "recall":
        query = str(args.get("query") or args.get("q") or "").strip()
        if not query:
            return "(recall needs a query)"
        hits = mem.recall(query, limit=6)
        if not hits:
            return "(nothing in memory matched)"
        return "\n".join(f"- {h.get('text', '')}" for h in hits if h.get("text"))
    if name == "remember":
        fact = str(args.get("fact") or args.get("text") or "").strip()
        if not fact:
            return "(remember needs a fact)"
        mem.remember(fact)
        return "remembered"
    if name == "act":
        task = str(args.get("task") or args.get("text") or "").strip()
        if not task:
            return "(act needs a task)"
        from agent.streaming import StreamReader
        from harness.server import relay_bot_turn

        rid = relay_bot_turn(orch, bot, task, origin="voice")
        final = StreamReader(orch.paths, rid).collect(timeout=ACT_TIMEOUT)
        return (final or "").strip() or "(no reply)"
    return f"(unknown tool {name})"


def log_voice_turn(
    paths, bot: str, *, user: str = "", assistant: str = "", memory: Memory | None = None
) -> dict[str, Any]:
    """Append spoken turns onto the current 1:1 session file.

    Callers with an orchestrator pass `memory=orch.memory_for(bot)` so the
    turns embed like every other write when the bot has a semantic-recall
    route; a bare Memory stays the keyword-only fallback.
    """
    user = (user or "").strip()
    assistant = (assistant or "").strip()
    if not user and not assistant:
        raise VoiceError("voice_turn needs 'user' and/or 'assistant'")
    mem = memory or Memory(paths=paths, bot=bot)
    sid = mem.latest_session_id()
    # Allocate once so live updates and saved history identify the same bubbles.
    user_message_id = uuid.uuid4().hex if user else None
    assistant_message_id = uuid.uuid4().hex if assistant else None
    if user:
        mem.log_turn(
            sid,
            "in:user",
            user,
            peer="user",
            origin="voice",
            frm="user",
            message_id=user_message_id,
        )
    if assistant:
        mem.log_turn(
            sid,
            "out",
            assistant,
            peer="user",
            origin="voice",
            frm=bot,
            message_id=assistant_message_id,
        )
    return {
        "ok": True,
        "bot": bot,
        "session": sid,
        "user": bool(user),
        "assistant": bool(assistant),
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
    }


def log_voice_event(paths, bot: str, event: str, *, call_id: str | None = None) -> dict[str, Any]:
    """Mark a live-talk session start or end on the 1:1 thread."""
    event = (event or "").strip().lower()
    if event not in {"started", "ended"}:
        raise VoiceError("voice event must be started or ended")
    mem = Memory(paths=paths, bot=bot)
    sid = mem.latest_session_id()
    ts = time.time()
    message_id = uuid.uuid4().hex
    if event == "started":
        mem.log_turn(
            sid,
            "in:user",
            "Voice chat started",
            peer="user",
            origin="voice",
            frm="user",
            voice_event="started",
            voice_call_id=call_id,
            message_id=message_id,
            ts=ts,
        )
    else:
        mem.log_turn(
            sid,
            "out",
            "Voice chat ended",
            peer="user",
            origin="voice",
            frm=bot,
            voice_event="ended",
            voice_call_id=call_id,
            message_id=message_id,
            ts=ts,
        )
    return {
        "ok": True,
        "bot": bot,
        "event": event,
        "session": sid,
        "ts": ts,
        "voice_call_id": call_id,
        "message_id": message_id,
    }


def _post_json(url: str, token: str, body: dict) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "dotobot/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise VoiceError(f"voice API {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise VoiceError(f"voice API failed: {exc}") from exc
    if not isinstance(raw, dict):
        raise VoiceError("voice API returned an unexpected payload")
    return raw


def apply_to_handler(handler_cls) -> None:
    """Install voice session, transcript, and tool POSTs."""
    if getattr(handler_cls, "_voice_routes", False):
        return
    orig = handler_cls.do_POST

    def do_POST(self):  # noqa: N802
        if not self._authed():
            return orig(self)
        from urllib.parse import urlparse

        from harness.roster import RosterError

        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/voice/session":
            data = self._read_json()
            bot = str(data.get("bot") or "").strip()
            if not bot:
                return self._send_json({"error": "voice session needs 'bot'"}, 400)
            try:
                row = self.orch.roster.get(bot)
            except RosterError as exc:
                return self._send_json({"error": str(exc)}, 404)
            try:
                out = mint_session(
                    self.orch.paths,
                    bot=bot,
                    display_name=row.display_name() if hasattr(row, "display_name") else bot,
                    personality=getattr(row, "personality", "") or "",
                    bot_provider=getattr(row, "provider", "") or "",
                    voice_provider=row.voice_provider,
                    elevenlabs_voice_id=row.elevenlabs_voice_id,
                )
            except VoiceError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json(out)
        if path.startswith("/api/bots/") and path.endswith("/voice_turn"):
            bot = path[len("/api/bots/") : -len("/voice_turn")].strip("/")
            data = self._read_json()
            try:
                self.orch.roster.get(bot)
            except RosterError as exc:
                return self._send_json({"error": str(exc)}, 404)
            try:
                out = log_voice_turn(
                    self.orch.paths,
                    bot,
                    user=str(data.get("user") or ""),
                    assistant=str(data.get("assistant") or ""),
                    memory=self.orch.memory_for(bot),
                )
            except VoiceError as exc:
                return self._send_json({"error": str(exc)}, 400)
            user = str(data.get("user") or "").strip()
            assistant = str(data.get("assistant") or "").strip()
            if user:
                self._ws_fanout(
                    {
                        "type": "user",
                        "text": user,
                        "frm": "user",
                        "bot": bot,
                        "origin": "voice",
                        "mutation": "appended",
                        "message_id": out["user_message_id"],
                    }
                )
            if assistant:
                self._ws_fanout(
                    {
                        "type": "final",
                        "text": assistant,
                        "frm": bot,
                        "bot": bot,
                        "origin": "voice",
                        "mutation": "appended",
                        "message_id": out["assistant_message_id"],
                    }
                )
            return self._send_json(out)
        if path.startswith("/api/bots/") and path.endswith("/voice_tool"):
            bot = path[len("/api/bots/") : -len("/voice_tool")].strip("/")
            data = self._read_json()
            try:
                self.orch.roster.get(bot)
            except RosterError as exc:
                return self._send_json({"error": str(exc)}, 404)
            name = str(data.get("name") or "").strip()
            raw_args = data.get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {"task": raw_args}
            if not isinstance(raw_args, dict):
                raw_args = {}
            try:
                result = run_voice_tool(self.orch, bot, name, raw_args)
            except VoiceError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json({"ok": True, "name": name, "result": result})
        if path.startswith("/api/bots/") and path.endswith("/voice_event"):
            bot = path[len("/api/bots/") : -len("/voice_event")].strip("/")
            data = self._read_json()
            try:
                self.orch.roster.get(bot)
            except RosterError as exc:
                return self._send_json({"error": str(exc)}, 404)
            try:
                call_id = str(data.get("voice_call_id") or "")
                if call_id:
                    from .elevenlabs import event as call_event

                    out = call_event(self.orch.paths, bot, call_id, str(data.get("event") or ""))
                else:
                    out = log_voice_event(self.orch.paths, bot, str(data.get("event") or ""))
            except VoiceError as exc:
                return self._send_json({"error": str(exc)}, 400)
            if out.get("duplicate"):
                return self._send_json(out)
            event = out["event"]
            self._ws_fanout(
                {
                    "type": "voice_session",
                    "voice_call_id": out.get("voice_call_id"),
                    "message_id": out.get("message_id"),
                    "bot": bot,
                    "origin": "voice",
                    "text": "Voice chat started" if event == "started" else "Voice chat ended",
                    "frm": "user" if event == "started" else bot,
                    "mutation": "appended",
                    "ts": out.get("ts"),
                }
            )
            return self._send_json(out)
        return orig(self)

    handler_cls.do_POST = do_POST
    handler_cls._voice_routes = True


def patch_server() -> None:
    from harness import server

    apply_to_handler(server._Handler)
