"""Device linking: zero-setup pairing between a remote harness and the app.

Run the harness (`harness serve`) and it prints a **linking key** and a one-paste
**link code** (which also carries the URL). Paste the code into the desktop app
to connect — no manual URL/token entry, no config files. The key is persisted at
`$HARNESS_HOME/link-key` so it stays stable across restarts (rotate with
`harness link --rotate`).

The link code is `dotobot_<base64url(json{"url","key"})>`.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
from pathlib import Path
from urllib.parse import urlsplit

from .fsutil import write_private
from .paths import HarnessPaths
from .redaction import register_secret


def get_or_create_key(paths: HarnessPaths, *, rotate: bool = False) -> str:
    path = paths.home / "link-key"
    paths.home.mkdir(parents=True, exist_ok=True)
    if rotate and path.exists():
        path.unlink()
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            register_secret(existing, "LINK_KEY")
            return existing
    key = secrets.token_urlsafe(24)
    register_secret(key, "LINK_KEY")
    # Born 0600 and atomic: the link key is the one bearer credential that
    # lives outside credentials/ (whose directory mode shields its files),
    # so the file itself must never exist with a wide mode, even briefly.
    write_private(path, key)
    return key


def advertised_host(bind_host: str) -> str:
    """A host the app can actually reach (detect LAN IP when binding all)."""
    if bind_host not in ("0.0.0.0", "::", ""):
        return bind_host
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def validate_public_url(value: str) -> str:
    """An HTTPS origin advertised to remote clients, never a credential URL."""
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("public URL must be an HTTPS origin without whitespace")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or "\\" in value
    ):
        raise ValueError("public URL must be an HTTPS origin, for example https://bots.example.com")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("public URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("public URL has an invalid port")
    return f"https://{parsed.netloc.lower()}"


def advertised_url(
    bind_host: str, port: int, public_url: str | None = None, *, home: Path | None = None
) -> str:
    """Explicit URL, environment, installed setting, then the legacy LAN URL."""
    configured = public_url if public_url is not None else os.environ.get("HARNESS_PUBLIC_URL")
    if configured is None and home is not None:
        try:
            configured = (home / "public-url").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
    if configured is not None:
        return validate_public_url(configured)
    host = advertised_host(bind_host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def link_code(url: str, key: str) -> str:
    payload = json.dumps({"url": url, "key": key}).encode("utf-8")
    code = "dotobot_" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    # The code is the key in base64 clothing: the scrubber knows the raw key
    # but not this form, so register it too — the pairing banner is printed
    # at first run and would otherwise land in run/server/serve.log in the clear.
    register_secret(code, "LINK_CODE")
    register_secret("harness_" + code.removeprefix("dotobot_"), "LINK_CODE")
    return code


def decode_link_code(code: str) -> dict:
    text = code.strip()
    for prefix in ("dotobot_", "harness_"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    padding = "=" * (-len(text) % 4)
    data = base64.urlsafe_b64decode(text + padding)
    return json.loads(data)


def pairing_banner(url: str, key: str) -> str:
    # The banner carries the bearer in the clear on purpose (it is how the
    # app pairs); make sure every log that copies stdout seals it, whichever
    # way the key arrived (`$HARNESS_TOKEN` skips get_or_create_key).
    register_secret(key, "LINK_KEY")
    code = link_code(url, key)
    bar = "═" * 60
    return (
        f"\n{bar}\n"
        "  Dotobot is running — paste this into the desktop app:\n\n"
        f"  Link code:   {code}\n\n"
        f"  (or enter manually)  URL: {url}\n"
        f"                       Linking key: {key}\n"
        f"{bar}\n"
    )


def pairing_notice(url: str, key: str, *, first_run: bool, interactive: bool) -> str:
    """What `harness serve` prints about pairing.

    The banner carries the linking key (the link code is base64 of it), so
    under systemd it used to land in the journal on every restart — a second,
    unprotected copy of the bearer for anyone who can read logs. Print it when
    somebody is there to read it (a TTY) or when the key was just created (the
    documented first-run flow); otherwise point at `harness link`, which
    prints it on demand.
    """
    if first_run or interactive:
        return pairing_banner(url, key)
    return f"\n  paired at {url} — run `harness link` to show the link code\n"
