"""What a bot-spawned host child is allowed to inherit.

When the harness runs a host process because a bot asked for it — a
`run_command` shell on the process backend, a browser or app launch on the
shared desktop — the child gets an environment built from an **allow-list**.

It used to be a deny-list of four names (`SSH_AUTH_SOCK`,
`DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`) applied over
a full copy of `os.environ`, which meant the child inherited everything nobody
had thought to name: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`,
`HARNESS_TOKEN`, and whatever the operator happened to have exported. A model
that can run `env` could read the harness's own credentials.

The difference is not cosmetic. A deny-list is a list of the things somebody
thought of, and it stays correct only until the next variable is added; an
allow-list is a list of the things the child actually needs, and adding a
variable elsewhere cannot silently widen it. Only one of those two stays right
without maintenance.

Follows openbot's shell environment (CopilotKit/openbot,
`docs/architecture.md`), which passes PATH, locale, terminal and proxy
variables and names anything else through one explicit setting.
`HARNESS_SHELL_ENV` is that setting here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Exact names a bot-spawned child inherits.
#:
#: `DISPLAY` and `XAUTHORITY` are here because bots drive an X display the
#: harness owns, and a GUI launch without them reaches no screen at all. That
#: is the harness's own display, not the user's Wayland session — `WAYLAND_DISPLAY`
#: is deliberately absent, as is `XDG_RUNTIME_DIR`, which is the rendezvous
#: directory for the user's session sockets.
ALLOWED_ENV_VARS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "PWD",
    "TMPDIR",
    "LANG",
    "LANGUAGE",
    "TERM",
    "TZ",
    "DISPLAY",
    "XAUTHORITY",
)

#: Prefixes that carry a family of names rather than one. `LC_*` is the rest of
#: the locale; the proxy variables decide whether a command can reach the
#: network at all, and a deployment behind a proxy that dropped them would see
#: every `curl` fail for no stated reason.
ALLOWED_ENV_PREFIXES = ("LC_",)

#: Proxy settings, both spellings. Tools disagree about case and both are
#: conventional, so passing one and not the other is its own bug.
ALLOWED_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "FTP_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "ftp_proxy",
)

#: Comma-separated extra names an operator chooses to pass through.
#: The escape hatch, and deliberately explicit: widening the environment a bot's
#: shell sees should be something somebody wrote down, not something that
#: happened.
EXTRA_ENV_SETTING = "HARNESS_SHELL_ENV"

#: Kept only so an existing import does not break. The deny-list is no longer
#: how the decision is made — these names simply are not on the allow-list.
AMBIENT_AUTHORITY_ENV_VARS = (
    "SSH_AUTH_SOCK",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "WAYLAND_DISPLAY",
)


def _extra_names(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get(EXTRA_ENV_SETTING) or ""
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def shell_env(
    env: Mapping[str, str] | None = None, *, extra: tuple[str, ...] = ()
) -> dict[str, str]:
    """The environment a bot-spawned child gets: allow-listed names only.

    `extra` is for a caller that knows the child needs something specific (a
    machine name, a bot id). Everything else has to come through
    `HARNESS_SHELL_ENV`, where an operator can see it.
    """
    source = dict(os.environ if env is None else env)
    names: list[str] = [*ALLOWED_ENV_VARS, *ALLOWED_PROXY_VARS]
    names.extend(_extra_names(source))
    names.extend(extra)

    out = {name: source[name] for name in names if name in source}
    for name, value in source.items():
        if name.startswith(ALLOWED_ENV_PREFIXES):
            out[name] = value
    # A child with no PATH at all cannot resolve `sh`, which turns a scrubbed
    # environment into "command not found" for everything. Give it the POSIX
    # default rather than nothing.
    out.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return out


def scrub_ambient_authority(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a bot-spawned host child.

    Kept under its original name because that is what the call sites say and
    what the intent still is; it is the implementation that changed, from
    dropping four names to keeping the ones that are needed.
    """
    return shell_env(env)
