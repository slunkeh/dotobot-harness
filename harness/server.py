"""HTTP + SSE API for the harness (stdlib only).

This is the *native connection point* for remote clients (e.g. the SwiftUI macOS
app). The harness — orchestrator + bots + shared volume — runs wherever you like
(Docker, Raspberry Pi, a VM, or localhost) and exposes this small JSON/SSE API;
a client connects over the network. No shared filesystem is required on the
client side.

Endpoints (all under /api unless noted):
    GET  /health                      -> {ok, bots:[names]}
    GET|POST|PATCH|DELETE /bots[...]  -> roster CRUD (JSON store); POST queues
                                         the bot's welcome turn ({welcome:false} skips)
    GET|PUT /bots/<b>/soul            -> per-bot soul file
    GET|POST /bots/<b>/memory         -> per-bot facts
    GET  /bots/<b>/history            -> 1:1 transcript with the user
                                         (?before=<ts>&limit=200 pages older)
    GET  /bots/<b>/threads/<id>       -> parent + side-thread replies
    GET  /bots/<b>/queue              -> pending follow-ups (newest user chat first)
    GET  /bots/<b>/audit              -> the gate's authorization ledger, newest
                                         first (?limit=200&event=&decision=)
    GET  /logs                        -> tailable logs: the server's own (`server`)
                                         and one per bot (name, or bot:<name>)
    GET  /logs/<source>               -> last lines of one (?limit=200), or only
                                         what landed since ?offset=<byte>&gen=<id>
    GET  /logs/<source>/stream        -> text/event-stream: a `log` snapshot, then
                                         an `appended` frame per batch of new lines
                                         (?offset=&gen= resumes: no snapshot, only
                                         what landed since)
    POST /reports                     -> file a problem report (a flagged message,
                                         an app error); the server attaches its
                                         context (logs, bot state, stream, audit)
    GET  /reports[/<id>]              -> newest-first summaries (?limit=50) / one report
    DELETE /reports/<id>              -> remove one
    GET  /bots/<b>/queue              -> full waiting queue (running task excluded)
    PATCH /bots/<b>/queue {ids:[...]}  -> persist complete waiting order; stale IDs: 409
    DELETE /bots/<b>/queue/<id>       -> remove waiting work only; admitted item: 409
    POST /bots/<b>/restart            -> stop+spawn that bot (inbox kept)
    POST /chat            {bot,text}  -> text/event-stream of stream events
                                         (optional client_nonce: idempotent
                                         retry — same nonce+input replays the
                                         original accept)
    GET  /sends/<nonce>               -> recorded acceptance for that nonce
                                         ("did that send land?")
    POST /upload                      -> store an attachment in workspace/uploads
    GET  /uploads/<name>              -> fetch a previously uploaded file
    POST /transcribe                  -> speech-to-text (Grok STT / OpenAI Whisper)
    POST /secrets         {name,value} -> answer a secret_request (never echoed)
    POST /answers         {id,value}   -> answer an in-chat choice box
    GET  /prompts?bot=<b>             -> open secret/choice boxes (any client)
    GET  /blocks?bot=<b>&status=open  -> block instances (hydrate cards on load)
    GET  /blocks/catalog?bot=<b>      -> installed block definitions
    POST /block_actions   {block_id,action,values} -> submit/press on a block
    GET  /skills?bot=<b>              -> slash command/skill catalog
    GET  /workflows?bot=<b>           -> unified skills+routines catalog
                                         (source, trigger, per-bot enabled)
    POST /bots/<b>/workflows          -> create one; a trigger auto-routes it
                                         to a routine, none makes a private skill
    PATCH /bots/<b>/workflows/<id>    -> {enabled} per-bot enable/disable
    GET|POST|DELETE /rooms[...]       -> group chats (+ POST /rooms/<id>/messages)
    GET|PUT /receipts                 -> last-read timestamps per conversation
                                         (chat picker unread; shared Mac/iOS)
    GET  /providers                   -> provider catalog + configured state
                                         (+ per-provider usage totals & readiness)
    GET  /usage                       -> per-provider activity rollup
    POST|DELETE /providers/<id>/key   -> API key store
    POST /providers/<id>/oauth/start  -> grok/minimax device-code; claude PKCE
                                         (paste code); codex PKCE (localhost:1455)
    POST /providers/<id>/oauth/exchange -> finish PKCE (claude/codex)
    GET  /providers/<id>/oauth/status -> flow status
    DELETE /providers/<id>/oauth      -> sign out (delete tokens)
    GET|POST|PATCH|DELETE /connectors[...] -> connector config (+ /connectors/catalog);
                                              PATCH updates name/config/enabled_for
    GET  /recipes                     -> bundled bot-recipe catalog
    GET  /recipes/<id>                -> one recipe (soul, skills, memories)
    POST /recipes/<id>/install        -> create a bot from that recipe
                                         (omitted provider/model uses account default)
    GET|PATCH /settings               -> account LLM default (provider/model/reasoning)
    GET  /control/<bot>               -> control state
    GET  /control/<bot>/teach/record  -> live demonstration capture, or 404
    POST /control/<bot>/takeover|return|teach/*    -> control state
                                     (return: 403 when someone else holds it)
    POST /control/<bot>/teach/record/start|stop    -> start/stop screen capture;
                                     stop sends /learn-from-demonstration
    POST /send            {from,to,text}           -> {id}
    GET  /screen/<bot>                -> one-shot PNG (single host display)
    GET  /ws (no /api prefix)         -> WebSocket: chat, control, live screen + input

Auth: none by default (personal/localhost use). If a linking key or
$HARNESS_TOKEN is set, every request must send `Authorization: Bearer <token>`
(or `?token=`).
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from agent import blocks as blocklib
from agent import messaging
from agent.commands import catalog as skill_catalog
from agent.history import user_thread
from agent.soul import load_soul, save_soul
from agent.streaming import (
    StreamReader,
    answer_prompt,
    get_prompt,
    is_blocking,
    list_prompts,
    multiplex,
    read_steered,
    resolve_secret_prompts,
    sync_stale_prompts,
    write_answer,
)
from isolation import IsolationUnavailable
from providers import anthropic_oauth, available_providers, codex_oauth, minimax_oauth
from providers import codex_login as codex_cli_login
from providers.anthropic_oauth import OAuthError as AnthropicOAuthError
from providers.codex_oauth import OAuthError as CodexOAuthError
from providers.minimax_oauth import OAuthError as MiniMaxOAuthError
from providers.model_list import is_picker_model, models_for
from providers.xai_oauth import OAuthError
from providers.xai_oauth import start_login as start_grok_oauth
from providers.xai_oauth import status as grok_oauth_status

from . import audit as audit_log
from . import browser_idle, hostinput, logstream, machine_view, mcp_oauth, reports, sends, teachrec
from . import receipts as receiptslib
from . import ws as wsproto
from .agent_updater import start_agent_updater
from .colors import bot_color
from .connectors import Connectors
from .connectors import catalog as connector_catalog
from .connectors import mcp_url as connector_mcp_url
from .control import ControlDenied
from .dreaming import start_dream_scheduler
from .fsutil import write_atomic
from .linking import advertised_url, get_or_create_key, pairing_notice
from .mcp_oauth import OAuthError as MCPOAuthError
from .netguard import install_safe_redirects
from .orchestrator import Orchestrator
from .persona import PersonaError
from .prefs import (
    ConsentError,
    account_prefs,
    caveman_default,
    record_consents,
    set_caveman,
    set_content_filter,
    set_llm_defaults,
    set_user_avatar,
)
from .readiness import provider_readiness
from .recipes import RecipeError
from .recipes import catalog as recipe_catalog
from .recipes import get as get_recipe
from .recipes import install as install_recipe
from .redaction import register_secret
from .redaction import scrub as scrub_secrets
from .rooms import (
    MAX_ROOM_MEMBERS,
    MIN_ROOM_MEMBERS,
    RoomError,
    delete_room,
    get_room,
    recent_messages,
    save_room,
)
from .roster import RosterError
from .routines import (
    RoutineError,
    add_routine,
    list_routines,
    remove_routine,
    run_now,
    start_scheduler,
    update_routine,
)
from .routines import (
    public as routine_public,
)
from .screen import capture_png, stream_frames, unavailable_reason
from .secrets import (
    SecretNameError,
    delete_oauth,
    delete_secret,
    secret_source,
    set_secret,
    valid_secret_name,
)
from .transcribe import TranscribeError
from .transcribe import transcribe as transcribe_audio
from .usage import empty_usage, provider_totals
from .usage import rollup as usage_rollup
from .version import __version__
from .workflows import (
    WorkflowError,
    create_workflow,
    list_workflows,
    set_workflow_enabled,
)

SSE_CHAT_TIMEOUT = 120.0
#: Largest JSON body any route reads into memory, and the largest raw
#: upload / audio clip. Bigger bodies get a 413 and the connection closes
#: without the body being read.
MAX_JSON_BYTES = 4 << 20
MAX_UPLOAD_BYTES = 256 << 20


def http_timeout() -> float:
    """Idle limit (s) for one request's line, headers and body.

    An unauthenticated peer that connects and sends nothing — or one header
    byte a minute — used to hold a server thread and a file descriptor for
    ever; enough of them and the harness could not accept the Mac app, open
    a log file, or `docker exec`. BaseHTTPRequestHandler applies this to the
    socket in `setup()`; the WebSocket and SSE paths lift it again once the
    caller is authenticated, so long-lived streams are unaffected.
    """
    try:
        return max(1.0, float(os.environ.get("HARNESS_HTTP_TIMEOUT") or 30))
    except ValueError:
        return 30.0


def _same_secret(given: str, expected: bytes) -> bool:
    """Constant-time bearer comparison (never `==` on the linking key)."""
    return hmac.compare_digest(given.encode("utf-8", "surrogateescape"), expected)


class _Refused(Exception):
    """A response was already sent (413); the route must stop here."""


def _oauth_page(*, ok: bool, message: str) -> str:
    """The tiny page the browser shows after the OAuth redirect."""
    import html as _html

    title = "Connected" if ok else "Sign-in failed"
    mark = "&#10003;" if ok else "&#10007;"
    color = "#2e7d32" if ok else "#c62828"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title} — Dotobot</title></head>"
        "<body style='font-family:-apple-system,system-ui,sans-serif;display:flex;"
        "align-items:center;justify-content:center;height:90vh;margin:0'>"
        "<div style='text-align:center;max-width:28em'>"
        f"<div style='font-size:3em;color:{color}'>{mark}</div>"
        f"<h2 style='margin:.3em 0'>{title}</h2>"
        f"<p style='color:#555'>{_html.escape(message)}</p>"
        "</div></body></html>"
    )


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
#: Frames of a per-server log stream; fenced on their own `log:<source>` key.
_LOG_FRAME_TYPES = frozenset({"log", "log_started", "log_stopped", "log_heartbeat"})
#: Upload types a client may render inline; everything else downloads.
_INLINE_TYPES = re.compile(r"^(image|audio|video)/|^application/pdf$")


def _clean_quote(raw) -> dict | None:
    """Validate a reply-quote payload: {id, author, text}, capped."""
    if not isinstance(raw, dict):
        return None
    text = " ".join(str(raw.get("text") or "").split())[:300]
    if not text:
        return None
    return {
        "id": str(raw.get("id") or "")[:64],
        "author": str(raw.get("author") or "")[:80],
        "text": text,
    }


def _clean_id(raw, *, n: int = 64) -> str | None:
    """A routing selector (message_id / thread_id), never an auth token."""
    s = "".join(ch for ch in str(raw or "").strip() if ch.isalnum() or ch in "-_")
    return s[:n] if s else None


def app_release(paths) -> dict:
    """Operator-managed desktop-app release pins served to clients.

    $HARNESS_HOME/app_release.json is written by the fleet updater
    (deploy/updater.py); absent file means no pins — fields come back null.
    """
    try:
        data = json.loads((paths.home / "app_release.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    url = data.get("app_download_url")
    if not (isinstance(url, str) and url.lower().startswith("https://")):
        # Clients install whatever this names as code; never relay a link
        # a network attacker could rewrite.
        url = None
    digest = data.get("app_sha256")
    if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
        digest = None
    return {
        "min_app_version": data.get("min_app_version"),
        "latest_app_version": data.get("latest_app_version"),
        "app_download_url": url,
        "app_sha256": digest.lower() if digest else None,
    }


class EpochSequencer:
    """Boot-scoped epoch + per-stream-key monotonic sequence.

    Every JSON frame a client sees is stamped `{epoch, seq}`: `seq` climbs
    monotonically per stream key (`roster`, `bot:<name>`, `room:<id>`,
    `screen:<bot>`, `log:<source>`), so a client drops any frame with `seq <= last_seen` for
    the same epoch (duplicate or stale replay). A new epoch means a new
    server boot — sequence numbering restarted, resubscribe/reseed from
    scratch (the post-hello snapshot push covers that).

    Frames are stamped at send time (per socket write), so seq gaps within a
    key are normal — only ordering matters.
    """

    def __init__(self, epoch: str | None = None) -> None:
        self.epoch = epoch or uuid.uuid4().hex
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}

    def next(self, key: str) -> int:
        with self._lock:
            self._seq[key] = self._seq.get(key, 0) + 1
            return self._seq[key]

    @staticmethod
    def stream_key(frame: dict) -> str:
        room = frame.get("room")
        if room:
            return f"room:{room}"
        if frame.get("type") in ("screen_started", "screen_stopped"):
            return f"screen:{frame.get('bot') or ''}"
        if frame.get("type") in _LOG_FRAME_TYPES:
            return f"log:{frame.get('source') or ''}"
        # A user-line fan-out carries frm="user"; that is the speaker, not a
        # conversation. Key on the bot/room the line belongs to or it lands
        # on a phantom "bot:user" stream and the other app drops it.
        bot = frame.get("bot")
        if not bot or bot == "user":
            frm = frame.get("frm")
            if frm and frm != "user":
                bot = frm
            else:
                bot = None
        if bot:
            return f"bot:{bot}"
        return "roster"

    def stamp(self, frame: dict) -> dict:
        """Return a stamped copy; a frame that already carries an epoch is
        passed through untouched (never renumber someone else's fencing)."""
        if "epoch" in frame:
            return frame
        return {**frame, "epoch": self.epoch, "seq": self.next(self.stream_key(frame))}


class WSHub:
    """Live WebSocket clients. Routine / unsolicited bot turns broadcast here
    so a result is not stranded in the user inbox while the app sits idle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list = []
        self.push_relay = None

    def add(self, handler) -> None:
        with self._lock:
            if handler not in self._clients:
                self._clients.append(handler)

    def discard(self, handler) -> None:
        with self._lock:
            self._clients = [h for h in self._clients if h is not handler]

    def broadcast(self, frame: dict) -> None:
        if self.push_relay is not None:
            try:
                self.push_relay.enqueue(frame)
            except Exception:
                pass  # Push persistence must never interrupt chat delivery.
        with self._lock:
            clients = list(self._clients)
        dead = []
        for handler in clients:
            try:
                if handler._ws_send_json(frame) is False:
                    dead.append(handler)
            except Exception:
                dead.append(handler)
        if dead:
            with self._lock:
                self._clients = [h for h in self._clients if h not in dead]

    def shutdown(self, retry_in: float = 2.0) -> None:
        """Warn every client the server is going away (update/restart), then
        close each socket cleanly so the app sees a deliberate restart —
        an amber 'updating' state — instead of a dropped connection."""
        self.broadcast({"type": "shutdown", "reason": "update", "retry_in": retry_in})
        with self._lock:
            clients = list(self._clients)
        for handler in clients:
            try:
                with handler._send_lock:
                    wsproto.send_close(handler.wfile, wsproto.CLOSE_GOING_AWAY)
            except Exception:  # noqa: BLE001 - a dead socket must not block shutdown
                pass


# Curated provider list the UI can configure (credentials live in the harness
# secrets store, never in the client).
PROVIDERS = [
    {
        "id": "claude",
        "name": "Anthropic Claude",
        "auth": ["api_key", "oauth"],
        "implemented": True,
        "oauth_implemented": True,
    },
    {
        "id": "codex",
        "name": "OpenAI Codex",
        "auth": ["api_key", "oauth"],
        "implemented": True,
        "oauth_implemented": True,
    },
    {
        "id": "grok",
        "name": "xAI Grok",
        "auth": ["api_key", "oauth"],
        "implemented": True,
        "oauth_implemented": True,
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "auth": ["api_key"],
        "implemented": True,
    },
    {
        "id": "qwen",
        "name": "Qwen (DashScope)",
        "auth": ["api_key"],
        "implemented": True,
    },
    {
        "id": "glm",
        "name": "GLM (Z.AI)",
        "auth": ["api_key"],
        "implemented": True,
    },
    {
        "id": "kimi",
        "name": "Kimi (Moonshot)",
        "auth": ["api_key"],
        "implemented": True,
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "auth": ["api_key", "oauth"],
        "implemented": True,
        "oauth_implemented": True,
    },
]

# Internal / alias ids that must not appear as extra catalog rows.
_HIDDEN_PROVIDERS = frozenset(
    {
        "echo",
        "anthropic",
        "openai",
        "xai",
        "codex-chatgpt",
        "chatgpt",
        "zai",
        "minimax-oauth",
        "xai-oauth",
    }
)


def _oauth_backend(name: str) -> str | None:
    key = name.lower().strip("/")
    if key in {"grok", "xai", "xai-oauth"}:
        return "grok"
    if key in {"claude", "anthropic"}:
        return "claude"
    if key in {"codex", "openai", "codex-chatgpt", "chatgpt"}:
        return "codex"
    if key in {"minimax", "minimax-oauth"}:
        return "minimax"
    return None


class _Handler(BaseHTTPRequestHandler):
    orch: Orchestrator = None  # type: ignore[assignment]
    token: str | None = None
    protocol_version = "HTTP/1.1"  # needed for 101 upgrade + keep-alive

    # -- helpers ----------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter default logging
        return

    def _authed(self) -> bool:
        if not self.token:
            return True
        expected = self.token.encode("utf-8", "surrogateescape")
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and _same_secret(header[len("Bearer ") :], expected):
            return True
        # `?token=` is honoured on the WebSocket upgrade only: that is the
        # one request URLSessionWebSocketTask cannot reliably put a header
        # on (HarnessSocket.swift). Anywhere else a bearer in the URL ends up
        # in proxy access logs, browser history and shell history.
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/ws":
            return False
        qs = parse_qs(parsed.query)
        return _same_secret((qs.get("token") or [""])[0], expected)

    def _send_json(self, obj, status: int = 200) -> None:
        text = json.dumps(obj, ensure_ascii=False)
        if status >= 400:
            # Error payloads carry str(exc), which can echo a credential
            # (bad URLs, connector failures) — scrub them.
            text = scrub_secrets(text)
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status == 401:
            # Keep-alive after a refused request would let an unauthenticated
            # peer park on this thread until the idle timeout, for free.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, *, filename: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # Never let a browser guess a type, and never render an uploaded
        # document inline on the harness origin (an uploaded .html would
        # otherwise run script with the API's origin). Images, PDFs and
        # media stay inline — they are what chat tiles fetch.
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename is not None:
            disposition = "inline" if _INLINE_TYPES.match(content_type) else "attachment"
            safe = _SAFE_NAME.sub("_", filename) or "file"
            self.send_header("Content-Disposition", f'{disposition}; filename="{safe}"')
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self, limit: int) -> bytes:
        """The request body, capped: over `limit` answers 413 and raises
        _Refused so the route stops without a second response."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > limit:
            self.close_connection = True
            self._send_json({"error": f"request body over {limit} bytes"}, 413)
            raise _Refused()
        return self.rfile.read(length)

    def _read_json(self) -> dict:
        raw = self._read_body(MAX_JSON_BYTES)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _arm_keepalive(sock, seconds: float) -> None:
        """Make a half-open peer fail within about `seconds` instead of the
        kernel's retransmission horizon. A send timeout alone only fires once
        the send buffer is full, which a stalled reader may take days to
        reach at one heartbeat per interval."""
        import socket as _socket

        setsockopt = getattr(sock, "setsockopt", None)
        if setsockopt is None:
            return  # not a real socket (tests stub the connection)
        seconds = max(5.0, seconds)
        try:
            setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            idle = max(1, int(seconds / 3))
            for name, value in (
                ("TCP_KEEPIDLE", idle),
                ("TCP_KEEPINTVL", idle),
                ("TCP_KEEPCNT", 3),
            ):
                if hasattr(_socket, name):
                    setsockopt(_socket.IPPROTO_TCP, getattr(_socket, name), value)
            if hasattr(_socket, "TCP_USER_TIMEOUT"):
                setsockopt(_socket.IPPROTO_TCP, _socket.TCP_USER_TIMEOUT, int(seconds * 1000))
        except OSError:
            pass

    def _begin_sse(self) -> None:
        # Close the connection when the stream ends so clients (curl, URLSession)
        # get a clean end-of-response instead of waiting on keep-alive.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        # Authenticated and long-lived from here: lift the request timeout.
        self.connection.settimeout(None)

    def _stamp(self, frame: dict) -> dict:
        """Fence a frame with {epoch, seq}; no-op without a sequencer."""
        seq = getattr(self.orch, "sequencer", None)
        return frame if seq is None else seq.stamp(frame)

    def _sse(self, obj) -> bool:
        """Write one SSE frame; return False if the client went away.

        Any OSError counts: a half-open peer surfaces as a send timeout
        (TimeoutError) or ENOTCONN/EHOSTUNREACH, not only as a reset.
        """
        try:
            obj = self._stamp(obj)
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            return True
        except OSError:
            return False

    # -- routing ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path == "/ws":
            return self._websocket()
        if path == "/oauth/callback":
            # The browser lands here from the service's consent page; it
            # cannot carry the linking key, so the single-use OAuth `state`
            # is the check instead (validated in mcp_oauth.exchange).
            return self._oauth_callback()
        if not self._authed():
            return self._send_json({"error": "unauthorized"}, 401)
        if path == "/api/jev" or path.startswith("/api/jev/"):
            return self._jev("GET")
        if path.startswith("/api/voice/elevenlabs/"):
            return self._elevenlabs("GET")
        if path == "/api/health":
            return self._send_json(
                {
                    "ok": True,
                    "bots": self.orch.roster.names(),
                    "version": __version__,
                    "boot_id": getattr(self, "boot_id", None),
                    "capabilities": ["harness_updates_v1"],
                    "started_at": getattr(self, "started_at", None),
                    **app_release(self.orch.paths),
                }
            )
        if path == "/api/updates":
            from .updates import snapshot
            return self._send_json(snapshot(self.orch))
        if path == "/api/updates/preview":
            from .update_api import preview
            try:
                return self._send_json(preview(self.orch))
            except Exception as exc:
                return self._send_json({"error": scrub_secrets(str(exc))}, 503)
        if path == "/api/bots":
            return self._send_json(self._bots())
        if path == "/api/logs":
            return self._list_logs()
        if path.startswith("/api/logs/"):
            return self._get_log(path[len("/api/logs/") :].strip("/"))
        if path == "/api/reports":
            return self._list_reports()
        if path.startswith("/api/reports/"):
            return self._get_report(path[len("/api/reports/") :].strip("/"))
        if path == "/api/skills":
            return self._skills()
        if path == "/api/workflows":
            return self._workflows()
        if path == "/api/receipts":
            return self._send_json({"reads": receiptslib.load(self.orch.paths)})
        if path == "/api/rooms":
            return self._send_json([r.to_dict() for r in self.orch.rooms()])
        if path.startswith("/api/rooms/"):
            return self._get_room(path[len("/api/rooms/") :])
        if path.startswith("/api/streams/"):
            return self._get_stream(path[len("/api/streams/") :].strip("/"))
        if path.startswith("/api/sends/"):
            return self._get_send(path[len("/api/sends/") :].strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/soul"):
            return self._get_soul(path[len("/api/bots/") : -len("/soul")].strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/memory"):
            return self._get_memory(path[len("/api/bots/") : -len("/memory")].strip("/"))
        if path.startswith("/api/bots/") and "/threads/" in path:
            rest = path[len("/api/bots/") :]
            name, _, tid = rest.partition("/threads/")
            return self._get_thread(name.strip("/"), tid.strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/history"):
            return self._get_history(path[len("/api/bots/") : -len("/history")].strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/queue"):
            return self._get_queue(path[len("/api/bots/") : -len("/queue")].strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/audit"):
            return self._get_audit(path[len("/api/bots/") : -len("/audit")].strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/routines"):
            return self._get_routines(path[len("/api/bots/") : -len("/routines")].strip("/"))
        if path == "/api/providers":
            return self._send_json(self._providers())
        if path == "/api/voice":
            from harness.voice import voice_status

            return self._send_json(voice_status(self.orch.paths))
        if path == "/api/usage":
            return self._send_json(usage_rollup(self.orch.paths))
        if path == "/api/connectors":
            return self._send_json(Connectors(self.orch.paths).list())
        if path == "/api/connectors/catalog":
            return self._send_json(connector_catalog())
        if path == "/api/settings":
            return self._send_json(account_prefs(self.orch.paths))
        if path == "/api/recipes":
            return self._send_json(recipe_catalog())
        if path.startswith("/api/recipes/"):
            rid = path[len("/api/recipes/") :].strip("/")
            return self._get_recipe(rid)
        if path.startswith("/api/connectors/") and path.endswith("/oauth/status"):
            cid = path[len("/api/connectors/") : -len("/oauth/status")].strip("/")
            from . import delegated_oauth
            from .connectors import WORKSPACE_TYPES
            record = next((r for r in Connectors(self.orch.paths).list() if r.get("id") == cid), None)
            if record and record["type"] in WORKSPACE_TYPES:
                return self._send_json({"connector": cid, "status": "connected" if delegated_oauth.connected(self.orch.paths, cid) else "idle"})
            return self._send_json(mcp_oauth.status(self.orch.paths, cid))
        if path == "/api/prompts":
            return self._get_prompts()
        if path == "/api/blocks":
            return self._get_blocks()
        if path == "/api/blocks/catalog":
            return self._get_block_catalog()
        if path.startswith("/api/screen/"):
            return self._screen(path[len("/api/screen/") :])
        if path.startswith("/api/providers/") and path.endswith("/oauth/status"):
            name = path[len("/api/providers/") : -len("/oauth/status")].strip("/")
            return self._oauth_status(name)
        if path.startswith("/api/control/") and path.endswith("/teach/record"):
            bot = path[len("/api/control/") : -len("/teach/record")].strip("/")
            if not bot:
                return self._send_json({"error": "control needs a bot"}, 400)
            info = teachrec.status(self.orch.paths, bot)
            if info is None:
                return self._send_json({"error": "no demonstration is recording"}, 404)
            return self._send_json(info)
        if path.startswith("/api/control/"):
            bot = path[len("/api/control/") :]
            if bot:
                return self._send_json(asdict(self.orch.control.state(bot)))
        if path.startswith("/api/uploads/"):
            return self._get_upload(path[len("/api/uploads/") :])
        return self._send_json({"error": "not found", "path": path}, 404)

    def do_POST(self):  # noqa: N802
        try:
            return self._route_post()
        except _Refused:
            return None

    def _route_post(self):
        if not self._authed():
            return self._send_json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/jev" or path.startswith("/api/jev/"):
            return self._jev("POST")
        if path == "/api/push/subscriptions":
            relay = getattr(self.orch.ws_hub, "push_relay", None)
            if relay is None:
                return self._send_json({"error": "push_not_configured"}, 503)
            try:
                relay.register(self._read_json())
            except ValueError:
                return self._send_json({"error": "invalid_push_subscription"}, 400)
            return self._send_json({"registered": True})
        if path.startswith("/api/voice/elevenlabs/"):
            return self._elevenlabs("POST")
        if path in {"/api/updates/start", "/api/updates/retry"}:
            from . import update_api, updates
            try:
                if path.endswith("retry"):
                    result = updates.retry(self.orch)
                else:
                    result = update_api.start(self.orch, self._read_json().get("version"))
                return self._send_json(result, 202)
            except Exception as exc:
                return self._send_json({"error": scrub_secrets(str(exc))}, 409)
        if path == "/api/chat":
            return self._chat()
        if path == "/api/reports":
            return self._post_report()
        if path == "/api/upload":
            return self._upload()
        if path == "/api/transcribe":
            return self._transcribe()
        if path == "/api/secrets":
            return self._provide_secret()
        if path == "/api/answers":
            return self._provide_answer()
        if path == "/api/block_actions":
            return self._block_action()
        if path == "/api/send":
            return self._send()
        if path == "/api/rooms":
            return self._add_room()
        if path.startswith("/api/bots/") and path.endswith("/memory"):
            return self._add_memory(path[len("/api/bots/") : -len("/memory")].strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/soul"):
            return self._put_soul(path[len("/api/bots/") : -len("/soul")].strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/skills"):
            return self._add_skill(path[len("/api/bots/") : -len("/skills")].strip("/"))
        if path == "/api/bots":
            return self._add_bot()
        if path.startswith("/api/recipes/") and path.endswith("/install"):
            rid = path[len("/api/recipes/") : -len("/install")].strip("/")
            return self._install_recipe(rid)
        if path.startswith("/api/bots/") and path.endswith("/routines"):
            return self._add_routine(path[len("/api/bots/") : -len("/routines")].strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/workflows"):
            return self._add_workflow(path[len("/api/bots/") : -len("/workflows")].strip("/"))
        if path.startswith("/api/bots/") and "/routines/" in path and path.endswith("/run"):
            return self._run_routine(path[len("/api/bots/") :])
        if path.startswith("/api/bots/") and "/queue/" in path and path.endswith("/now"):
            rest = path[len("/api/bots/") : -len("/now")].strip("/")
            name, _, rid = rest.partition("/queue/")
            return self._send_now(name.strip("/"), rid.strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/restart"):
            return self._restart_bot(path[len("/api/bots/") : -len("/restart")].strip("/"))
        if path.startswith("/api/bots/") and path.endswith("/duplicate"):
            return self._duplicate_bot(path[len("/api/bots/") : -len("/duplicate")].strip("/"))
        if path == "/api/connectors":
            return self._add_connector()
        if path.startswith("/api/rooms/") and path.endswith("/messages"):
            rid = path[len("/api/rooms/") : -len("/messages")].strip("/")
            return self._post_room_message(rid)
        if path.startswith("/api/connectors/") and path.endswith("/delegated-auth"):
            cid = path[len("/api/connectors/") : -len("/delegated-auth")].strip("/")
            return self._install_delegated_auth(cid)
        if path == "/api/connectors/oauth/exchange":
            return self._connector_oauth_exchange()
        if path.startswith("/api/connectors/") and path.endswith("/oauth/start"):
            cid = path[len("/api/connectors/") : -len("/oauth/start")].strip("/")
            return self._connector_oauth_start(cid)
        if path.startswith("/api/providers/") and path.endswith("/key"):
            return self._set_provider_key(path[len("/api/providers/") : -len("/key")])
        if path.startswith("/api/providers/") and path.endswith("/oauth/start"):
            return self._oauth_start(path[len("/api/providers/") : -len("/oauth/start")])
        if path.startswith("/api/providers/") and path.endswith("/oauth/exchange"):
            return self._oauth_exchange(path[len("/api/providers/") : -len("/oauth/exchange")])
        if path.startswith("/api/bots/"):
            return self._update_bot(path[len("/api/bots/") :])
        if path.startswith("/api/control/"):
            return self._control(path[len("/api/control/") :])
        return self._send_json({"error": "not found", "path": path}, 404)

    def do_PATCH(self):  # noqa: N802
        try:
            return self._route_patch()
        except _Refused:
            return None

    def _route_patch(self):
        if not self._authed():
            return self._send_json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/api/bots/") and path.endswith("/queue"):
            return self._edit_queue(path[len("/api/bots/"):-len("/queue")].strip("/"))
        if path.startswith("/api/rooms/"):
            return self._patch_room(path[len("/api/rooms/") :])
        if path.startswith("/api/bots/") and path.endswith("/soul"):
            return self._put_soul(path[len("/api/bots/") : -len("/soul")].strip("/"))
        if "/routines/" in path and path.startswith("/api/bots/"):
            return self._patch_routine(path[len("/api/bots/") :])
        if "/workflows/" in path and path.startswith("/api/bots/"):
            return self._patch_workflow(path[len("/api/bots/") :])
        if path.startswith("/api/connectors/"):
            return self._patch_connector(path[len("/api/connectors/") :])
        if path.startswith("/api/bots/"):
            return self._update_bot(path[len("/api/bots/") :])
        if path == "/api/jev":
            return self._jev("PATCH")
        if path == "/api/voice":
            from harness.voice import VoiceError, set_voice_settings

            data = self._read_json()
            try:
                return self._send_json(set_voice_settings(self.orch.paths, data))
            except VoiceError as exc:
                return self._send_json({"error": str(exc)}, 400)
        if path == "/api/settings":
            data = self._read_json() or {}
            kwargs = {}
            if "default_provider" in data:
                kwargs["provider"] = data.get("default_provider")
            if "default_model" in data:
                kwargs["model"] = data.get("default_model")
            if "default_reasoning" in data:
                kwargs["reasoning"] = data.get("default_reasoning")
            if "user_avatar" in data:
                try:
                    set_user_avatar(self.orch.paths, str(data.get("user_avatar") or ""))
                except PersonaError as exc:
                    return self._send_json({"error": str(exc)}, 400)
            if kwargs:
                set_llm_defaults(self.orch.paths, **kwargs)
            if "caveman" in data:
                set_caveman(self.orch.paths, bool(data.get("caveman")))
                # Every bot that follows the account just changed its
                # effective flag: re-fan the roster so both apps repaint.
                self._push_lists(bots=True)
            if "content_filter" in data:
                set_content_filter(self.orch.paths, bool(data.get("content_filter")))
            if "consents" in data:
                try:
                    record_consents(self.orch.paths, data.get("consents"))
                except ConsentError as exc:
                    return self._send_json({"error": str(exc)}, 400)
            return self._send_json(account_prefs(self.orch.paths))
        return self._send_json({"error": "not found", "path": path}, 404)

    def do_PUT(self):  # noqa: N802
        try:
            return self._route_put()
        except _Refused:
            return None

    def _route_put(self):
        if not self._authed():
            return self._send_json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/api/bots/") and path.endswith("/soul"):
            return self._put_soul(path[len("/api/bots/") : -len("/soul")].strip("/"))
        if path == "/api/receipts":
            return self._put_receipts()
        return self._send_json({"error": "not found", "path": path}, 404)

    def do_DELETE(self):  # noqa: N802
        if not self._authed():
            return self._send_json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/jev" or path.startswith("/api/jev/"):
            return self._jev("DELETE")
        if path.startswith("/api/bots/") and "/queue/" in path:
            name, _, rid = path[len("/api/bots/"):].partition("/queue/")
            return self._edit_queue(name, remove_id=rid)
        if path.startswith("/api/voice/elevenlabs/"):
            return self._elevenlabs("DELETE")
        if path.startswith("/api/rooms/"):
            return self._delete_room(path[len("/api/rooms/") :])
        if path.startswith("/api/reports/"):
            return self._delete_report(path[len("/api/reports/") :].strip("/"))
        if "/routines/" in path and path.startswith("/api/bots/"):
            return self._delete_routine(path[len("/api/bots/") :])
        if path.startswith("/api/bots/"):
            return self._remove_bot(path[len("/api/bots/") :])
        if path.startswith("/api/providers/") and path.endswith("/key"):
            return self._delete_provider_key(path[len("/api/providers/") : -len("/key")])
        if path.startswith("/api/providers/") and path.endswith("/oauth"):
            return self._delete_provider_oauth(path[len("/api/providers/") : -len("/oauth")])
        if path.startswith("/api/connectors/") and path.endswith("/oauth"):
            cid = path[len("/api/connectors/") : -len("/oauth")].strip("/")
            return self._delete_connector_oauth(cid)
        if path.startswith("/api/connectors/"):
            return self._remove_connector(path[len("/api/connectors/") :])
        return self._send_json({"error": "not found", "path": path}, 404)

    # -- handlers ---------------------------------------------------------
    @classmethod
    def _bots(cls):
        handles = {h.bot: h for h in cls.orch.status()}
        statuses = {name: handle.status.value for name, handle in handles.items()}
        caveman_account = caveman_default(cls.orch.paths)
        out = []
        for b in cls.orch.bots():
            caveman_own = getattr(b, "caveman", None)
            busy, current_id = cls.orch.control.busy_state(b.name)
            q = messaging.queue_state(
                cls.orch.paths,
                b.name,
                busy=busy,
                current_id=current_id,
            )
            out.append(
                {
                    "name": b.name,
                    "title": b.title or b.display_name(),
                    "role": b.role,
                    "personality": b.personality,
                    "provider": b.provider,
                    "model": b.model,
                    "reasoning": getattr(b, "reasoning", "") or "",
                    "embeddings": getattr(b, "embeddings", "") or "",
                    "avatar": b.avatar,
                    "color": b.color or bot_color(b.name),
                    "dreaming": bool(getattr(b, "dreaming", False)),
                    # legacy key for app builds from the idle-think release
                    "idle_think": bool(getattr(b, "dreaming", False)),
                    "private_browser": bool(getattr(b, "private_browser", False)),
                    # Blocked by the owner: on the roster, never dispatched.
                    "blocked": bool(getattr(b, "blocked", False)),
                    # Caveman mode: the bot's own tri-state override (null =
                    # follow the account) and what that resolves to today.
                    "caveman": caveman_own,
                    "voice_provider": b.voice_provider,
                    "elevenlabs_voice_id": b.elevenlabs_voice_id,
                    "caveman_effective": (
                        caveman_account if caveman_own is None else bool(caveman_own)
                    ),
                    "status": statuses.get(b.name, "unknown"),
                    **(
                        {"startup_error": handles[b.name].meta["startup_error"]}
                        if b.name in handles and handles[b.name].meta.get("startup_error")
                        else {}
                    ),
                    "busy": q["busy"],
                    "queued": q["queued"],
                }
            )
        return out

    def _put_receipts(self):
        data = self._read_json()
        paths = self.orch.paths
        if isinstance(data.get("reads"), dict):
            reads = receiptslib.merge(paths, data["reads"])
            return self._send_json({"reads": reads})
        key = str(data.get("key") or "").strip()
        if not key:
            return self._send_json({"error": "receipts need 'key' and 'ts', or 'reads'"}, 400)
        try:
            ts = float(data.get("ts"))
        except (TypeError, ValueError):
            return self._send_json({"error": "ts must be a unix timestamp"}, 400)
        try:
            reads = receiptslib.set_read(paths, key, ts)
        except receiptslib.ReceiptError as exc:
            return self._send_json({"error": str(exc)}, 400)
        self._ws_fanout({"type": "receipt", "key": key, "ts": ts})
        return self._send_json({"reads": reads})

    def _ws_fanout(self, frame: dict) -> bool:
        """Write a chat frame to every live client (conversation sync).

        Routines already used hub.broadcast; user chats used to unicast back
        to the sender, so the other app never saw the thread until a re-link.
        The originating socket is in the hub, so it still receives the frame.
        """
        hub = getattr(self.orch, "ws_hub", None)
        if hub is not None:
            hub.broadcast(frame)
            return True
        return self._ws_send_json(frame) is not False

    def _jev(self, method: str):
        from . import jev

        path = urlparse(self.path).path.rstrip("/")
        try:
            data = self._read_json() if method in {"POST", "PATCH"} else {}
            if not isinstance(data, dict):
                raise jev.JevError("Expected a JSON object.")
            if path == "/api/jev" and method == "GET":
                result = jev.status(self.orch.paths)
            elif path == "/api/jev" and method == "PATCH":
                from .jev_features import configure
                result = configure(self.orch.paths, data)
            elif path == "/api/jev/key" and method == "POST":
                result = jev.connect(self.orch.paths, data.get("key"))
            elif path == "/api/jev/key" and method == "DELETE":
                result = jev.disconnect(self.orch.paths)
            elif path == "/api/jev/test" and method == "POST":
                result = jev.test(self.orch.paths)
            else:
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(result)
        except jev.JevError as exc:
            return self._send_json({"error": str(exc)}, 400)

    def _elevenlabs(self, method: str):
        from . import elevenlabs
        from .voice import VoiceError

        path = urlparse(self.path).path.rstrip("/")
        try:
            if method == "GET" and path == "/api/voice/elevenlabs/voices":
                query = parse_qs(urlparse(self.path).query)
                result = elevenlabs.voices(
                    self.orch.paths,
                    (query.get("search") or [""])[0],
                    (query.get("next_page_token") or [""])[0],
                )
            elif path == "/api/voice/elevenlabs/key" and method == "POST":
                result = elevenlabs.connect(self.orch.paths, self._read_json().get("key"))
            elif path == "/api/voice/elevenlabs/key" and method == "DELETE":
                result = elevenlabs.disconnect(self.orch.paths)
            elif path == "/api/voice/elevenlabs/token" and method == "POST":
                data = self._read_json()
                result = elevenlabs.connection(
                    self.orch.paths,
                    str(data.get("bot") or ""),
                    str(data.get("call_id") or ""),
                    str(data.get("token_type") or ""),
                )
            else:
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(result)
        except VoiceError as exc:
            return self._send_json({"error": str(exc)}, 400)

    def _voice_chat(self, data: dict, bot: str, room: str, thread_id: str | None) -> str | None:
        from .elevenlabs import call
        from .voice import VoiceError

        call_id = data.get("voice_call_id")
        if call_id is None:
            return None
        if (
            not isinstance(call_id, str)
            or not call_id
            or room
            or thread_id
            or data.get("attachments")
        ):
            raise VoiceError("Voice input needs a call ID and a direct bot chat.")
        if not _clean_id(data.get("message_id")) or not data.get("client_nonce"):
            raise VoiceError("Voice input needs a stable message ID and retry nonce.")
        call(self.orch.paths, bot, call_id)
        return call_id

    def _announce_user(
        self,
        *,
        text: str,
        bot: str | None,
        room: str | None,
        attachments: list | None = None,
        quote: dict | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
        frm: str = "user",
        voice_call_id: str | None = None,
    ) -> None:
        """Tell every client this line was just sent, before the stream.

        `frm` is a roster bot when the line is a bot's own post into a
        group (`message_room`); clients key their bubble side on it.
        """
        frame = {"type": "user", "text": text, "frm": frm or "user", "mutation": "appended"}
        if bot:
            frame["bot"] = bot
        if room:
            frame["room"] = room
        if attachments:
            frame["attachments"] = attachments
        if quote:
            frame["quote"] = quote
        if thread_id:
            frame["thread_id"] = thread_id
        if message_id:
            frame["message_id"] = message_id
        if voice_call_id:
            frame.update(origin="voice", voice_call_id=voice_call_id)
        self._ws_fanout(frame)

    def _announce_secret_saved(self, name: str) -> None:
        """Tell every client the box settled. Never include the value."""
        bots = {
            str(row.get("bot") or "").strip()
            for row in list_prompts(self.orch.paths, include_resolved=True)
            if row.get("type") == "secret_request" and str(row.get("name") or "") == name
        }
        bots.discard("")
        frame = {"type": "secret_saved", "name": name, "secret_provided": True}
        if len(bots) == 1:
            frame["bot"] = next(iter(bots))
        self._ws_fanout(frame)

    def _commit_prompt_resolution(self, live: dict, answer_id: str, resolution: dict) -> None:
        """Fan the settled pick to every client and persist it on the 1:1 log.

        The waiting tool also re-emits + re-logs when its wait ends; coalescing
        by card_id makes that a no-op. Doing it here is what lets a second
        phone see the tick before the bot's next token, and what GET /history
        reads if that wait never runs.
        """
        self._announce_prompt_resolution(live, answer_id, resolution)
        self._persist_prompt_resolution(live, answer_id, resolution)

    def _refresh_task_prompts(self, bot: str | None = None) -> None:
        for row in sync_stale_prompts(self.orch.paths, bot):
            self._announce_prompt_resolution(row, row["id"], row["resolution"])

    def _announce_prompt_resolution(self, live: dict, answer_id: str, resolution: dict) -> None:
        card_type, payload, bot, room = _prompt_card_shape(live)
        if not card_type:
            return
        frame = {
            "type": "card",
            "id": answer_id,
            "card_type": card_type,
            "payload": payload,
            "resolution": dict(resolution),
            "mutation": "updated",
        }
        if bot:
            frame["bot"] = bot
        if room:
            frame["room"] = room
        self._ws_fanout(frame)

    def _persist_prompt_resolution(self, live: dict, answer_id: str, resolution: dict) -> None:
        card_type, payload, bot, room = _prompt_card_shape(live)
        if not bot or not card_type:
            return
        if room:
            # Room cards stay off the 1:1 log — same invariant as
            # `_persist_card`. The room transcript keeps the settled box.
            from .rooms import append_card

            try:
                append_card(
                    self.orch.paths,
                    room,
                    frm=bot,
                    card_id=answer_id,
                    card_type=card_type,
                    payload=payload,
                    resolution=resolution,
                )
            except (RoomError, OSError):
                pass
            return
        mem = self.orch.memory_for(bot)
        mem.log_card(
            mem.latest_session_id(),
            card_id=answer_id,
            card_type=card_type,
            payload=payload,
            frm=bot,
            resolution=resolution,
        )

    def _push_lists(self, *, bots: bool = False, rooms: bool = False) -> None:
        """Fan a full roster / room snapshot to every live client.

        Clients replace their in-memory lists on these frames, so a create
        on iOS shows on desktop without a relaunch (and the reverse).
        """
        hub = getattr(self.orch, "ws_hub", None)
        if hub is None:
            return
        if bots:
            hub.broadcast({"type": "bots", "bots": self._bots(), "mutation": "snapshot"})
        if rooms:
            hub.broadcast(
                {
                    "type": "rooms",
                    "rooms": [r.to_dict() for r in self.orch.rooms()],
                    "mutation": "snapshot",
                }
            )

    def _providers(self):
        # Per-provider activity totals (local bookkeeping, not vendor billing)
        # plus the readiness probe.
        usage = provider_totals(self.orch.paths)
        out = []
        for p in PROVIDERS:
            configured = secret_source(p["id"], self.orch.paths) is not None
            if p["id"] == "codex" and not configured:
                # A Codex CLI ChatGPT login on the host counts.
                configured = codex_cli_login.login_available()
            if configured:
                live_models, live_reasoning = models_for(p["id"], self.orch.paths)
            else:
                live_models, live_reasoning = [], []
            for bot in self.orch.bots():
                if bot.provider != p["id"]:
                    continue
                mid = str(bot.model or "").strip()
                if mid and mid not in live_models and is_picker_model(p["id"], mid):
                    live_models.append(mid)
            out.append(
                {
                    **p,
                    "models": live_models,
                    "reasoning": live_reasoning,
                    "configured": configured,
                    "usage": usage.get(p["id"], empty_usage()),
                    "readiness": provider_readiness(p["id"], self.orch.paths),
                }
            )
        # surface any other registered providers too
        known = {p["id"] for p in PROVIDERS}
        for name in available_providers():
            # aliases / test-only adapters of catalog entries are not re-listed
            if name not in known and name not in _HIDDEN_PROVIDERS:
                out.append(
                    {
                        "id": name,
                        "name": name,
                        "auth": ["api_key"],
                        "implemented": True,
                        "models": [],
                        "configured": secret_source(name, self.orch.paths) is not None,
                        "usage": usage.get(name, empty_usage()),
                        "readiness": provider_readiness(name, self.orch.paths),
                    }
                )
        return out

    def _provide_secret(self):
        """Answer a bot's secret_request: store the value, never echo it."""
        data = self._read_json()
        name = _SAFE_NAME.sub("", str(data.get("name", "")).strip())
        value = str(data.get("value", ""))
        if not name or not value.strip():
            return self._send_json({"error": "secrets need 'name' and 'value'"}, 400)
        if not valid_secret_name(name):
            return self._send_json({"error": "invalid secret name"}, 400)
        try:
            set_secret(name, value, self.orch.paths)
        except SecretNameError:
            return self._send_json({"error": "invalid secret name"}, 400)
        # Settle (not delete) the open boxes so the reseed renders them as
        # answered; only `secret_provided` is recorded, never the value.
        resolve_secret_prompts(self.orch.paths, name)
        self._announce_secret_saved(name)
        return self._send_json({"name": name, "configured": True})

    def _provide_answer(self):
        """Apply a durable decision once; retries never become new user turns."""
        data = self._read_json()
        answer_id = str(data.get("id", "")).strip()
        value = str(data.get("value", "")).strip()
        bot = str(data.get("bot", "")).strip()
        if not answer_id or not value:
            return self._send_json({"error": "answers need 'id' and 'value'"}, 400)
        live = get_prompt(self.orch.paths, answer_id)
        control_answer = self._answer_control_return(
            answer_id, value, bot, live, str(data.get("user") or "").strip() or None
        )
        if control_answer is not None:
            return control_answer
        status, row = answer_prompt(self.orch.paths, answer_id, value, bot=bot)
        if status in {"applied", "replayed"}:
            if status == "applied":
                self._commit_prompt_resolution(row, answer_id, row["resolution"])
            return self._send_json(
                {
                    "id": answer_id,
                    "ok": True,
                    "parked": True,
                    "replayed": status == "replayed",
                    "resolution": row["resolution"],
                }
            )
        if status in {"conflict", "stale"}:
            if status == "stale" and (row or {}).get("resolution"):
                self._refresh_task_prompts(str(row.get("bot") or "") or None)
            return self._send_json(
                {
                    "id": answer_id,
                    "error": "prompt_" + status,
                    "resolution": (row or {}).get("resolution"),
                },
                409,
            )
        # An answer always belongs to its question, including free text. Turning
        # a missing question into chat loses the sender's identity and can repeat
        # work on retries. Clients retain the text when this request is rejected.
        return self._send_json({"id": answer_id, "error": "prompt_expired"}, 410)

    def _answer_control_return(self, answer_id: str, value: str, bot: str, live, by: str | None):
        """Apply an accept/dismiss on a `control_return` card.

        The control flip happens here, not only in the waiting tool, so the
        card still works when the agent has moved on or the app reconnected.
        Returns a response when it handled the answer, else None.
        """
        target = str((live or {}).get("bot") or "").strip() or bot
        card = str((live or {}).get("card_type") or "").strip()
        ctrl = self.orch.control
        if not target:
            return None
        state = ctrl.state(target)
        if card != "control_return" and state.return_request_id != answer_id:
            return None
        if live is not None and bot and bot != target:
            return self._send_json({"error": "prompt_conflict"}, 409)
        accepted = value.strip().lower() in ("accept", "confirm", "yes")
        if accepted and not ctrl.can_return(target, by):
            return self._send_json(
                {"error": f"only the holder can return control of {target}"}, 403
            )
        if (
            live
            and not live.get("resolution")
            and state.paused
            and state.return_request_id != answer_id
        ):
            return self._send_json({"error": "prompt_stale"}, 409)
        if live is None:
            from agent.streaming import write_prompt

            write_prompt(
                self.orch.paths,
                {
                    "id": answer_id,
                    "bot": target,
                    "type": "card",
                    "card_type": "control_return",
                    "payload": {},
                },
            )
        status, row = answer_prompt(self.orch.paths, answer_id, value, bot=target)
        if status not in {"applied", "replayed"}:
            return self._send_json({"error": "prompt_" + status}, 409)
        # A replay may finish a crash-interrupted return, but an old card can
        # never return a subsequently acquired desktop with a different request.
        if status == "applied" or state.return_request_id == answer_id:
            if accepted:
                try:
                    ctrl.return_control(target, by=by)
                except ControlDenied as exc:
                    return self._send_json({"error": str(exc)}, 403)
            else:
                ctrl.decline_return(target)
        if status == "applied":
            self._commit_prompt_resolution(row, answer_id, row["resolution"])
        return self._send_json(
            {
                "id": answer_id,
                "ok": True,
                "parked": live is not None,
                "replayed": status == "replayed",
                "mode": ctrl.state(target).mode,
            }
        )

    def _provider_id(self, name: str) -> str | None:
        """A provider id names a file under credentials/; anything shaped
        like a path is a 400, never a lookup."""
        name = name.strip("/")
        if not valid_secret_name(name):
            self._send_json({"error": "invalid provider id"}, 400)
            return None
        return name

    def _set_provider_key(self, name: str):
        data = self._read_json()
        name = self._provider_id(name)
        if name is None:
            return None
        key = str(data.get("api_key", "")).strip()
        if not key:
            return self._send_json({"error": "api_key required"}, 400)
        try:
            set_secret(name, key, self.orch.paths)
        except SecretNameError:
            return self._send_json({"error": "invalid provider id"}, 400)
        return self._send_json({"id": name, "configured": True})

    def _delete_provider_key(self, name: str):
        name = self._provider_id(name)
        if name is None:
            return None
        removed = delete_secret(name, self.orch.paths)
        configured = secret_source(name, self.orch.paths) is not None
        return self._send_json({"id": name, "configured": configured, "removed": removed})

    def _delete_provider_oauth(self, name: str):
        name = self._provider_id(name)
        if name is None:
            return None
        removed = delete_oauth(name, self.orch.paths)
        configured = secret_source(name, self.orch.paths) is not None
        return self._send_json({"id": name, "configured": configured, "removed": removed})

    def _oauth_start(self, name: str):
        # Always consume the POST body so keep-alive reuse cannot glue it onto
        # the next request-line (the Mac client sends `{}`; leftover bytes
        # become `{}GET` → Python 501 Unsupported method).
        self._read_json()
        key = name.lower().strip("/")
        backend = _oauth_backend(key)
        try:
            if backend == "grok":
                return self._send_json(start_grok_oauth(self.orch.paths, provider="grok"))
            if backend == "claude":
                return self._send_json(
                    anthropic_oauth.start_login(self.orch.paths, provider="claude")
                )
            if backend == "codex":
                return self._send_json(codex_oauth.start_login(self.orch.paths, provider="codex"))
            if backend == "minimax":
                return self._send_json(
                    minimax_oauth.start_login(self.orch.paths, provider="minimax")
                )
        except (OAuthError, AnthropicOAuthError, CodexOAuthError, MiniMaxOAuthError) as exc:
            return self._send_json(
                {"provider": key, "status": "error", "message": str(exc)},
                502,
            )
        return self._send_json(
            {
                "provider": key,
                "status": "unavailable",
                "message": "OAuth is not configured yet; use an API key for now.",
            },
            501,
        )

    def _oauth_status(self, name: str):
        key = name.lower().strip("/")
        backend = _oauth_backend(key)
        if backend == "grok":
            return self._send_json(grok_oauth_status("grok", self.orch.paths))
        if backend == "claude":
            return self._send_json(anthropic_oauth.status("claude", self.orch.paths))
        if backend == "codex":
            return self._send_json(codex_oauth.status("codex", self.orch.paths))
        if backend == "minimax":
            return self._send_json(minimax_oauth.status("minimax", self.orch.paths))
        return self._send_json(
            {
                "provider": key,
                "status": "unavailable",
                "message": "OAuth is not configured yet; use an API key for now.",
            },
            501,
        )

    def _oauth_exchange(self, name: str):
        data = self._read_json() or {}
        key = name.lower().strip("/")
        code = str(data.get("code") or "").strip()
        state = str(data.get("state") or "").strip() or None
        raw_tokens = data.get("tokens")
        tokens = raw_tokens if isinstance(raw_tokens, dict) else None
        if not code and not (tokens and tokens.get("access_token")):
            return self._send_json({"error": "code required"}, 400)
        backend = _oauth_backend(key)
        try:
            if backend == "claude":
                return self._send_json(
                    anthropic_oauth.exchange(
                        self.orch.paths, code, tokens=tokens, provider="claude"
                    )
                )
            if backend == "codex":
                return self._send_json(
                    codex_oauth.exchange(self.orch.paths, code, state=state, provider="codex")
                )
        except (AnthropicOAuthError, CodexOAuthError) as exc:
            status = getattr(exc, "status", None)
            http = 429 if status == 429 else 400 if status == 400 else 502
            return self._send_json(
                {"provider": key, "status": "error", "message": str(exc)},
                http,
            )
        return self._send_json(
            {
                "provider": key,
                "status": "unavailable",
                "message": "This provider does not use a pasted authorization code.",
            },
            501,
        )

    def _restart_bot(self, name: str):
        """Stop and spawn one bot. Inbox / queue files are not touched."""
        try:
            self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        try:
            self.orch.restart(name, wait=False)
        except IsolationUnavailable as exc:
            return self._send_json({"error": str(exc)}, 503)
        bot = self.orch.roster.get(name)
        restart = self.orch._restarts.get(name)
        self._send_json({**bot.to_dict(), "status": restart[0].value if restart else "restarting"})
        self._push_lists(bots=True)

    def _add_bot(self):
        data = self._read_json()
        start = bool(data.pop("start", True))
        welcome = bool(data.pop("welcome", True))
        try:
            bot = self.orch.add_bot(start=False, **data)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 400)
        startup = {"status": "stopped"}
        if start:
            try:
                startup = self.orch.start_created_bot(
                    bot.name, welcome=welcome, on_finished=lambda: self._push_lists(bots=True)
                )
            except RosterError as exc:
                # A concurrent delete won between persistence and registration.
                return self._send_json({"error": str(exc)}, 409)
        # Creation is durable now. Machine provisioning and full-fleet status
        # probes must not outlive the tool/app's HTTP deadline before this ack.
        self._send_json({**bot.to_dict(), **startup})
        self._push_lists(bots=True)

    def _duplicate_bot(self, source: str):
        data = self._read_json()
        try:
            bot = self.orch.duplicate_bot(source, name=str(data.get("name") or "") or None)
        except RosterError as exc:
            msg = str(exc).lower()
            code = 404 if "no bot named" in msg else 400
            return self._send_json({"error": str(exc)}, code)
        self._push_lists(bots=True)
        return self._send_json(bot.to_dict())

    def _get_recipe(self, recipe_id: str):
        try:
            return self._send_json(get_recipe(recipe_id))
        except RecipeError as exc:
            return self._send_json({"error": str(exc)}, 404)

    def _install_recipe(self, recipe_id: str):
        data = self._read_json() or {}
        kwargs: dict = {}
        if "provider" in data:
            kwargs["provider"] = str(data.get("provider") or "")
        if "model" in data:
            kwargs["model"] = str(data.get("model") or "")
        if "reasoning" in data:
            kwargs["reasoning"] = str(data.get("reasoning") or "")
        try:
            bot = install_recipe(
                self.orch,
                recipe_id,
                name=str(data.get("name") or data.get("title") or ""),
                **kwargs,
            )
        except RecipeError as exc:
            return self._send_json({"error": str(exc)}, 404)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 400)
        self._push_lists(bots=True)
        # Same shape as GET /api/bots. bot.to_dict() has no `status`, and the
        # iOS/Mac RecipeCard decodes the install body as Bot — missing status
        # surfaces as "The data couldn't be read because it is missing." The
        # bot is already on the roster, so a second tap looks like success.
        name = bot.get("name") if isinstance(bot, dict) else recipe_id
        row = next((b for b in self._bots() if b["name"] == name), None)
        if row is None:
            row = dict(bot)
            row.setdefault("status", "unknown")
        return self._send_json(row)

    def _update_bot(self, name: str):
        data = self._read_json()
        try:
            bot = self.orch.update_bot(name, **data)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        self._push_lists(bots=True)
        return self._send_json(bot.to_dict())

    def _remove_bot(self, name: str):
        try:
            job = self.orch.remove_bot(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        try:
            self._send_json({"removed": name, "purge_after": job["purge_after"]})
        finally:
            self._push_lists(bots=True)

    def _get_routines(self, bot: str):
        try:
            self.orch.roster.get(bot)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        try:
            rows = list_routines(self.orch.paths, bot)
        except RoutineError as exc:
            return self._send_json({"error": str(exc)}, 500)
        return self._send_json([routine_public(r) for r in rows])

    def _add_routine(self, bot: str):
        try:
            self.orch.roster.get(bot)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        data = self._read_json()
        once_raw = data.get("once_at")
        once_at = None
        if once_raw not in (None, ""):
            try:
                once_at = float(once_raw)
            except (TypeError, ValueError):
                return self._send_json({"error": f"could not parse once_at {once_raw!r}"}, 400)
        enabled = bool(data["enabled"]) if "enabled" in data else None
        try:
            row = add_routine(
                self.orch.paths,
                bot,
                title=str(data.get("title") or ""),
                prompt=str(data.get("prompt") or ""),
                when=str(data.get("time") or data.get("when") or data.get("cron") or ""),
                enabled=enabled,
                once_at=once_at,
                timezone=str(data.get("timezone") or ""),
            )
        except RoutineError as exc:
            return self._send_json({"error": str(exc)}, 400)
        return self._send_json(row)

    def _routine_bot_id(self, rest: str) -> tuple[str, str] | None:
        parts = [p for p in rest.split("/") if p]
        if len(parts) >= 3 and parts[1] == "routines":
            return parts[0], parts[2]
        return None

    def _patch_routine(self, rest: str):
        parsed = self._routine_bot_id(rest)
        if not parsed:
            return self._send_json({"error": "not found", "path": rest}, 404)
        bot, rid = parsed
        try:
            self.orch.roster.get(bot)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        data = self._read_json()
        try:
            row = update_routine(self.orch.paths, bot, rid, **data)
        except RoutineError as exc:
            return self._send_json({"error": str(exc)}, 404)
        return self._send_json(row)

    def _delete_routine(self, rest: str):
        parsed = self._routine_bot_id(rest)
        if not parsed:
            return self._send_json({"error": "not found", "path": rest}, 404)
        bot, rid = parsed
        try:
            removed = remove_routine(self.orch.paths, bot, rid)
        except RoutineError as exc:
            return self._send_json({"error": str(exc)}, 500)
        if not removed:
            return self._send_json({"error": f"no routine {rid!r}"}, 404)
        return self._send_json({"removed": rid})

    def _run_routine(self, rest: str):
        parsed = self._routine_bot_id(rest)
        if not parsed:
            return self._send_json({"error": "not found", "path": rest}, 404)
        bot, rid = parsed
        try:
            self.orch.roster.get(bot)
            if self.orch.is_blocked(bot):
                return self._send_json({"error": f"{bot} is blocked; unblock it first"}, 409)
            row = run_now(
                self.orch.paths,
                bot,
                rid,
                send=lambda b, t, **kw: relay_bot_turn(self.orch, b, t, **kw),
            )
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        except RoutineError as exc:
            return self._send_json({"error": str(exc)}, 400)
        return self._send_json(row)

    # -- workflows (unified skills + routines) --------------------
    def _workflows(self):
        qs = parse_qs(urlparse(self.path).query)
        bot = (qs.get("bot", [None])[0] or "").strip()
        if bot:
            try:
                self.orch.roster.get(bot)
            except RosterError as exc:
                return self._send_json({"error": str(exc)}, 404)
        else:
            names = self.orch.roster.names()
            bot = names[0] if names else ""
        if not bot:
            return self._send_json([])
        try:
            rows = list_workflows(self.orch.paths, bot)
        except RoutineError as exc:
            return self._send_json({"error": str(exc)}, 500)
        return self._send_json([w.to_dict() for w in rows])

    def _add_workflow(self, bot: str):
        try:
            self.orch.roster.get(bot)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        data = self._read_json()
        try:
            workflow = create_workflow(
                self.orch.paths,
                bot,
                name=str(data.get("name") or ""),
                description=str(data.get("description") or ""),
                body=str(data.get("body") or ""),
                when_to_use=str(data.get("when_to_use") or ""),
                trigger=str(data.get("trigger") or data.get("time") or data.get("when") or ""),
            )
        except (WorkflowError, RoutineError) as exc:
            return self._send_json({"error": str(exc)}, 400)
        return self._send_json(workflow.to_dict())

    def _patch_workflow(self, rest: str):
        parts = [p for p in rest.split("/") if p]
        if len(parts) < 3 or parts[1] != "workflows":
            return self._send_json({"error": "not found", "path": rest}, 404)
        bot, wid = parts[0], parts[2]
        try:
            self.orch.roster.get(bot)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        data = self._read_json()
        enabled = data.get("enabled")
        if enabled is None:
            return self._send_json({"error": "enabled (true/false) required"}, 400)
        try:
            workflow = set_workflow_enabled(self.orch.paths, bot, wid, bool(enabled))
        except (WorkflowError, RoutineError) as exc:
            return self._send_json({"error": str(exc)}, 404)
        return self._send_json(workflow.to_dict())

    def _add_connector(self):
        data = self._read_json()
        try:
            record = Connectors(self.orch.paths).add(
                str(data.get("type", "")),
                str(data.get("name", "")),
                data.get("config") or {},
                data.get("secret"),
                enabled_for=data.get("enabled_for"),
            )
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 400)
        return self._send_json(record)

    def _patch_connector(self, connector_id: str):
        data = self._read_json()
        kwargs = {}
        if "name" in data:
            kwargs["name"] = str(data.get("name") or "")
        if "config" in data:
            kwargs["config"] = data.get("config") or {}
        if "enabled_for" in data:
            kwargs["enabled_for"] = data.get("enabled_for")
        try:
            record = Connectors(self.orch.paths).update(connector_id, **kwargs)
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 400)
        if record is None:
            return self._send_json({"error": f"no connector {connector_id!r}"}, 404)
        return self._send_json(record)

    def _remove_connector(self, connector_id: str):
        try:
            removed = Connectors(self.orch.paths).remove(connector_id)
        except MCPOAuthError as exc:
            return self._send_json({"error": str(exc)}, 502)
        return self._send_json({"removed": connector_id, "ok": removed})

    # -- connector OAuth (remote MCP servers) --------------------------------

    def _send_html(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _connector_oauth_start(self, connector_id: str):
        data = self._read_json()
        record = next(
            (r for r in Connectors(self.orch.paths).list() if r.get("id") == connector_id),
            None,
        )
        if record is None:
            return self._send_json({"error": f"no connector {connector_id!r}"}, 404)
        from .connectors import WORKSPACE_TYPES
        if record.get("type") in WORKSPACE_TYPES:
            return self._send_json({"status": "unavailable", "message": "Connect this account through Dotobot's account service. Update your app if needed."}, 409)
        url = connector_mcp_url(str(record.get("type", "")), record.get("config"))
        if not url:
            return self._send_json(
                {
                    "connector": connector_id,
                    "status": "unavailable",
                    "message": "This connector has no MCP OAuth sign-in; use an API key.",
                },
                501,
            )
        redirect = str(data.get("redirect_uri") or "").strip()
        if not redirect:
            redirect = f"{self.server.public_url}/oauth/callback"
        try:
            return self._send_json(
                mcp_oauth.start_authorize(
                    self.orch.paths,
                    record,
                    url,
                    redirect,
                    client_id=str(data.get("client_id") or ""),
                    client_secret=str(data.get("client_secret") or ""),
                )
            )
        except MCPOAuthError as exc:
            return self._send_json(
                {"connector": connector_id, "status": "error", "message": str(exc)}, 502
            )

    def _connector_oauth_exchange(self):
        data = self._read_json()
        try:
            result = mcp_oauth.exchange(
                self.orch.paths, str(data.get("state") or ""), str(data.get("code") or "")
            )
        except MCPOAuthError as exc:
            return self._send_json({"status": "error", "message": str(exc)}, 400)
        self._refresh_mcp_tools(str(result.get("connector") or ""))
        return self._send_json(result)

    def _oauth_callback(self):
        qs = parse_qs(urlparse(self.path).query)
        state = (qs.get("state") or [""])[0]
        code = (qs.get("code") or [""])[0]
        error = (qs.get("error") or [""])[0]
        if error:
            desc = (qs.get("error_description") or [error])[0]
            mcp_oauth.fail(state, desc)
            return self._send_html(_oauth_page(ok=False, message=desc))
        try:
            result = mcp_oauth.exchange(self.orch.paths, state, code)
        except MCPOAuthError as exc:
            return self._send_html(_oauth_page(ok=False, message=str(exc)), 400)
        self._refresh_mcp_tools(str(result.get("connector") or ""))
        return self._send_html(
            _oauth_page(ok=True, message="You can close this window and return to the app.")
        )

    def _install_delegated_auth(self, connector_id: str):
        from . import delegated_oauth
        from .connectors import WORKSPACE_TYPES
        records = Connectors(self.orch.paths)
        record = next((r for r in records.list() if r["id"] == connector_id), None)
        if not record or record["type"] not in WORKSPACE_TYPES:
            return self._send_json({"error": "Unsupported delegated connector"}, 400)
        try:
            email = delegated_oauth.install(self.orch.paths, record, self._read_json())
            records.update(connector_id, name=email or record["name"], config={"email": email})
            return self._send_json({"status": "connected"})
        except MCPOAuthError as exc:
            return self._send_json({"error": str(exc)}, 400)

    def _delete_connector_oauth(self, connector_id: str):
        from . import delegated_oauth
        from .connectors import WORKSPACE_TYPES
        record = next((r for r in Connectors(self.orch.paths).list() if r["id"] == connector_id), None)
        if record and record["type"] in WORKSPACE_TYPES:
            try:
                delegated_oauth.disconnect(self.orch.paths, record)
            except MCPOAuthError as exc:
                return self._send_json({"error": str(exc)}, 502)

        removed = mcp_oauth.clear_tokens(self.orch.paths, connector_id)
        try:
            from connectors import mcp as mcp_runtime

            mcp_runtime.reset_sessions()
        except ImportError:  # runtime package absent (partial deploy)
            pass
        Connectors(self.orch.paths).set_mcp_tools(connector_id, [])
        return self._send_json({"id": connector_id, "removed": removed, "status": "idle"})

    def _refresh_mcp_tools(self, connector_id: str) -> None:
        """Cache the freshly connected server's tool names on the record.

        Off-thread and best-effort: the tool list is display metadata, and
        the callback page must not hang on a slow MCP server.
        """
        if not connector_id:
            return
        paths = self.orch.paths

        def work():
            try:
                from connectors import mcp as mcp_runtime

                store = Connectors(paths)
                record = next((r for r in store.list() if r.get("id") == connector_id), None)
                if record is None:
                    return
                url = connector_mcp_url(str(record.get("type", "")), record.get("config"))
                if not url:
                    return
                from collections import Counter

                from connectors.registry import tool_prefix

                counts: Counter[str] = Counter(str(r.get("type") or "") for r in store.list())
                names = mcp_runtime.tool_names(
                    paths,
                    record,
                    url,
                    name_prefix=tool_prefix(record, counts),
                )
                if names:
                    store.set_mcp_tools(connector_id, names)
            except Exception:
                return

        threading.Thread(target=work, daemon=True).start()

    def _screen(self, bot: str):
        # A bot on the machines backend gets ITS machine's display; every
        # other backend still shares the single host display.
        machine = machine_view.machine_for_bot(self.orch.paths, bot)
        if machine:
            png = machine_view.capture_png(machine)
            if png:
                return self._send_bytes(png, "image/png")
            return self._send_json(
                {
                    "available": False,
                    "reason": f"machine {machine} screen unavailable (is it running?)",
                },
                503,
            )
        png = capture_png()
        if png:
            return self._send_bytes(png, "image/png")
        return self._send_json({"available": False, "reason": unavailable_reason()}, 503)

    def _skills(self):
        qs = parse_qs(urlparse(self.path).query)
        bot = (qs.get("bot", [None])[0] or "").strip()
        if bot:
            try:
                self.orch.roster.get(bot)
            except RosterError as exc:
                return self._send_json({"error": str(exc)}, 404)
        else:
            names = self.orch.roster.names()
            bot = names[0] if names else ""
        return self._send_json(skill_catalog(self.orch.paths, bot) if bot else [])

    def _get_room(self, rest: str):
        parts = [p for p in rest.split("/") if p]
        if not parts:
            return self._send_json({"error": "room id required"}, 400)
        room_id = parts[0]
        try:
            room = get_room(self.orch.paths, room_id)
        except RoomError as exc:
            return self._send_json({"error": str(exc)}, 404)
        if len(parts) > 1 and parts[1] == "messages":
            before, limit = self._history_page()
            return self._send_json(
                recent_messages(self.orch.paths, room_id, limit=limit, before=before)
            )
        return self._send_json(room.to_dict())

    def _add_room(self):
        data = self._read_json()
        try:
            room = self.orch.create_room(
                str(data.get("title", "")),
                data.get("members") or [],
                owner=str(data.get("owner") or "") or None,
                description=str(data.get("description") or ""),
            )
        except (RosterError, RoomError) as exc:
            return self._send_json({"error": str(exc)}, 400)
        self._push_lists(rooms=True)
        return self._send_json(room.to_dict())

    def _patch_room(self, room_id: str):
        data = self._read_json()
        try:
            room = get_room(self.orch.paths, room_id)
        except RoomError as exc:
            return self._send_json({"error": str(exc)}, 404)
        if "title" in data and data["title"]:
            room.title = str(data["title"]).strip()
        if "description" in data:
            room.description = str(data.get("description") or "").strip()[:2000]
        if "members" in data:
            known = set(self.orch.roster.names())
            members = []
            seen: set[str] = set()
            for raw in data.get("members") or []:
                member = str(raw).strip()
                if member in known and member not in seen:
                    members.append(member)
                    seen.add(member)
            if len(members) < MIN_ROOM_MEMBERS:
                return self._send_json({"error": "A group chat needs at least two bots"}, 400)
            if len(members) > MAX_ROOM_MEMBERS:
                return self._send_json({"error": "A group chat can include at most six bots"}, 400)
            room.members = members
        # Older apps may still submit a bot owner. Accept their other edits,
        # but ownership stays with the user and never follows membership.
        saved = save_room(self.orch.paths, room)
        self._push_lists(rooms=True)
        return self._send_json(saved.to_dict())

    def _delete_room(self, room_id: str):
        try:
            delete_room(self.orch.paths, room_id)
        except RoomError as exc:
            return self._send_json({"error": str(exc)}, 404)
        self._push_lists(rooms=True)
        return self._send_json({"removed": room_id})

    def _get_soul(self, name: str):
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        return self._send_json(
            {
                "bot": bot.name,
                "soul": load_soul(self.orch.paths, bot.name, personality=bot.personality),
            }
        )

    def _put_soul(self, name: str):
        data = self._read_json()
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        text = str(data.get("soul", data.get("text", "")))
        save_soul(self.orch.paths, bot.name, text)
        payload = {"bot": bot.name, "soul": load_soul(self.orch.paths, bot.name)}
        # A peer's update_bot (or the other device) rewrote this identity:
        # an open Settings panel replaces its Instructions text in place.
        self._ws_fanout({"type": "soul", **payload})
        return self._send_json(payload)

    def _get_memory(self, name: str):
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        mem = self.orch.memory_for(bot.name)
        return self._send_json({"bot": bot.name, "facts": mem.facts()[-50:]})

    def _get_prompts(self):
        qs = parse_qs(urlparse(self.path).query)
        bot = (qs.get("bot", [None])[0] or "").strip() or None
        include_resolved = (qs.get("include_resolved", [""])[0]).lower() in {"true", "1"}
        self._refresh_task_prompts(bot)
        return self._send_json(
            list_prompts(self.orch.paths, bot, include_resolved=include_resolved)
        )

    def _get_blocks(self):
        qs = parse_qs(urlparse(self.path).query)
        bot = (qs.get("bot", [None])[0] or "").strip() or None
        status = (qs.get("status", [None])[0] or "").strip() or None
        return self._send_json(blocklib.list_blocks(self.orch.paths, bot, status))

    def _get_block_catalog(self):
        qs = parse_qs(urlparse(self.path).query)
        bot = (qs.get("bot", [None])[0] or "").strip()
        if not bot:
            names = self.orch.roster.names()
            bot = names[0] if names else ""
        return self._send_json(blocklib.catalog(self.orch.paths, bot) if bot else [])

    def _block_action(self):
        data = self._read_json()
        block_id = str(data.get("block_id", "") or data.get("id", "")).strip()
        action = str(data.get("action", "")).strip() or "submit"
        values = data.get("values") if isinstance(data.get("values"), dict) else {}
        if not block_id:
            return self._send_json({"error": "block actions need 'block_id'"}, 400)
        result = handle_block_action(self.orch, block_id, action, values)
        status = 404 if result.get("error", "").startswith("no block") else 200
        return self._send_json(result, status)

    def _send_now(self, name: str, rid: str):
        """Promote a queued message: preempt the turn and handle it next."""
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        if not rid:
            return self._send_json({"error": "missing request id"}, 400)
        if not messaging.mark_now(self.orch.paths, bot.name, rid):
            # Already picked up (or dropped) — the natural outcome the card
            # wants anyway, so tell the client it is no longer queued.
            return self._send_json({"ok": False, "queued": False})
        return self._send_json({"ok": True, "queued": True})

    def _get_queue(self, name: str):
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        with messaging.queue_lock(self.orch.paths, bot.name):
            busy, current = self.orch.control.busy_state(bot.name)
            state = messaging.queue_state(self.orch.paths, bot.name, busy=busy, current_id=current)
        return self._send_json(state)

    def _edit_queue(self, name: str, remove_id: str | None = None):
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        data = self._read_json() if remove_id is None else {}
        ids = data.get("ids") if isinstance(data, dict) else None
        if remove_id is None and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)):
            return self._send_json({"error": "ids must be a list of request IDs"}, 400)
        with messaging.queue_lock(self.orch.paths, bot.name):
            busy, current = self.orch.control.busy_state(bot.name)
            try:
                if remove_id is not None:
                    if not messaging.remove_queued(self.orch.paths, bot.name, remove_id, current_id=current):
                        return self._send_json({"error": "This item is no longer waiting. Refresh the queue."}, 409)
                else:
                    messaging.reorder_queue(self.orch.paths, bot.name, ids, current_id=current)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 409)
            state = messaging.queue_state(self.orch.paths, bot.name, busy=busy, current_id=current)
        return self._send_json(state)

    def _history_page(self) -> tuple[float | None, int]:
        qs = parse_qs(urlparse(self.path).query)
        raw_before = (qs.get("before") or [None])[0]
        before = None
        if raw_before:
            try:
                before = float(raw_before)
            except ValueError:
                before = None
        try:
            limit = int((qs.get("limit") or ["200"])[0] or 200)
        except ValueError:
            limit = 200
        return before, max(1, min(limit, 500))

    def _get_history(self, name: str):
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        self._refresh_task_prompts(bot.name)
        mem = self.orch.memory_for(bot.name)
        before, limit = self._history_page()
        return self._send_json(user_thread(mem, peer="user", limit=limit, before=before))

    #: most ledger rows one GET returns; the file keeps DEFAULT_RETENTION.
    _AUDIT_PAGE_MAX = 1000

    def _get_audit(self, name: str):
        """The bot's authorization ledger (harness/audit.py), newest first.

        Every governed tool call writes its decision here BEFORE it runs, so
        this is "what was this bot allowed to do, and who said so" — the
        question the per-request stream trail cannot answer once its
        conversation is gone. Rows are already secret-free (the gate records
        targets, never argument values that the redaction layer sealed).
        """
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = int(qs.get("limit", ["200"])[0])
        except ValueError:
            return self._send_json({"error": "limit must be an integer"}, 400)
        limit = max(1, min(limit, self._AUDIT_PAGE_MAX))
        event = (qs.get("event", [""])[0] or "").strip() or None
        decision = (qs.get("decision", [""])[0] or "").strip() or None
        rows = audit_log.read(
            self.orch.paths, bot.name, limit=limit, event=event, decision=decision
        )
        return self._send_json({"bot": bot.name, "rows": rows})

    # -- per-server log stream (harness/logstream.py) ------------------------
    def _log_bots(self) -> list[str]:
        try:
            return self.orch.roster.names()
        except RosterError:
            return []

    def _list_logs(self):
        """Every log a client may tail: the server's own, then one per bot."""
        return self._send_json(
            {"sources": logstream.list_sources(self.orch.paths, self._log_bots())}
        )

    def _log_query(self) -> tuple[int, int | None, int | None] | None:
        """?limit= (tail length, capped), ?offset= (byte position, as on
        /api/streams/<rid>) and ?gen= (the file identity that offset came
        from); None when malformed."""
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = int((qs.get("limit") or [str(logstream.DEFAULT_LINES)])[0])
            raw_offset = (qs.get("offset") or [""])[0]
            offset = int(raw_offset) if raw_offset != "" else None
            raw_gen = (qs.get("gen") or [""])[0]
            gen = int(raw_gen) if raw_gen != "" else None
        except ValueError:
            return None
        if limit < 0 or (offset is not None and offset < 0) or (gen is not None and gen < 0):
            return None
        return min(limit, logstream.MAX_LINES), offset, gen

    def _get_log(self, rest: str):
        """Tail one log, or follow it: /api/logs/<source>[/stream].

        Without ?offset the last ?limit= complete lines come back with the
        byte offset after them; with ?offset only the complete lines appended
        since (poll mode). /stream keeps the response open as SSE.
        """
        raw_source, _, action = rest.partition("/")
        source = unquote(raw_source)  # a bot name may carry a space
        path = logstream.resolve_source(self.orch.paths, source, self._log_bots())
        if path is None:
            return self._send_json({"error": f"no log source {source!r}"}, 404)
        query = self._log_query()
        if query is None:
            return self._send_json({"error": "limit, offset and gen must be integers >= 0"}, 400)
        limit, offset, gen = query
        if action == "stream":
            return self._stream_log(source, path, limit, offset, gen)
        if action:
            return self._send_json({"error": "not found"}, 404)
        if offset is not None:
            page = logstream.read_from(path, offset, gen=gen)
        else:
            page = logstream.tail(path, lines=limit, previous=logstream.previous_generation(path))
        return self._send_json({"source": source, **page.to_dict()})

    @staticmethod
    def _log_frame(source: str, page: logstream.LogPage, mutation: str) -> dict:
        if mutation == "heartbeat":
            return {
                "type": "log_heartbeat",
                "source": source,
                "offset": page.offset,
                "size": page.size,
                "gen": page.gen,
            }
        return {"type": "log", "source": source, "mutation": mutation, **page.to_dict()}

    def _stream_log(
        self, source: str, path, limit: int, offset: int | None, gen: int | None
    ) -> None:
        """SSE follow: the tail as a `snapshot`, then `appended` frames as
        lines land (a reconnect passes its last `offset` + `gen` and skips
        the snapshot). A `log_heartbeat` every quiet interval is what a
        client that closed cleanly fails on; a peer that vanished without a
        close (a sleeping laptop, a NAT drop) is caught by TCP keepalive /
        user-timeout armed below, so this thread never outlives its reader
        by more than a few intervals. The send timeout is a second net."""
        self._begin_sse()
        heartbeat = logstream.heartbeat_interval()
        self._arm_keepalive(self.connection, 2 * heartbeat)
        try:
            self.connection.settimeout(max(30.0, 2 * heartbeat))
        except OSError:
            pass
        logstream.stream_pages(
            path,
            lines=limit,
            offset=offset,
            gen=gen,
            stop=threading.Event(),
            write=lambda page, mutation: self._sse(self._log_frame(source, page, mutation)),
            heartbeat=heartbeat,
        )

    def _start_log_stream(
        self, source: str, path, limit: int, offset: int | None, gen: int | None
    ) -> None:
        """WS follow on a side thread (one per socket; a new start replaces it)."""
        self._stop_log_stream()
        stop = threading.Event()
        self._log_stop = stop
        self._log_source = source

        def _run():
            try:
                logstream.stream_pages(
                    path,
                    lines=limit,
                    offset=offset,
                    gen=gen,
                    stop=stop,
                    write=lambda page, mutation: self._ws_send_json(
                        self._log_frame(source, page, mutation)
                    ),
                    heartbeat=logstream.heartbeat_interval(),
                )
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        self._log_thread = threading.Thread(target=_run, daemon=True, name="log-stream")
        self._log_thread.start()

    def _stop_log_stream(self) -> str | None:
        """End this socket's log follower; returns the source it was on."""
        stop = getattr(self, "_log_stop", None)
        if stop is not None:
            stop.set()
        thread = getattr(self, "_log_thread", None)
        self._log_thread = None
        self._log_stop = None
        source = getattr(self, "_log_source", None)
        self._log_source = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        return source

    # -- problem reports (harness/reports.py) --------------------------------
    def _list_reports(self):
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = int((qs.get("limit") or ["50"])[0])
        except ValueError:
            return self._send_json({"error": "limit must be an integer"}, 400)
        limit = max(1, min(limit, 500))
        return self._send_json({"reports": reports.list_reports(self.orch.paths, limit=limit)})

    def _get_report(self, report_id: str):
        record = reports.load_report(self.orch.paths, report_id)
        if record is None:
            return self._send_json({"error": f"no report {report_id!r}"}, 404)
        return self._send_json(record)

    def _post_report(self):
        """File a report: the client's half plus the server's context now."""
        try:
            record = reports.file_report(
                self.orch,
                self._read_json(),
                boot_id=getattr(self, "boot_id", None),
                started_at=getattr(self, "started_at", None),
            )
        except reports.ReportError as exc:
            return self._send_json({"error": str(exc)}, 400)
        return self._send_json(
            {"id": record["id"], "ts": record["ts"], "kind": record["kind"]}, 201
        )

    def _delete_report(self, report_id: str):
        if not reports.delete_report(self.orch.paths, report_id):
            return self._send_json({"error": f"no report {report_id!r}"}, 404)
        return self._send_json({"deleted": report_id})

    def _get_thread(self, name: str, thread_id: str):
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        tid = _clean_id(thread_id)
        if not tid:
            return self._send_json({"error": "thread needs a message id"}, 400)
        mem = self.orch.memory_for(bot.name)
        before, limit = self._history_page()
        return self._send_json(
            user_thread(mem, peer="user", limit=limit, before=before, thread_id=tid)
        )

    def _add_memory(self, name: str):
        data = self._read_json()
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        text = str(data.get("text", "")).strip()
        if not text:
            return self._send_json({"error": "memory needs 'text'"}, 400)
        mem = self.orch.memory_for(bot.name)
        mem.remember(text)
        # Same shape as GET .../memory, so an open Settings panel swaps its
        # Memory list for the new one (a teach_bot from a peer lands live).
        self._ws_fanout({"type": "memory", "bot": bot.name, "facts": mem.facts()[-50:]})
        return self._send_json({"bot": bot.name, "ok": True})

    def _add_skill(self, name: str):
        """Write a private SKILL.md for `name` (share_skill's route).

        The body is wrapped into the six-heading procedure when it lacks
        them, like the teach-save path — the caller is a bot, not the
        strict propose_skill prompt. Fans the same `skill_saved` frame a
        chat-authored skill does, so `/` and Settings refresh at once.
        """
        try:
            bot = self.orch.roster.get(name)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        data = self._read_json()
        from agent.skills import propose_skill, skill_slug

        raw = str(data.get("name") or "").strip()
        body = str(data.get("body") or "").strip()
        if not raw or not body:
            return self._send_json({"error": "skill needs 'name' and 'body'"}, 400)
        slash = skill_slug(raw)
        path = propose_skill(
            self.orch.paths,
            bot.name,
            name=slash,
            description=str(data.get("description") or ""),
            body=body,
            when_to_use=str(data.get("when_to_use") or ""),
            strict=False,
        )
        frame = self._skill_saved_frame(bot.name, slash, str(path))
        self._ws_fanout(frame)
        return self._send_json(frame)

    def _chat(self):
        data = self._read_json()
        bot = str(data.get("bot", "")).strip()
        room = str(data.get("room", "")).strip()
        text = str(data.get("text", "")).strip()
        nonce = str(data.get("client_nonce", "") or "").strip()[:128]
        thread_id = _clean_id(data.get("thread_id"))
        message_id = _clean_id(data.get("message_id")) or uuid.uuid4().hex
        attachments = data.get("attachments") or []
        if not isinstance(attachments, list):
            attachments = []
        if (not text and not attachments) or (not bot and not room):
            return self._send_json(
                {"error": "chat needs 'text' or attachments, and 'bot' or 'room'"}, 400
            )
        digest = ""
        if nonce:
            digest = sends.input_digest(
                text,
                attachments,
                {
                    "bot": bot or None,
                    "room": room or None,
                    "thread_id": thread_id,
                    **(
                        {"voice_call_id": data.get("voice_call_id"), "message_id": message_id}
                        if data.get("voice_call_id") is not None
                        else {}
                    ),
                },
            )
            try:
                prior = sends.admit(self.orch.paths, nonce, digest)
            except sends.NonceMismatchError as exc:
                return self._send_json({"error": str(exc), "code": exc.code}, 409)
            if prior is not None:
                # Safe retry: the nonce already dispatched — replay
                # the original acceptance instead of double-sending.
                self._begin_sse()
                return self._replay_send(prior, room=room or None, write=self._sse)
        from .voice import VoiceError

        try:
            voice_call_id = self._voice_chat(data, bot, room, thread_id)
            turns = self.orch.dispatch_chat(
                text,
                bot=bot or None,
                room_id=room or None,
                attachments=attachments,
                quote=_clean_quote(data.get("quote")),
                thread_id=thread_id,
                message_id=message_id,
                voice_call_id=voice_call_id,
            )
        except (RosterError, RoomError, VoiceError) as exc:
            return self._send_json({"error": str(exc)}, 404)
        note_user_announced(self.orch, [rid for _name, rid, _reader in turns])
        if nonce:
            sends.record_accept(
                self.orch.paths,
                nonce,
                digest,
                [(name, rid) for name, rid, _reader in turns],
                room=room or None,
            )
        self._begin_sse()
        self._announce_user(
            text=text,
            bot=bot or (turns[0][0] if turns else None),
            room=room or None,
            attachments=attachments,
            quote=_clean_quote(data.get("quote")),
            thread_id=thread_id,
            message_id=message_id,
            voice_call_id=voice_call_id,
        )

        owned = [rid for _name, rid, _reader in turns if claim_stream_relay(self.orch, rid)]

        def _sse_fanout(frame):
            if owned:
                self._ws_fanout(frame)
            return self._sse(frame)

        try:
            self._stream_turns(turns, room=room or None, thread_id=thread_id, write=_sse_fanout)
        finally:
            for rid in owned:
                release_stream_relay(self.orch, rid)

    def _replay_send(
        self, record: dict, *, room: str | None, write, park_prompts: bool = True
    ) -> None:
        """Re-serve a nonce's recorded acceptance.

        The original request ids are announced with `duplicate: true`, then
        each stream is replayed from the top so the retrying client still
        ends on the reply (or a fail-fast error for a dead turn).
        """
        for turn in record.get("turns") or []:
            turn_bot = str(turn.get("bot") or "")
            rid = str(turn.get("request_id") or "")
            if not rid:
                continue
            frame = {"type": "accepted", "request_id": rid, "bot": turn_bot, "duplicate": True}
            if room:
                frame["room"] = room
            if write(frame) is False:
                return
            self._resume_frames(turn_bot, rid, 0, write, park_prompts=park_prompts)

    def _get_send(self, nonce: str):
        """Did that send land? Return the acceptance recorded for a nonce."""
        if not nonce:
            return self._send_json({"error": "nonce required"}, 400)
        record = sends.lookup(self.orch.paths, nonce)
        if record is None:
            return self._send_json({"nonce": nonce, "status": "unknown"}, 404)
        return self._send_json(record)

    def _stream_turns(
        self,
        turns,
        *,
        room: str | None,
        write,
        park_prompts: bool = True,
        thread_id: str | None = None,
    ) -> None:
        stream_turns(
            self.orch,
            turns,
            room=room,
            thread_id=thread_id,
            write=write,
            park_prompts=park_prompts,
        )

    # -- stream resume ----------------------------------------------------
    _FINAL_TAIL_BYTES = 64 * 1024

    def _stream_has_final(self, rid: str) -> bool:
        # `final` is (near-)always among the last lines — the writer appends
        # it last, plus at most a few settling re-emits — so a bounded tail
        # answers without reading a whole long turn back into memory. A False
        # from a partial window falls back to the full scan, so the answer
        # matches the old whole-file read exactly.
        path = self.orch.paths.stream_file(rid)
        if not path.is_file():
            return False
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                start = max(0, size - self._FINAL_TAIL_BYTES)
                fh.seek(start)
                chunk = fh.read()
            lines = chunk.decode("utf-8", errors="replace").splitlines()
            if start > 0 and lines:
                lines = lines[1:]  # first line is mid-record; drop it
        except OSError:
            return False
        for line in reversed(lines):
            try:
                if json.loads(line).get("type") == "final":
                    return True
            except json.JSONDecodeError:
                continue
        if size > self._FINAL_TAIL_BYTES:
            try:
                all_lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return False
            return any(self._line_is_final(line) for line in reversed(all_lines))
        return False

    @staticmethod
    def _line_is_final(line: str) -> bool:
        try:
            return json.loads(line).get("type") == "final"
        except json.JSONDecodeError:
            return False

    def _resume_frames(
        self, bot: str, rid: str, offset: int, write, *, park_prompts: bool = True
    ) -> None:
        """Replay a request's stream events from a byte offset, then follow live.

        Stream files are durable and fsync'd per event, so a client that lost
        its socket (server update, network blip) re-attaches here and its
        in-flight turn completes instead of freezing on 'thinking'.
        """
        paths = self.orch.paths
        ctrl = self.orch.control
        reader = StreamReader(paths, rid, offset=offset)
        if write({"type": "resumed", "request_id": rid, "bot": bot}) is False:
            return
        joined = read_steered(paths, rid)
        target = joined.target_request_id if joined is not None else None
        active = ctrl.busy_request(bot)
        if target is None and active and active != rid:
            # Compatibility for follow-ups consumed before acknowledgments
            # existed: the live recovery claim already retained their ids.
            try:
                from agent.recovery import store_for

                claim = store_for(paths).claim(active)
                inputs = json.loads((claim or {}).get("input_json") or "[]")
                if (claim or {}).get("bot") == bot and any(
                    isinstance(m, dict) and m.get("id") == rid for m in inputs
                ):
                    target = active
            except Exception:  # recovery metadata is best-effort
                pass
        if target:
            frame = {
                "type": "steered",
                "bot": bot,
                "request_id": rid,
                "target_request_id": target,
            }
            if joined is not None:
                frame["offset"] = joined.offset
                frame["target_offset"] = joined.target_offset
                if joined.room:
                    frame["room"] = joined.room
            write(frame)
            return
        alive = active == rid or any(m.id == rid for m in messaging.pending(paths, bot))
        if alive or self._stream_has_final(rid):
            stream_turns(
                self.orch,
                [(bot, rid, reader)],
                write=write,
                announce=False,
                park_prompts=park_prompts,
            )
            return
        # Dead turn (agent crashed before final): replay what landed on disk,
        # then fail fast instead of holding the stream timeout open.
        for ev in reader._read_new():
            frame = {k: v for k, v in asdict_event(ev).items() if v is not None}
            frame["bot"] = bot
            frame["request_id"] = rid
            if write(frame) is False or ev.type in ("final", "steered"):
                return
        write(
            {
                "type": "error",
                "error": "request is no longer running",
                "bot": bot,
                "request_id": rid,
            }
        )
        write({"type": "final", "text": "", "frm": bot, "bot": bot, "request_id": rid})

    def _get_stream(self, rid: str):
        """SSE replay/follow of one request's stream: /api/streams/<rid>?bot=&offset=."""
        rid = _SAFE_NAME.sub("", rid)
        qs = parse_qs(urlparse(self.path).query)
        bot = _SAFE_NAME.sub("", (qs.get("bot") or [""])[0])
        try:
            offset = max(0, int((qs.get("offset") or ["0"])[0] or 0))
        except ValueError:
            offset = 0
        if not rid:
            return self._send_json({"error": "stream id required"}, 400)
        self._begin_sse()
        self._resume_frames(bot, rid, offset, self._sse)

    def _upload(self):
        """Accept a raw file body (X-Filename header) into shared uploads."""
        raw = self._read_body(MAX_UPLOAD_BYTES)
        name = self.headers.get("X-Filename", "upload.bin")
        safe = _SAFE_NAME.sub("_", name).strip("_") or "upload.bin"
        self.orch.paths.uploads.mkdir(parents=True, exist_ok=True)
        stored = self.orch.paths.uploads / f"{uuid.uuid4().hex[:8]}-{safe}"
        stored.write_bytes(raw)
        mime = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        return self._send_json({"name": name, "path": str(stored), "size": len(raw), "mime": mime})

    def _get_upload(self, name: str):
        """Serve a file from workspace/uploads by stored basename only."""
        safe = _SAFE_NAME.sub("", os.path.basename(name)).strip(".")
        if not safe:
            return self._send_json({"error": "not found"}, 404)
        root = self.orch.paths.uploads.resolve()
        stored = (root / safe).resolve()
        try:
            stored.relative_to(root)
        except ValueError:
            return self._send_json({"error": "not found"}, 404)
        if not stored.is_file():
            # Bots often paste /home/agent/Downloads/<name> into markdown.
            # Promote that jail file into uploads under the same basename so
            # existing chat tiles start loading without a resend.
            try:
                from agent.postimage import promote_upload

                promoted = promote_upload(self.orch.paths, safe)
            except Exception:
                promoted = None
            if promoted is None or not promoted.is_file():
                return self._send_json({"error": "not found"}, 404)
            stored = promoted
        mime = mimetypes.guess_type(stored.name)[0] or "application/octet-stream"
        return self._send_bytes(stored.read_bytes(), mime, filename=stored.name)

    def _transcribe(self):
        raw = self._read_body(MAX_UPLOAD_BYTES)
        name = self.headers.get("X-Filename", "clip.wav")
        ctype = self.headers.get("Content-Type", "audio/wav")
        provider = self.headers.get("X-Provider", "")
        try:
            out = transcribe_audio(
                self.orch.paths,
                raw,
                filename=name,
                content_type=ctype.split(";")[0].strip() or "audio/wav",
                provider=provider or None,
            )
        except TranscribeError as exc:
            print(f"transcribe error: {exc}", flush=True)
            return self._send_json({"error": str(exc)}, 400)
        print(
            f"transcribe ok provider={out.get('provider')} bytes={len(raw)} "
            f"chars={len(str(out.get('text') or ''))}",
            flush=True,
        )
        return self._send_json(out)

    def _send(self):
        data = self._read_json()
        frm = str(data.get("from", "")).strip()
        to = str(data.get("to", "")).strip()
        text = str(data.get("text", "")).strip()
        if not frm or not to or not text:
            return self._send_json({"error": "send needs 'from','to','text'"}, 400)
        try:
            mid = self.orch.send(frm, to, text)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 404)
        return self._send_json({"id": mid})

    def _control(self, rest: str):
        parts = rest.split("/")
        bot = parts[0] if parts else ""
        action = parts[1] if len(parts) > 1 else ""
        sub = parts[2] if len(parts) > 2 else ""
        ctrl = self.orch.control
        if not bot:
            return self._send_json({"error": "control needs a bot"}, 400)

        if action == "takeover":
            teachrec.abort(self.orch.paths, bot)
            holder = str(self._read_json().get("user") or "").strip() or None
            return self._send_json(asdict(ctrl.take_over(bot, holder=holder)))
        if action == "return":
            by = str(self._read_json().get("user") or "").strip() or None
            try:
                return self._send_json(asdict(ctrl.return_control(bot, by=by)))
            except ControlDenied as exc:
                return self._send_json({"error": str(exc)}, 403)
        if action == "teach" and sub == "start":
            return self._send_json(asdict(ctrl.start_teach(bot)))
        if action == "teach" and sub == "step":
            step = str(self._read_json().get("step", "")).strip()
            return self._send_json(asdict(ctrl.record_step(bot, step)))
        if action == "teach" and sub == "save":
            data = self._read_json()
            name = str(data.get("name", "")).strip()
            if not name:
                return self._send_json({"error": "save needs 'name'"}, 400)
            _state, path = ctrl.save_teach(bot, name, str(data.get("description", "")))
            frame = self._skill_saved_frame(bot, name, path)
            self._ws_fanout(frame)
            return self._send_json(frame)
        if action == "teach" and sub == "record":
            op = parts[3] if len(parts) > 3 else ""
            if op == "start":
                self._read_json()
                return self._teach_record_start(bot)
            if op == "stop":
                self._read_json()
                return self._teach_record_stop(bot, via_http=True)
            return self._send_json({"error": "teach/record needs start or stop"}, 404)
        return self._send_json({"error": f"unknown control action {action!r}"}, 404)

    def _teach_record_start(self, bot: str):
        try:
            payload = teachrec.start(self.orch.paths, bot)
        except teachrec.TeachBusy as exc:
            return self._send_json({"error": str(exc)}, 409)
        except teachrec.TeachUnavailable as exc:
            return self._send_json({"error": str(exc)}, 503)
        self._ws_fanout({"type": "control_state", **payload})
        return self._send_json(payload)

    def _skill_saved_frame(self, bot: str, name: str, path: str) -> dict:
        """WS payload so clients refresh `/` as soon as a skill is written."""
        from agent.skills import skill_slug

        slash = skill_slug(name)
        return {
            "type": "skill_saved",
            "bot": bot,
            "name": slash,
            "skill_path": path,
            "skills": skill_catalog(self.orch.paths, bot),
        }

    def _teach_record_stop(self, bot: str, *, via_http: bool):
        try:
            result = teachrec.stop(self.orch.paths, bot)
        except teachrec.TeachIdle as exc:
            if via_http:
                return self._send_json({"error": str(exc)}, 404)
            self._ws_send_json({"type": "error", "error": str(exc)})
            return None
        state = asdict(self.orch.control.state(bot))
        self._ws_fanout({"type": "control_state", **state})
        self._dispatch_learn_chat(bot, result)
        payload = {
            **state,
            "session_id": result.session_id,
            "duration": result.duration,
            "unusable": result.unusable,
            "reason": result.reason,
            "attachments": result.attachments,
        }
        if via_http:
            return self._send_json(payload)
        return payload

    def _post_room_message(self, room_id: str):
        """Post a line into a group and fan it out like a user line.

        `from` is "user" (default) or a roster bot: `message_room` lets a
        bot ping a group from a 1:1 (Grok Bot's "pinged the group too").
        A bot never answers its own line — `dispatch_chat` drops it from
        the recipients. The turns relay on a side thread, the same way a
        host-initiated chat does, so every open client sees the answers.
        """
        data = self._read_json()
        text = str(data.get("text") or "").strip()
        frm = str(data.get("from") or "user").strip() or "user"
        if not text:
            return self._send_json({"error": "message needs 'text'"}, 400)
        if frm != "user" and frm not in self.orch.roster.names():
            return self._send_json({"error": f"unknown sender {frm!r}"}, 400)
        try:
            turns = self.orch.dispatch_chat(text, room_id=room_id, frm=frm)
        except RoomError as exc:
            return self._send_json({"error": str(exc)}, 404)
        except RosterError as exc:
            return self._send_json({"error": str(exc)}, 400)
        note_user_announced(self.orch, [rid for _name, rid, _reader in turns])
        self._announce_user(text=text, bot=None, room=room_id, frm=frm)

        def _ok(frame):
            return self._ws_fanout(frame)

        def _run():
            owned = [rid for _name, rid, _reader in turns if claim_stream_relay(self.orch, rid)]
            if not owned:
                return
            try:
                self._stream_turns(turns, room=room_id, write=_ok, park_prompts=False)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                for rid in owned:
                    release_stream_relay(self.orch, rid)

        threading.Thread(target=_run, daemon=True).start()
        return self._send_json(
            {
                "room": room_id,
                "from": frm,
                "turns": [{"bot": name, "request_id": rid} for name, rid, _reader in turns],
            }
        )

    def _dispatch_learn_chat(self, bot: str, result: teachrec.StopResult) -> None:
        from agent.learn_demo import LEARN_BLANK, LEARN_CHAT

        text = (
            LEARN_BLANK.format(reason=result.reason or "no frames")
            if result.unusable
            else LEARN_CHAT
        )
        attachments = result.attachments
        try:
            turns = self.orch.dispatch_chat(text, bot=bot, attachments=attachments)
        except (RosterError, RoomError):
            return
        note_user_announced(self.orch, [rid for _name, rid, _reader in turns])
        self._announce_user(text=text, bot=bot, room=None, attachments=attachments)

        def _ok(frame):
            return self._ws_fanout(frame)

        def _run():
            owned = [rid for _name, rid, _reader in turns if claim_stream_relay(self.orch, rid)]
            if not owned:
                return
            try:
                self._stream_turns(turns, room=None, write=_ok, park_prompts=False)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                for rid in owned:
                    release_stream_relay(self.orch, rid)

        threading.Thread(target=_run, daemon=True).start()

    # -- websocket --------------------------------------------------------
    def _websocket(self):
        if not self._authed():
            return self._send_json({"error": "unauthorized"}, 401)
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            return self._send_json({"error": "expected websocket upgrade"}, 400)

        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", wsproto.accept_key(key))
        self.end_headers()
        self.close_connection = True
        # Authenticated and long-lived from here: lift the request timeout
        # so an idle client (frames arrive on its schedule) is not cut.
        self.connection.settimeout(None)
        # A peer that vanished without closing must still end this read loop
        # (and with it any screen or log follower this socket started).
        self._arm_keepalive(self.connection, 2 * logstream.heartbeat_interval())

        # A writer lock lets a background screen-streamer thread and the reader
        # loop share this full-duplex socket safely.
        self._send_lock = threading.Lock()
        self._stream_stop: threading.Event | None = None
        self._stream_thread: threading.Thread | None = None
        self._log_stop: threading.Event | None = None
        self._log_thread: threading.Thread | None = None
        self._log_source: str | None = None
        hub = getattr(self.orch, "ws_hub", None)
        # Version handshake: lets the app detect harness updates and decide
        # whether it should nudge (or force) its own update.
        self._ws_send_json(
            {
                "type": "hello",
                "version": __version__,
                "boot_id": getattr(self, "boot_id", None),
                    "capabilities": ["harness_updates_v1"],
                **app_release(self.orch.paths),
            }
        )
        if hub is not None:
            hub.add(self)
        self._ws_reseed()

        try:
            while True:
                try:
                    frame = wsproto.read_frame(self.rfile)
                except wsproto.PayloadTooLarge:
                    with self._send_lock:
                        wsproto.send_close(self.wfile, wsproto.CLOSE_TOO_BIG)
                    break
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == wsproto.OP_CLOSE:
                    with self._send_lock:
                        wsproto.send_close(self.wfile)
                    break
                if opcode == wsproto.OP_PING:
                    with self._send_lock:
                        wsproto.send_frame(self.wfile, payload, wsproto.OP_PONG)
                    continue
                if opcode != wsproto.OP_TEXT:
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except json.JSONDecodeError:
                    self._ws_send_json({"type": "error", "error": "bad json"})
                    continue
                if not self._ws_dispatch(msg):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if hub is not None:
                hub.discard(self)
            self._stop_stream()
            self._stop_log_stream()

    def _ws_send_json(self, obj) -> bool:
        with self._send_lock:
            try:
                # Stamp inside the send lock: the global counter plus per-socket
                # serialization keeps seq strictly increasing per key per client.
                wsproto.send_json(self.wfile, self._stamp(obj))
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

    def _ws_reseed(self) -> None:
        """Push a full state snapshot right after the hello.

        A reconnecting client may have missed any number of broadcasts and a
        fresh client must never render stale connection state, so every
        socket starts from the same truth: roster, rooms, open prompts and
        per-bot control state, all marked `mutation: snapshot`. `ready`
        closes the burst so clients (and tests) know the reseed is complete.
        """
        try:
            self._refresh_task_prompts()
            self._ws_send_json({"type": "bots", "bots": self._bots(), "mutation": "snapshot"})
            self._ws_send_json(
                {
                    "type": "rooms",
                    "rooms": [r.to_dict() for r in self.orch.rooms()],
                    "mutation": "snapshot",
                }
            )
            self._ws_send_json(
                {
                    "type": "prompts",
                    # include_resolved: settled prompts ride the snapshot with
                    # their `resolution` so clients render them as settled
                    # instead of resurrecting an open box.
                    "prompts": list_prompts(self.orch.paths, include_resolved=True),
                    "mutation": "snapshot",
                }
            )
            for name in self.orch.roster.names():
                self._ws_send_json(
                    {
                        "type": "control_state",
                        **asdict(self.orch.control.state(name)),
                        "mutation": "snapshot",
                    }
                )
        except Exception:  # noqa: BLE001 - reseed is best-effort; the read loop owns errors
            pass
        self._ws_send_json({"type": "ready"})

    def _ws_send_binary(self, data: bytes) -> None:
        with self._send_lock:
            wsproto.send_frame(self.wfile, data, wsproto.OP_BINARY)

    def _start_stream(self, fps: int, source=None) -> None:
        # Always restart so switching bots / reopening computer picks up the
        # current machine instead of keeping a stale ffmpeg pipe.
        self._stop_stream()
        stop = threading.Event()
        self._stream_stop = stop

        def _run():
            try:
                for frame, _mime in stream_frames(fps, stop, source=source):
                    if stop.is_set():
                        break
                    self._ws_send_binary(frame)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        self._stream_thread = threading.Thread(target=_run, daemon=True)
        self._stream_thread.start()

    def _stop_stream(self) -> None:
        if self._stream_stop:
            self._stream_stop.set()
        thread = self._stream_thread
        self._stream_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _ws_dispatch(self, msg: dict) -> bool:
        """Handle one client WS message. Return False to close the socket."""
        kind = msg.get("type")
        ctrl = self.orch.control
        bot = str(msg.get("bot", "")).strip()
        try:
            if kind == "chat":
                text = str(msg.get("text", "")).strip()
                nonce = str(msg.get("client_nonce", "") or "").strip()[:128]
                attachments = msg.get("attachments") or []
                if not isinstance(attachments, list):
                    attachments = []
                room = str(msg.get("room", "")).strip()
                thread_id = _clean_id(msg.get("thread_id"))
                message_id = _clean_id(msg.get("message_id")) or uuid.uuid4().hex
                if not text and not attachments:
                    self._ws_send_json({"type": "error", "error": "chat needs text or attachments"})
                    return True

                def _ok(frame):
                    return self._ws_fanout(frame)

                digest = ""
                if nonce:
                    digest = sends.input_digest(
                        text,
                        attachments,
                        {
                            "bot": bot or None,
                            "room": room or None,
                            "thread_id": thread_id,
                            **(
                                {
                                    "voice_call_id": msg.get("voice_call_id"),
                                    "message_id": message_id,
                                }
                                if msg.get("voice_call_id") is not None
                                else {}
                            ),
                        },
                    )
                    try:
                        prior = sends.admit(self.orch.paths, nonce, digest)
                    except sends.NonceMismatchError as exc:
                        self._ws_send_json({"type": "error", "error": str(exc), "code": exc.code})
                        return True
                    if prior is not None:
                        # Safe retry: replay the recorded acceptance
                        # on a side thread, no second dispatch.
                        def _dup_run(record=prior, room=room):
                            try:
                                self._replay_send(
                                    record, room=room or None, write=_ok, park_prompts=False
                                )
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                pass

                        threading.Thread(target=_dup_run, daemon=True).start()
                        return True
                from .voice import VoiceError

                try:
                    voice_call_id = self._voice_chat(msg, bot, room, thread_id)
                    turns = self.orch.dispatch_chat(
                        text,
                        bot=bot or None,
                        room_id=room or None,
                        attachments=attachments,
                        quote=_clean_quote(msg.get("quote")),
                        thread_id=thread_id,
                        message_id=message_id,
                        voice_call_id=voice_call_id,
                    )
                except (RosterError, RoomError, VoiceError) as exc:
                    self._ws_send_json({"type": "error", "error": str(exc)})
                    return True
                note_user_announced(self.orch, [rid for _name, rid, _reader in turns])
                if nonce:
                    sends.record_accept(
                        self.orch.paths,
                        nonce,
                        digest,
                        [(name, rid) for name, rid, _reader in turns],
                        room=room or None,
                    )
                self._announce_user(
                    text=text,
                    bot=bot or (turns[0][0] if turns else None),
                    room=room or None,
                    attachments=attachments,
                    quote=_clean_quote(msg.get("quote")),
                    thread_id=thread_id,
                    message_id=message_id,
                    voice_call_id=voice_call_id,
                )

                # Relay on a side thread so return/takeover/chat are not queued
                # behind the 120s stream wait (the Mac sends those on this socket).
                def _run():
                    owned = [
                        rid for _name, rid, _reader in turns if claim_stream_relay(self.orch, rid)
                    ]
                    if not owned:
                        return
                    try:
                        # Stay on the socket until final so the app sees later
                        # choices, tools, and the reply — HTTP/SSE still parks.
                        self._stream_turns(
                            turns,
                            room=room or None,
                            thread_id=thread_id,
                            write=_ok,
                            park_prompts=False,
                        )
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        for rid in owned:
                            release_stream_relay(self.orch, rid)

                threading.Thread(target=_run, daemon=True).start()
            elif kind == "resume":
                rid = _SAFE_NAME.sub("", str(msg.get("request_id", "")))
                try:
                    offset = max(0, int(msg.get("offset", 0) or 0))
                except (TypeError, ValueError):
                    offset = 0
                if not bot or not rid:
                    self._ws_send_json(
                        {"type": "error", "error": "resume needs bot and request_id"}
                    )
                    return True

                def _resume_ok(frame):
                    self._ws_send_json(frame)
                    return True

                def _resume_run(bot=bot, rid=rid, offset=offset):
                    try:
                        self._resume_frames(bot, rid, offset, _resume_ok, park_prompts=False)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass

                threading.Thread(target=_resume_run, daemon=True).start()
            elif kind == "bots":
                self._ws_send_json({"type": "bots", "bots": self._bots(), "mutation": "snapshot"})
            elif kind == "rooms":
                self._ws_send_json(
                    {
                        "type": "rooms",
                        "rooms": [r.to_dict() for r in self.orch.rooms()],
                        "mutation": "snapshot",
                    }
                )
            elif kind == "skills":
                target = bot or (self.orch.roster.names()[0] if self.orch.roster.names() else "")
                self._ws_send_json(
                    {
                        "type": "skills",
                        "bot": target,
                        "skills": skill_catalog(self.orch.paths, target),
                    }
                )
            elif kind == "control":
                self._ws_send_json({"type": "control_state", **asdict(ctrl.state(bot))})
            elif kind == "takeover":
                teachrec.abort(self.orch.paths, bot)
                self._ws_send_json({"type": "control_state", **asdict(ctrl.take_over(bot))})
            elif kind == "return":
                self._ws_send_json({"type": "control_state", **asdict(ctrl.return_control(bot))})
            elif kind == "teach_start":
                self._ws_send_json({"type": "control_state", **asdict(ctrl.start_teach(bot))})
            elif kind == "teach_step":
                step = str(msg.get("step", "")).strip()
                self._ws_send_json({"type": "control_state", **asdict(ctrl.record_step(bot, step))})
            elif kind == "teach_save":
                name = str(msg.get("name", "")).strip()
                _state, path = ctrl.save_teach(bot, name, str(msg.get("description", "")))
                self._ws_fanout(self._skill_saved_frame(bot, name, path))
            elif kind == "teach_record_start":
                try:
                    payload = teachrec.start(self.orch.paths, bot)
                except teachrec.TeachBusy as exc:
                    self._ws_send_json({"type": "error", "error": str(exc)})
                except teachrec.TeachUnavailable as exc:
                    self._ws_send_json({"type": "error", "error": str(exc)})
                else:
                    self._ws_fanout({"type": "control_state", **payload})
            elif kind == "teach_record_stop":
                self._teach_record_stop(bot, via_http=False)
            elif kind == "screen_start":
                # A machines-backend bot streams ITS machine's display; the
                # host display stays the default for everything else.
                machine = machine_view.machine_for_bot(self.orch.paths, bot) if bot else None
                source = machine_view.screen_source(machine) if machine else None
                self._start_stream(int(msg.get("fps", 12)), source=source)
                self._ws_send_json(
                    {
                        "type": "screen_started",
                        "input": True if machine else hostinput.available(),
                    }
                )
            elif kind == "screen_stop":
                self._stop_stream()
                self._ws_send_json({"type": "screen_stopped"})
            elif kind == "log_start":
                # Per-server log stream: unicast to this socket only, like
                # the screen. `source` is a bot name or `server`; `bot` (the
                # screen op's spelling) is an alias; neither means the server.
                source = str(msg.get("source", "") or "").strip()
                if not source:
                    source = logstream.BOT_PREFIX + bot if bot else logstream.SERVER_SOURCE
                path = logstream.resolve_source(self.orch.paths, source, self._log_bots())
                if path is None:
                    self._ws_send_json({"type": "error", "error": f"no log source {source!r}"})
                else:
                    try:
                        limit = int(msg.get("limit", logstream.DEFAULT_LINES))
                    except (TypeError, ValueError):
                        limit = logstream.DEFAULT_LINES
                    limit = max(0, min(limit, logstream.MAX_LINES))
                    # A reconnecting client resumes from its last offset (and
                    # the gen it belongs to) and gets only what landed since.
                    raw_offset, raw_gen = msg.get("offset"), msg.get("gen")
                    try:
                        offset = None if raw_offset is None else max(0, int(raw_offset))
                        gen = None if raw_gen is None else max(0, int(raw_gen))
                    except (TypeError, ValueError):
                        offset, gen = None, None
                    # Stop the old follower before announcing the new source so
                    # no stale frame lands after `log_started`.
                    self._stop_log_stream()
                    self._ws_send_json({"type": "log_started", "source": source})
                    self._start_log_stream(source, path, limit, offset, gen)
            elif kind == "log_stop":
                source = self._stop_log_stream()
                self._ws_send_json({"type": "log_stopped", "source": source or ""})
            elif kind == "input":
                # Shared computer: human and bot may drive at the same time
                # (Grok-style). Takeover is no longer required for input.
                if bot:
                    teachrec.note_input(self.orch.paths, bot, msg)
                    browser_idle.touch(self.orch.paths, bot)
                machine = machine_view.machine_for_bot(self.orch.paths, bot) if bot else None
                if msg.get("action") == "copy":
                    text = hostinput.copy_from_display(machine)
                    if text is None:
                        self._ws_send_json(
                            {"type": "input_rejected", "reason": "no clipboard tool on host"}
                        )
                    else:
                        self._ws_send_json({"type": "clipboard", "text": text})
                elif not hostinput.dispatch(msg, machine):
                    reason = (
                        f"input to machine {machine} failed (is its container running?)"
                        if machine
                        else "no input tool on host (install xdotool, set DISPLAY)"
                    )
                    self._ws_send_json({"type": "input_rejected", "reason": reason})
            elif kind == "secret":
                # NOTE: while a chat is streaming, this socket's read loop is
                # busy relaying — clients should submit over POST /api/secrets.
                name = _SAFE_NAME.sub("", str(msg.get("name", "")).strip())
                value = str(msg.get("value", ""))
                if name and value.strip():
                    set_secret(name, value, self.orch.paths)
                    resolve_secret_prompts(self.orch.paths, name)
                    self._announce_secret_saved(name)
                else:
                    self._ws_send_json({"type": "error", "error": "secret needs name and value"})
            elif kind == "choice_response":
                answer_id = str(msg.get("id", "")).strip()
                value = str(msg.get("value", "")).strip()
                live = get_prompt(self.orch.paths, answer_id) if answer_id else None
                if live and live.get("card_type") == "control_return":
                    # Return-control answers require the holder check in the HTTP endpoint.
                    self._ws_send_json(
                        {"type": "error", "error": "use /api/answers to return control"}
                    )
                elif answer_id and value:
                    status, row = answer_prompt(
                        self.orch.paths, answer_id, value, bot=str(msg.get("bot") or "")
                    )
                    if status in {"applied", "replayed"}:
                        if status == "applied":
                            self._commit_prompt_resolution(row, answer_id, row["resolution"])
                        self._ws_send_json({"type": "choice_saved", "id": answer_id})
                    else:
                        self._ws_send_json({"type": "error", "error": "prompt_" + status})
                else:
                    self._ws_send_json({"type": "error", "error": "choice needs id and value"})
            elif kind == "block_action":
                # NOTE: like `secret`, clients should prefer POST /api/block_actions
                # while a chat is streaming (this read loop is busy relaying).
                block_id = str(msg.get("block_id", "") or msg.get("id", "")).strip()
                action = str(msg.get("action", "")).strip() or "submit"
                values = msg.get("values") if isinstance(msg.get("values"), dict) else {}
                if not block_id:
                    self._ws_send_json({"type": "error", "error": "block_action needs block_id"})
                else:
                    result = handle_block_action(self.orch, block_id, action, values)
                    if result.get("error"):
                        self._ws_send_json({"type": "error", "error": result["error"]})
                    else:
                        self._ws_send_json({"type": "block_saved", "id": block_id})
            elif kind == "ping":
                self._ws_send_json({"type": "pong"})
            else:
                self._ws_send_json({"type": "error", "error": f"unknown type {kind!r}"})
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True


def stream_turns(
    orch,
    turns,
    *,
    room: str | None = None,
    thread_id: str | None = None,
    write,
    announce: bool = True,
    park_prompts: bool = True,
) -> None:
    """Mux one-or-many bot streams; `write` returns False to stop.

    `announce=False` skips the accepted/paused/queue preamble — used when a
    reconnecting client resumes an already-accepted request mid-stream.
    `park_prompts=False` keeps following after a choice (WebSocket); HTTP/SSE
    parks so the request can close.
    """
    ctrl = orch.control
    finals: set[str] = set()
    for name, rid, _reader in turns:
        if not announce:
            break
        frame = {"type": "accepted", "request_id": rid, "bot": name}
        if room:
            frame["room"] = room
        if thread_id:
            frame["thread_id"] = thread_id
        if write(frame) is False:
            return
        q = messaging.queue_state(
            orch.paths,
            name,
            busy=ctrl.is_busy(name),
            current_id=rid,
        )
        frame = {
            "type": "queue",
            "bot": name,
            "queued": q["queued"],
            "busy": q["busy"],
            # Follow-ups join the live turn (Hermes steer). `held` used to
            # grow an Interrupt card on the bubble; that card is gone.
            "held": False,
            "request_id": rid,
        }
        if room:
            frame["room"] = room
        if write(frame) is False:
            return
    readers = [(name, reader) for name, _rid, reader in turns]
    ids = {name: rid for name, rid, _reader in turns}
    parked: set[str] = set()

    def _keep(names) -> bool:
        # A later chat is sitting behind ask_user_choice / a long tool loop.
        # Do not paint "timed out" while that bot is still working.
        starting = getattr(orch, "is_starting", lambda _name: False)
        return any(ctrl.is_busy(n) or starting(n) for n in names)

    for name, ev in multiplex(
        readers, timeout=SSE_CHAT_TIMEOUT, keep_waiting=_keep, park_prompts=park_prompts
    ):
        frame = {k: v for k, v in asdict_event(ev).items() if v is not None}
        frame["bot"] = name
        frame["request_id"] = ids.get(name)
        if room:
            frame["room"] = room
        if ev.type in ("final", "steered"):
            finals.add(name)
        if park_prompts and (ev.type in ("choice", "secret_request") or is_blocking(ev)):
            parked.add(name)
        if write(frame) is False:
            return
        if ev.type in ("final", "steered"):
            note_stream_delivered(orch, ids[name])
    for name, rid, _reader in turns:
        if name in finals or name in parked:
            continue
        err = {
            "type": "error",
            "error": "timed out waiting for a reply",
            "bot": name,
            "request_id": rid,
        }
        if room:
            err["room"] = room
        if write(err) is False:
            return
        fin = {
            "type": "final",
            "text": "",
            "frm": name,
            "bot": name,
            "request_id": rid,
        }
        if room:
            fin["room"] = room
        write(fin)


def relay_bot_turn(
    orch,
    bot: str,
    text: str,
    origin: str | None = "routine",
    *,
    task_scope: dict | None = None,
) -> str:
    """Inbox a bot AND fan the live stream out to every connected app.

    Used by the routine and dream schedulers / Test run so a result is not
    only written to `messages/user/inbox` (which the Mac client never reads).
    """
    scope_args = {"task_scope": task_scope} if task_scope is not None else {}
    rid, reader = orch.chat_stream(bot, text, origin=origin, **scope_args)
    note_user_announced(orch, [rid])
    hub = getattr(orch, "ws_hub", None)

    def write(frame):
        if hub is not None:
            hub.broadcast(frame)
        return True

    if hub is not None:
        frame = {
            "type": "routine",
            "bot": bot,
            "request_id": rid,
            "message_id": rid,
            "origin": origin,
        }
        # Dream (and leftover idle-think) ticks and a new bot's welcome
        # prompt must not appear as a user/routine prompt in chat — only the
        # bot's result should. Keep the frame so stream relay still binds
        # request_id.
        if origin not in (
            messaging.ORIGIN_DREAM,
            messaging.ORIGIN_IDLE,
            messaging.ORIGIN_WELCOME,
        ):
            frame["text"] = text
            # Only scheduler dumps collapse to a card; human block actions
            # share this fan-out with their human origin preserved.
            if messaging.is_routine_prompt(text):
                title, body = messaging.split_routine_prompt(text)
                frame["card_type"] = "routine"
                frame["payload"] = {"title": title, "detail": body}
        hub.broadcast(frame)

    def _run():
        if not claim_stream_relay(orch, rid):
            return
        try:
            # This is a server-owned broadcast, not an HTTP response that
            # needs to close at a question. Keep relaying the resolution,
            # subsequent questions and final reply while the user is away.
            stream_turns(orch, [(bot, rid, reader)], write=write, park_prompts=False)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            release_stream_relay(orch, rid)

    threading.Thread(target=_run, daemon=True, name=f"relay-{bot}").start()
    return rid


def _relay_lock(orch) -> threading.Lock:
    lock = getattr(orch, "_relay_lock", None)
    if lock is None:
        lock = threading.Lock()
        orch._relay_lock = lock
        orch._relay_rids = set()
    return lock


def claim_stream_relay(orch, rid: str) -> bool:
    """First caller to tail this request wins; others skip to avoid double fan-out."""
    if not rid:
        return False
    lock = _relay_lock(orch)
    with lock:
        active: set[str] = orch._relay_rids
        if rid in active:
            return False
        active.add(rid)
        return True


def release_stream_relay(orch, rid: str) -> None:
    if not rid:
        return
    lock = _relay_lock(orch)
    with lock:
        getattr(orch, "_relay_rids", set()).discard(rid)


#: Announced-rid memory is bounded; older entries fall off first.
_ANNOUNCED_CAP = 512


def note_user_announced(orch, rids) -> None:
    """Record that a live handler announced the user bubble for these rids.

    The consult-relay poller consults this so it never re-posts a turn's
    prompt from the busy preview — the second copy is what users saw as a
    duplicated (and truncated) chat bubble.
    """
    lock = _relay_lock(orch)
    with lock:
        seen = getattr(orch, "_announced_user_rids", None)
        if seen is None:
            seen = orch._announced_user_rids = {}
        for rid in rids:
            if rid:
                seen.pop(rid, None)
                seen[rid] = True
        while len(seen) > _ANNOUNCED_CAP:
            seen.pop(next(iter(seen)))


def user_announced(orch, rid: str) -> bool:
    lock = _relay_lock(orch)
    with lock:
        return rid in getattr(orch, "_announced_user_rids", {})


def note_stream_delivered(orch, rid: str) -> None:
    """Only an actual terminal event proves delivery; a timeout does not."""
    with _relay_lock(orch):
        delivered = getattr(orch, "_delivered_streams", None)
        if delivered is None:
            delivered = orch._delivered_streams = {}
        delivered[rid] = True
        while len(delivered) > _ANNOUNCED_CAP:
            delivered.pop(next(iter(delivered)))


def _stream_context(paths, rid: str, field: str, *, max_lines: int = 40) -> str | None:
    """Read a routing field stamped at the source, including fast settled turns."""
    path = paths.stream_file(rid)
    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                room = str(ev.get(field) or "").strip() if isinstance(ev, dict) else ""
                if room:
                    return room
    except OSError:
        return None
    return None


def _stream_room(paths, rid: str) -> str | None:
    return _stream_context(paths, rid, "room")


def _stream_bot(paths, rid: str, *, max_lines: int = 200) -> str | None:
    """Which bot wrote a stream — from a `bot` / `frm` field on any event."""
    path = paths.stream_file(rid)
    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                for key in ("bot", "frm"):
                    name = str(ev.get(key) or "").strip()
                    if name and name != "user":
                        return name
    except OSError:
        return None
    return None


def relay_consult_turn(
    orch, bot: str, rid: str, *, claimed: bool = False, room: str | None = None
) -> None:
    """Fan an in-flight turn to every connected app (live sync).

    Codex/Convex model: the server owns one tail of the durable stream file
    and broadcasts it. HTTP/WS handlers claim the same rid so we do not
    double-send. Inbox-injected user work (no sending socket) still lands
    on Mac and iOS. `claimed` says the caller already holds the relay claim
    for this rid; the release still happens here either way.
    """
    if not claimed and not claim_stream_relay(orch, rid):
        return
    hub = getattr(orch, "ws_hub", None)
    room = str(room or "").strip() or _stream_room(orch.paths, rid)

    def write(frame):
        if hub is not None:
            hub.broadcast(frame)
        return True

    def _run():
        try:
            reader = StreamReader(orch.paths, rid)
            stream_turns(
                orch,
                [(bot, rid, reader)],
                room=room,
                thread_id=_stream_context(orch.paths, rid, "thread_id"),
                write=write,
                park_prompts=False,
            )
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            release_stream_relay(orch, rid)

    threading.Thread(target=_run, daemon=True, name=f"consult-relay-{bot}").start()


def start_consult_relays(orch) -> None:
    """Watch busy flags and fan every in-flight turn to live clients.

    Colleague handoffs and user turns that never had a sending
    socket (inbox, CLI, a dropped WS) all need the same tail. A rid the
    HTTP/WS handler already claimed is skipped.
    """
    if getattr(orch, "_consult_relays_started", False):
        return
    orch._consult_relays_started = True
    seen: dict[tuple[str, str], bool] = {}
    # Request ids already handed to a relay by either path below, so the
    # stream sweep never re-plays a turn the busy poll already fanned out.
    relayed: dict[str, bool] = {}
    started = time.time()
    ticks = {"n": 0}

    def _remember(rid: str) -> None:
        relayed[rid] = True
        while len(relayed) > _ANNOUNCED_CAP:
            relayed.pop(next(iter(relayed)))

    def _sweep_streams() -> None:
        """Relay turns that settle between busy polls, including private replies."""
        try:
            entries = list(os.scandir(orch.paths.streams))
        except OSError:
            return
        for entry in entries:
            if not entry.name.endswith(".jsonl"):
                continue
            rid = entry.name[: -len(".jsonl")]
            if rid in relayed:
                continue
            if rid in getattr(orch, "_delivered_streams", {}):
                _remember(rid)
                continue
            try:
                if entry.stat().st_mtime < started:
                    continue
            except OSError:
                continue
            room = _stream_room(orch.paths, rid)
            bot = _stream_bot(orch.paths, rid)
            if not bot:
                continue  # nothing attributable yet — look again next tick
            if not claim_stream_relay(orch, rid):
                continue
            _remember(rid)
            relay_consult_turn(orch, bot, rid, claimed=True, room=room)

    def _poll() -> None:
        try:
            names = list(orch.roster.names())
        except Exception:  # noqa: BLE001 - roster can flap during tests
            return
        hub = getattr(orch, "ws_hub", None)
        ticks["n"] += 1
        for name in names:
            info = orch.control._busy_info(name)
            if not info:
                continue
            rid = str(info.get("request_id") or "")
            if not rid:
                continue
            key = (name, rid)
            if key in seen or rid in relayed or rid in getattr(orch, "_delivered_streams", {}):
                continue
            if not claim_stream_relay(orch, rid):
                # A live HTTP/WS handler owns this turn: it already announced
                # the user's bubble (full text, attachments, right thread) and
                # is fanning the stream. Re-posting the busy preview here was
                # the duplicated user bubble — clipped, and in the bot's 1:1
                # thread even for room sends.
                continue
            seen[key] = True
            while len(seen) > _ANNOUNCED_CAP:
                seen.pop(next(iter(seen)))
            _remember(rid)
            frm = str(info.get("frm") or "")
            preview = str(info.get("preview") or "").strip()
            origin = str(info.get("origin") or "")
            room = str(info.get("room") or "").strip() or None
            # Background lane: routines send as frm="user" too.
            # Claiming can lose a race with relay_bot_turn, so origin must
            # also suppress the user-bubble fanout — same as dream/idle. A
            # new bot's welcome prompt is user-lane but equally not a person
            # talking (harness/welcome.py), so it never paints a bubble.
            background = origin in (
                messaging.ORIGIN_DREAM,
                messaging.ORIGIN_IDLE,
                messaging.ORIGIN_ROUTINE,
                messaging.ORIGIN_WELCOME,
            )
            if not background:
                background = (
                    preview.startswith("[Dreaming")
                    or preview.startswith("[Idle reflection")
                    or preview.startswith("[Welcome")
                )
            if (
                hub is not None
                and frm in {"", "user"}
                and preview
                and not background
                and not user_announced(orch, rid)
            ):
                bubble = {
                    "type": "user",
                    "text": preview,
                    "frm": "user",
                    "bot": name,
                    "request_id": rid,
                    "mutation": "appended",
                }
                if room:
                    bubble["room"] = room
                for field in ("message_id", "thread_id"):
                    if info.get(field):
                        bubble[field] = info[field]
                hub.broadcast(bubble)
            relay_consult_turn(orch, name, rid, claimed=True, room=room)
        # Announce active inputs before the fallback sweep can claim their
        # streams; the sweep lacks the busy flag's full user-message context.
        if ticks["n"] % 5 == 0:
            _sweep_streams()

    def _loop() -> None:
        while True:
            time.sleep(0.2)
            try:
                _poll()
            except Exception:  # noqa: BLE001 - a missed tick must not kill the watcher
                pass

    threading.Thread(target=_loop, daemon=True, name="consult-relay").start()


def _block_task_scope(paths, inst: dict) -> dict:
    """A click inherits only grants from its still-current source task."""
    import sqlite3

    from .delivery import DeliveryUnavailable, Ledger
    from .taskscope import matching_scope

    scope = None
    if inst.get("task_id") and inst.get("task_conversation"):
        try:
            bot = str(inst.get("bot") or "")
            task_id = str(inst["task_id"])
            if Ledger(paths).unresolved_actions(bot, task_id):
                return {
                    "error": "the source task has an unresolved action outcome; verify it before continuing"
                }
            scope = matching_scope(
                paths,
                bot,
                str(inst["task_conversation"]),
                task_id,
                int(inst.get("task_revision") or 0),
            )
        except (DeliveryUnavailable, OSError, sqlite3.Error, ValueError):
            return {"error": "the block's task state could not be verified; try again later"}
        if scope is None:
            return {"error": "the block belongs to a stopped, superseded or unresolved task"}
    elif inst.get("task_id") or inst.get("task_conversation") or inst.get("task_revision"):
        return {"error": "the block has incomplete task identity; create it again"}
    return {
        **(scope or {"connector_ids": [], "provenance": []}),
        "conversation": "block:" + str(inst["id"]),
    }


def handle_block_action(orch, block_id: str, action: str, values: dict) -> dict:
    """Route a user's block submit/press to whoever is listening.

    In order: a parked show_block(wait=true) call (answers bus), then an
    installed block's handler.py, then the bot's inbox as a normal turn.
    """
    paths = orch.paths
    inst = blocklib.read_block(paths, block_id)
    if inst is None:
        return {"error": f"no block {block_id!r}"}
    bot = str(inst.get("bot") or "")
    task_scope = _block_task_scope(paths, inst)
    if task_scope.get("error"):
        return {"id": block_id, "error": task_scope["error"]}

    if inst.get("blocking") and inst.get("status") == "open":
        payload = json.dumps({"action": action, "values": values}, ensure_ascii=False)
        if not write_answer(paths, block_id, payload):
            return {"error": f"no block {block_id!r}"}
        discarded = action != "submit"
        inst["status"] = "settled"
        inst["result"] = {"action": action, "values": values}
        blocklib.write_block(paths, inst)
        hub = getattr(orch, "ws_hub", None)
        if hub is not None:
            hub.broadcast(
                {
                    "type": "block",
                    "bot": bot,
                    "id": block_id,
                    "block_type": inst.get("block_type"),
                    "status": "settled",
                    "blocking": False,
                    "update": True,
                    "mutation": "cleared" if discarded else "updated",
                }
            )
        return {"id": block_id, "ok": True, "status": "settled"}

    if inst.get("status") == "settled" and action != "submit":
        hub = getattr(orch, "ws_hub", None)
        if hub is not None:
            hub.broadcast(
                {
                    "type": "block",
                    "bot": bot,
                    "id": block_id,
                    "block_type": inst.get("block_type"),
                    "status": "settled",
                    "blocking": False,
                    "update": True,
                    "mutation": "cleared",
                }
            )
        return {"id": block_id, "ok": True, "status": "settled"}

    block_type = str(inst.get("block_type") or "")
    block_def = blocklib.find_block_def(paths, bot, block_type) if bot and block_type else None
    if block_def is not None and block_def.has_handler:
        out = blocklib.run_handler(
            block_def,
            "on_action",
            action=action,
            values=values,
            state=dict(inst.get("state") or {}),
        )
        if isinstance(out, str):
            return {"id": block_id, "error": out}
        if not isinstance(out, dict):
            return {"id": block_id, "error": "error: handler returned a non-dict"}
        if isinstance(out.get("state"), dict):
            inst["state"] = out["state"]
        view = out.get("view")
        if view is not None and blocklib.validate_view(view) is None:
            inst["view"] = view
        if out.get("settle"):
            inst["status"] = "settled"
        blocklib.write_block(paths, inst)
        hub = getattr(orch, "ws_hub", None)
        if hub is not None:
            hub.broadcast(
                {
                    "type": "block",
                    "bot": bot,
                    "id": block_id,
                    "block_type": block_type,
                    "surface": inst.get("surface"),
                    "title": inst.get("title"),
                    "view": inst.get("view"),
                    "state": inst.get("state"),
                    "status": inst.get("status"),
                    "blocking": False,
                    "update": True,
                    "mutation": "updated",
                }
            )
        message = str(out.get("message") or "").strip()
        if message and bot:
            relay_bot_turn(orch, bot, message, origin="block_action", task_scope=task_scope)
        return {"id": block_id, "ok": True, "status": inst.get("status", "open")}

    if bot:
        text = (
            f"[block action] {inst.get('title') or block_id}: "
            f"action={action} values={json.dumps(values, ensure_ascii=False)}"
        )
        relay_bot_turn(orch, bot, text, origin="block_action", task_scope=task_scope)
        return {"id": block_id, "ok": True, "status": inst.get("status", "open")}
    return {"error": f"no block {block_id!r}"}


def _answer_resolution(live: dict, value: str) -> dict:
    """Resolution payload for an answered prompt record."""
    res = {"state": "answered", "responded_value": value, "skipped": False}
    if str((live or {}).get("card_type") or "") == "confirm":
        res["approved"] = value.strip().lower() in (
            "confirm",
            "yes",
            "allow_all",
            "confirm_all",
        )
    return res


def _prompt_card_shape(live: dict) -> tuple[str, dict, str, str]:
    """card_type, payload, bot, room for a prompts/ row (choice boxes included)."""
    row = live or {}
    bot = str(row.get("bot") or "").strip()
    room = str(row.get("room") or "").strip()
    kind = str(row.get("type") or "").strip()
    if kind == "choice":
        options = row.get("options") if isinstance(row.get("options"), list) else []
        return (
            "choice",
            {
                "question": str(row.get("question") or ""),
                "options": [str(o) for o in options],
            },
            bot,
            room,
        )
    if kind == "secret_request":
        return (
            "secret_request",
            {
                "name": row.get("name"),
                "title": row.get("title"),
                "detail": row.get("reason"),
            },
            bot,
            room,
        )
    card_type = str(row.get("card_type") or "").strip() or kind
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return card_type, dict(payload), bot, room


def asdict_event(ev):
    return {
        "type": ev.type,
        "origin": ev.origin,
        "thread_id": ev.thread_id,
        "voice_call_id": ev.voice_call_id,
        "voice_input_id": ev.voice_input_id,
        "voice_text": ev.voice_text,
        "value": ev.value,
        "text": ev.text,
        "frm": ev.frm,
        "bot": ev.bot,
        "reason": ev.reason,
        "room": ev.room,
        "id": ev.id,
        "message_id": ev.message_id,
        "name": ev.name,
        "question": ev.question,
        "options": ev.options,
        "card_type": ev.card_type,
        "payload": ev.payload,
        "block_type": ev.block_type,
        "surface": ev.surface,
        "title": ev.title,
        "view": ev.view,
        "state": ev.state,
        "status": ev.status,
        "blocking": ev.blocking,
        "update": ev.update,
        "detail": getattr(ev, "detail", None),
        "offset": getattr(ev, "offset", None),
        "target_request_id": ev.target_request_id,
        "target_offset": ev.target_offset,
        # upsert fields; `streaming` False must survive the relay's
        # is-not-None filter (it does — only None is dropped).
        "streaming": getattr(ev, "streaming", None),
        "mutation": getattr(ev, "mutation", None),
        # settled-prompt outcome riding `updated` card re-emits
        "resolution": getattr(ev, "resolution", None),
    }


def make_server(
    orch: Orchestrator,
    host: str,
    port: int,
    token: str | None = None,
    *,
    public_url: str | None = None,
):
    if getattr(orch, "ws_hub", None) is None:
        orch.ws_hub = WSHub()
    from .push import PushRelay, configured_url
    push_url = configured_url(orch.paths.home)
    if push_url and orch.ws_hub.push_relay is None:
        def notification_titles(bot, room):
            try:
                author = orch.roster.get(bot).display_name()
            except (KeyError, ValueError):
                author = bot
            try:
                title = get_room(orch.paths, room).title if room else author
            except (KeyError, ValueError, OSError):
                title = room or author
            return title, author
        orch.ws_hub.push_relay = PushRelay(orch.paths.home, push_url, notification_titles)
    # The fencing epoch IS the boot id: one uuid per server instance, so an
    # epoch change tells clients their sequence state is from a dead server.
    boot_id = uuid.uuid4().hex
    orch.sequencer = EpochSequencer(epoch=boot_id)
    handler = type(
        "BoundHandler",
        (_Handler,),
        {
            "orch": orch,
            "token": token,
            "boot_id": boot_id,
            "started_at": time.time(),
            "timeout": http_timeout(),
        },
    )
    advertised = advertised_url(host, port, public_url, home=orch.paths.home)
    class PushHTTPServer(ThreadingHTTPServer):
        def server_close(self):
            relay = getattr(orch.ws_hub, "push_relay", None)
            if relay is not None:
                relay.close()
                orch.ws_hub.push_relay = None
            super().server_close()

    httpd = PushHTTPServer((host, port), handler)
    if orch.ws_hub.push_relay is not None:
        orch.ws_hub.push_relay.start()
    httpd.public_url = (
        advertised_url(host, httpd.server_address[1], public_url, home=orch.paths.home)
        if port == 0
        else advertised
    )
    # Observe the lifecycle itself, including updater rolls with no HTTP caller.
    orch.on_restart_changed = lambda: orch.ws_hub.broadcast(
        {"type": "bots", "bots": handler._bots(), "mutation": "snapshot"}
    )
    start_consult_relays(orch)
    bound_host, bound_port = httpd.server_address
    # Local loopback URL so bot tools can call the parent API (create_bot).
    info = {
        "url": f"http://127.0.0.1:{bound_port}",
        "host": bound_host,
        "port": bound_port,
        "key": token or "",
        "version": __version__,
        "pid": os.getpid(),
    }
    try:
        write_atomic(orch.paths.home / "serve.json", json.dumps(info))
    except OSError:
        pass
    return httpd


def start_machine_flush(orch: Orchestrator) -> None:
    """Periodic machine -> canonical state sync (machines backend only).

    The authoritative sync stays the quiesced end-of-session one; this timer
    just narrows the window a crash could lose. HARNESS_MACHINE_SYNC_INTERVAL
    seconds, default 300; 0 disables.
    """
    if orch.backend_name != "machines":
        return
    try:
        interval = int(os.environ.get("HARNESS_MACHINE_SYNC_INTERVAL", "300"))
    except ValueError:
        interval = 300
    if interval <= 0:
        return

    def _loop():
        while True:
            time.sleep(interval)
            flush = getattr(orch.backend, "flush", None)
            if flush is None:
                return
            for name in orch.roster.names():
                try:
                    flush(name)
                except Exception:  # noqa: BLE001 - a sick machine must not kill the timer
                    pass

    threading.Thread(target=_loop, daemon=True, name="machine-flush").start()


def start_browser_idle_sweep(orch: Orchestrator) -> None:
    """Close browsers in machines whose bot has gone idle (machines backend).

    HARNESS_BROWSER_IDLE_MINUTES (default 30, 0 disables) is the idle limit,
    HARNESS_BROWSER_IDLE_SWEEP_INTERVAL (default 120 s, 0 disables) the
    period. Contract and clock: harness/browser_idle.py.
    """
    if orch.backend_name != "machines":
        return
    limit_min = browser_idle.idle_minutes()
    interval = browser_idle.sweep_interval()
    if limit_min <= 0 or interval <= 0:
        return
    close = getattr(orch.backend, "close_browser", None)
    if close is None:
        return

    def _held(bot: str) -> bool:
        try:
            return orch.control.state(bot).paused
        except Exception:  # noqa: BLE001 - unreadable control = do not touch it
            return True

    def _loop():
        while True:
            time.sleep(interval)
            try:
                browser_idle.sweep(
                    orch.paths,
                    orch.roster.names(),
                    close_browser=close,
                    control_held=_held,
                    limit_s=limit_min * 60,
                    log=lambda m: print(m, flush=True),
                )
            except Exception:  # noqa: BLE001 - never kill the timer
                pass

    threading.Thread(target=_loop, daemon=True, name="browser-idle").start()


def start_screenshot_sweeper(orch: Orchestrator) -> None:
    """Purge expired staged screenshots on a timer.

    One sweep at startup catches anything that expired while the server was
    down; then HARNESS_SCREENSHOT_SWEEP_INTERVAL seconds between sweeps
    (default hourly, 0 disables). Reading a file through ScreenshotStore
    refreshes its mtime, so in-flight sends are never swept.
    """
    from .screenshots import ScreenshotStore, sweep_interval

    store = ScreenshotStore(orch.paths)
    try:
        store.sweep()
    except Exception:  # noqa: BLE001 - housekeeping must not block startup
        pass
    interval = sweep_interval()
    if interval <= 0:
        return

    def _loop():
        while True:
            time.sleep(interval)
            try:
                store.sweep()
            except Exception:  # noqa: BLE001 - housekeeping must not die
                pass

    threading.Thread(target=_loop, daemon=True, name="screenshot-sweep").start()


def serve(
    *,
    home: str | None = None,
    roster_path: str = "roster.toml",
    backend: str = "machines",
    host: str = "127.0.0.1",
    port: int = 8765,
    start_bots: bool = False,
    desktop: bool = False,
    public_url: str | None = None,
) -> int:  # pragma: no cover - long-running loop
    orch = Orchestrator.create(home=home, roster_path=roster_path, backend=backend)
    # Per-server log stream: from here on every line this process prints
    # (journal included) is also in run/server/serve.log, tailable over the
    # API — including what init() says (a state-store migration summary, a
    # network-home warning, a schema refusal).
    server_log = logstream.install_server_log(orch.paths)
    orch.init()
    orch.use_json_store()  # UI-manageable roster (bots CRUD persist here)
    from agent.policy import ensure_suggested_policy

    seeded = ensure_suggested_policy(orch.paths)
    if seeded:
        print(f"policy: seeded suggested auto-review at {seeded}", flush=True)
    if desktop:
        from .computer_env import bringup

        report = bringup(orch.paths)
        print(f"desktop: started={report.get('started')} skipped={report.get('skipped')}")

    # Zero-setup pairing: always auth with a persisted linking key (or an
    # explicit HARNESS_TOKEN override).
    # Credentials never follow a redirect off their host (harness/netguard);
    # `harness serve` gets this from cli.main, an embedded serve() needs it too.
    install_safe_redirects()
    had_key = (orch.paths.home / "link-key").is_file()
    override = os.environ.get("HARNESS_TOKEN")
    token = override or get_or_create_key(orch.paths)
    # However the bearer arrived, run/server/serve.log must never carry it.
    register_secret(token, "LINK_KEY")
    httpd = make_server(orch, host, port, token, public_url=public_url)
    start_scheduler(orch, send=lambda bot, text, **kw: relay_bot_turn(orch, bot, text, **kw))
    start_dream_scheduler(
        orch, send=lambda bot, text: relay_bot_turn(orch, bot, text, origin="dream")
    )
    start_machine_flush(orch)
    start_browser_idle_sweep(orch)
    start_screenshot_sweeper(orch)
    from .bot_cleanup import start_sweeper

    start_sweeper(orch)
    start_agent_updater(orch)

    def _sweep_junk() -> None:
        try:
            from isolation.state_sync import sweep_canonical_store

            n = sweep_canonical_store(orch.paths)
            if n:
                print(f"machines: purged {n} bytes of regenerable chrome junk", flush=True)
        except Exception as exc:  # noqa: BLE001 - a junk walk must not kill serve
            print(f"machines: junk sweep failed: {exc}", file=sys.stderr, flush=True)

    threading.Thread(target=_sweep_junk, daemon=True, name="junk-sweep").start()
    print(f"harness API on http://{host}:{port}  bots={orch.roster.names()}")
    print(f"server log: {server_log.path}  (harness logs server -f, GET /api/logs/server/stream)")
    print(
        pairing_notice(
            httpd.public_url,
            token,
            first_run=not had_key and not override,
            interactive=sys.stdout.isatty(),
        )
    )

    # Bind before --up. Machine clone-down/peer-flush can take minutes; apps
    # must be able to reconnect as soon as the port is open. orch.up() adopts
    # any agents that survived a KillMode=process restart.
    if start_bots:

        def _bring_up() -> None:
            try:
                from .update_state import read
                operation = read(orch.paths)
                if operation.get("running_before") is not None:
                    names = [n for n in orch.roster.names()
                             if (n in operation["running_before"] or
                                 ((h := orch._handle(n)) and h.status.value == "running"))
                             and not (orch.paths.run / f"{n}.stopped").exists()]
                    handles = [orch._start_bot(n) for n in names]
                else:
                    handles = orch.up()
                for h in handles:
                    print(f"started {h.bot} pid={h.pid}", flush=True)
            except Exception as exc:  # noqa: BLE001 - a sick spawn must not kill serve
                print(f"bot bring-up failed: {exc}", file=sys.stderr, flush=True)

        threading.Thread(target=_bring_up, daemon=True, name="bots-up").start()

    stopping = threading.Event()

    def _graceful() -> None:
        """Warn clients, snapshot machine state, stop accepting. Bots are NOT
        stopped: their processes survive and the next serve adopts them."""
        if stopping.is_set():
            return
        stopping.set()
        hub = getattr(orch, "ws_hub", None)
        if hub is not None:
            hub.shutdown(retry_in=2.0)
        flush = getattr(orch.backend, "flush", None)
        from .update_state import read
        managed_update = read(orch.paths).get("stage") == "installing_controller"
        if flush is not None and not managed_update:
            for name in orch.roster.names():
                try:
                    flush(name)
                except Exception:  # noqa: BLE001 - a sick machine must not block shutdown
                    pass
        httpd.shutdown()

    def _on_sigterm(_signum, _frame) -> None:
        # Never call httpd.shutdown() from the signal handler itself: it runs
        # on the main thread, which sits inside serve_forever(), and shutdown()
        # blocks until that loop exits — a deadlock. Tear down on a thread.
        print("\nSIGTERM: notifying clients and shutting down…")
        threading.Thread(target=_graceful, daemon=True, name="graceful-shutdown").start()

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
        _graceful()
    finally:
        httpd.shutdown()
        httpd.server_close()
        server_log.flush_partial()
    return 0
