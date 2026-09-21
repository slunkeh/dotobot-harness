"""Optional durable notification relay. No Apple credentials live in the runtime.

The operator configures one HTTPS relay URL. Authenticated owners register
opaque, revocable delivery capabilities obtained from that relay. Chat work
never waits on the network; a bounded SQLite outbox retries temporary failures.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_ID = re.compile(r"[a-f0-9]{64}\Z")


def configured_url(home) -> str:
    if "HARNESS_PUSH_RELAY_URL" in os.environ:
        return os.environ["HARNESS_PUSH_RELAY_URL"]
    path = home / "push-relay.json"
    if not path.exists():
        return ""
    value = json.loads(path.read_text()).get("url", "")
    if not isinstance(value, str):
        raise ValueError("push relay URL must be a string")
    return value


def notification(frame: dict) -> dict | None:
    bot = frame.get("bot")
    if not isinstance(bot, str) or not bot:
        return None
    kind = frame.get("type")
    if frame.get("mutation") == "updated" or frame.get("resolution") or frame.get("update"):
        return None
    body = None
    if kind == "final":
        body = frame.get("text")
    elif kind == "choice":
        body = frame.get("question") or "Waiting on your choice"
    elif kind == "takeover":
        body = frame.get("reason") or "Asking you to take over the computer"
    elif kind == "secret_request":
        body = "Waiting on a secret: " + str(frame.get("title") or frame.get("name") or "Secret")
    elif kind == "card" and frame.get("card_type") in ("confirm", "control_return"):
        body = (
            "Waiting for your confirmation"
            if frame["card_type"] == "confirm"
            else "Asking for the computer back"
        )
    elif kind == "block" and frame.get("blocking") is True and frame.get("status") != "settled":
        body = frame.get("title") or "Waiting on your input"
    if not isinstance(body, str) or not body.strip():
        return None
    # A replay or prompt upsert has the same identity, including across boots.
    identity = (
        frame.get("id")
        or frame.get("block_id")
        or frame.get("message_id")
        or frame.get("request_id")
    )
    if not isinstance(identity, str) or not identity:
        return None
    conv = "room:" + str(frame["room"]) if frame.get("room") else "bot:" + bot
    event_id = hashlib.sha256(json.dumps([kind, bot, conv, identity]).encode()).hexdigest()
    return {
        "event_id": event_id,
        "bot": bot,
        "conv": conv,
        "title": bot,
        "body": body.strip()[:600],
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PushRelay:
    def __init__(self, home, url: str, title_for=None):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise ValueError("HARNESS_PUSH_RELAY_URL must be an HTTPS URL")
        self.url = url
        self.title_for = title_for
        self.path = home / "push.sqlite3"
        self.lock = threading.Lock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY, secret TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, subscription TEXT NOT NULL, payload TEXT NOT NULL,
                    created REAL NOT NULL, next_attempt REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
            """)
        os.chmod(self.path, 0o600)
        self.stop = threading.Event()
        self.thread = None

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    def register(self, body: dict):
        sid, secret = body.get("id"), body.get("secret")
        if (
            not isinstance(sid, str)
            or not _ID.fullmatch(sid)
            or not isinstance(secret, str)
            or not _ID.fullmatch(secret)
        ):
            raise ValueError("invalid push subscription")
        with self.lock, self._db() as db:
            db.execute("DELETE FROM subscriptions WHERE expires < ?", (time.time(),))
            if (
                db.execute("SELECT count(*) FROM subscriptions").fetchone()[0] >= 100
                and not db.execute("SELECT 1 FROM subscriptions WHERE id=?", (sid,)).fetchone()
            ):
                raise ValueError("push subscription limit reached")
            db.execute(
                "INSERT OR REPLACE INTO subscriptions VALUES (?,?,?)",
                (sid, secret, time.time() + 90 * 86400),
            )

    def enqueue(self, frame: dict):
        event = notification(frame)
        if event is None:
            return
        if self.title_for is not None:
            title, author = self.title_for(event["bot"], frame.get("room"))
            event["title"] = title
            if frame.get("room"):
                event["body"] = f"{author}: {event['body']}"[:600]
        now = time.time()
        with self.lock, self._db() as db:
            db.execute("DELETE FROM outbox WHERE created < ?", (now - 86400,))
            db.execute("DELETE FROM subscriptions WHERE expires < ?", (now,))
            if db.execute("SELECT count(*) FROM outbox").fetchone()[0] >= 10000:
                return
            for (sid,) in db.execute("SELECT id FROM subscriptions"):
                db.execute(
                    "INSERT OR IGNORE INTO outbox (id, subscription, payload, created, next_attempt) VALUES (?,?,?,?,?)",
                    (sid + event["event_id"], sid, json.dumps(event), now, now),
                )

    def deliver_one(self):
        now = time.time()
        with self.lock, self._db() as db:
            row = db.execute(
                "SELECT o.id, o.subscription, o.payload, o.attempts, s.secret FROM outbox o JOIN subscriptions s ON s.id=o.subscription WHERE o.done=0 AND o.next_attempt<=? AND o.created>? AND s.expires>? ORDER BY o.created LIMIT 1",
                (now, now - 3600, now),
            ).fetchone()
        if row is None:
            return False
        key, sid, payload, attempts, secret = row
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"subscription_id": sid, **json.loads(payload)}).encode(),
            headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (OSError, urllib.error.URLError):
            status = 503
        with self.lock, self._db() as db:
            if status in (401, 403, 404, 410):
                db.execute("DELETE FROM subscriptions WHERE id=? AND secret=?", (sid, secret))
            if 200 <= status < 300 or status in (400, 401, 403, 404, 410, 413):
                db.execute("UPDATE outbox SET done=1 WHERE id=?", (key,))
            else:
                db.execute(
                    "UPDATE outbox SET attempts=attempts+1, next_attempt=? WHERE id=?",
                    (now + min(300, 2 ** min(attempts + 1, 8)), key),
                )
        return True

    def start(self):
        def run():
            while not self.stop.wait(1):
                try:
                    self.deliver_one()
                except (OSError, sqlite3.Error):
                    # Never log capability tokens or message text.
                    pass

        self.thread = threading.Thread(target=run, daemon=True, name="push-relay")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=11)
