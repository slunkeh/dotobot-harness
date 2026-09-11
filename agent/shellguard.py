"""Built-in block on destructive deletes of sensitive directories.

Policy.toml can forbid `rm -rf` with a glob, and nothing asks by default — so
an install with no policy will run `rm -rf /` (or `rm -rf ~`) through
`run_command` on the process backend, or through xterm on a bot desktop,
without anyone being asked. That is not an operator preference. It is a
safety interlock, hung off `govern()` the same way the dream and exposure
defaults are: one place, before the handler, never inside one.

What is sensitive:

* filesystem roots (`/`) and Unix/macOS system prefixes (`/usr`, `/etc`,
  `/System`, …)
* the parent of home directories (`/home`, `/Users`) as an exact target
* the home *root* (`~`, `$HOME`, `/home/agent`) — not `~/Downloads`
* Chrome/harness state under the home (`~/.config`, `~/.harness-local`)
* extra paths the caller names (the host `$HARNESS_HOME` on the process
  backend, `/app` inside a machine)

A recursive delete of a path *under* a system prefix is also blocked
(`rm -rf /usr/bin`). A recursive delete of a file the bot owns is not
(`rm -rf /tmp/build`, `rm -rf ~/Downloads/old`).

Commands are split the way `/bin/sh -c` reads them: on `;` `&&` `||`
`|` `&`, on newlines, and at `( ) { }` grouping; leading reserved words
(`then`, `do`, `!`, …) and wrappers (`sudo`, `env`, `exec`, …) are
stripped before the verb is read, and a `sh -c "…"` script is inspected
as a command of its own. A `true\nrm -rf /` two-liner or `(rm -rf /)`
used to read as one argv whose verb was `true` / `(`, and walked past.

This is a scanner, not a shell. Command substitution, shell variables
(`d=/; rm -rf $d`), a copied `/bin/rm`, and `python -c
"shutil.rmtree('/')"` are known residuals — the in-machine `/bin/rm`
wrapper (`deploy/machine_rm.py`) closes the desktop/xterm door for the
same patterns. It never raises: a command it cannot parse is left to the
handler rather than refusing `ls`.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from dataclasses import dataclass

#: Basenames of commands that only wrap another argv. Stripped so
#: `sudo rm -rf /` and `timeout 5 rm -rf /` are the same hit as `rm -rf /`.
_WRAPPERS = frozenset(
    {
        "sudo",
        "command",
        "nice",
        "nohup",
        "stdbuf",
        "env",
        "time",
        "timeout",
        "ionice",
        "unshare",
        "chroot",
        "busybox",
        "exec",
    }
)

#: Shells whose `-c SCRIPT` argument is a command line of its own. The
#: script is inspected recursively so `sh -c "rm -rf /"` is the same hit
#: as `rm -rf /` instead of an opaque quoted string.
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "fish"})
_MAX_SHELL_DEPTH = 3

#: Reserved words and grouping tokens that can sit in front of the real
#: verb (`if true; then rm -rf /; fi`, `! rm -rf /`, `{ rm -rf /; }`).
#: Stripped like wrappers; before this the keyword itself was taken for
#: the command and the delete behind it was never looked at.
_LEADING_NOISE = frozenset(
    {
        "!",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "do",
        "done",
        "while",
        "until",
        "(",
        ")",
        "{",
        "}",
    }
)

#: Tokens that end one simple command and start the next. shlex emits
#: `&&` / `||` / `;;` / `|&` as single tokens, so any run of these
#: characters is a boundary; the grouping tokens are boundaries too.
_SEPARATOR_CHARS = frozenset(";|&")
_GROUPING = frozenset("(){}")

#: `\` + newline continues a line; joined before the newline split so
#: `rm -rf \<newline>/` stays one command.
_CONTINUATION = re.compile(r"\\\r?\n")

#: Directories a recursive (or root) delete must never take. A path is a
#: hit when it equals one of these or is a child of one, except the
#: carve-outs in `_SAFE_UNDER_SYSTEM`.
_SYSTEM_PREFIXES = (
    "/bin",
    "/sbin",
    "/usr",
    "/etc",
    "/lib",
    "/lib64",
    "/lib32",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/run",
    "/root",
    "/opt",
    "/app",
    "/var",
    "/System",
    "/Library",
    "/Applications",
    "/private",
)

#: Exact parents of user homes. `/home/agent/Downloads` is not a hit;
#: `/home` and `/home/agent` (when that is HOME) are.
_HOME_PARENTS = ("/home", "/Users")

#: Writable scratch that happens to live under a system prefix.
_SAFE_UNDER_SYSTEM = ("/var/tmp", "/private/tmp", "/tmp")

#: State under $HOME that *is* the machine, not the user's files.
_HOME_STATE_SUFFIXES = ("/.config", "/.harness-local")

#: Last-ditch pattern when shlex cannot tokenise the line. Narrow on
#: purpose: a false refuse of `ls` is worse than missing a clever bypass.
_EMERGENCY = re.compile(
    r"\brm\b[^;&|\n]*("
    r"--no-preserve-root"
    r"|-[^\s]*[rR][^\s]*\s+(/(\s|$)|/\*|~(?:/|\s|$)|\$\{?HOME\}?)"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Hit:
    """One blocked command: which verb, which path, why."""

    command: str
    path: str
    reason: str


def default_home() -> str:
    """Home the command will see, not necessarily the harness host's.

    A host process driving a machine (`HARNESS_MACHINE_NAME`) execs into
    uid 1000's `/home/agent`. Inside the machine image (`HARNESS_MACHINE`)
    the same path is `$HOME`. Everywhere else the process backend runs as
    the host user, so `~` is the operator's home and wiping it is the
    failure this module exists to prevent.
    """
    if os.environ.get("HARNESS_MACHINE_NAME") or os.environ.get("HARNESS_MACHINE"):
        return "/home/agent"
    return os.path.expanduser("~")


def default_cwd() -> str:
    """Best-effort cwd for relative `..` resolution. Empty if unknown."""
    if os.environ.get("HARNESS_MACHINE_NAME") or os.environ.get("HARNESS_MACHINE"):
        return "/home/agent"
    return ""


def refusal_text(tool: str, hit: Hit) -> str:
    """Model-facing refusal. Same shape as the other govern() errors."""
    where = f" ({hit.path})" if hit.path else ""
    return (
        f"error: {tool} refused — {hit.reason}{where}. "
        "Destructive deletes of system directories and the home root are "
        "blocked. Do not retry it and do not work around it; tell the user "
        "what you were trying to do and why it would help."
    )


def inspect(
    command: str,
    *,
    home: str | None = None,
    cwd: str | None = None,
    extra_sensitive: tuple[str, ...] | list[str] = (),
) -> Hit | None:
    """Return a Hit if `command` would destroy a sensitive directory.

    Never raises. `home` / `cwd` default from the process environment so
    callers that do not know the jail can still refuse `rm -rf ~`.
    """
    text = str(command or "").strip()
    if not text:
        return None
    home_s = _posix(home if home is not None else default_home())
    cwd_s = _posix(cwd if cwd is not None else default_cwd())
    extra = tuple(_posix(p) for p in extra_sensitive if p)
    text = _expand_home(text, home_s)
    try:
        return _inspect_text(text, home=home_s, cwd=cwd_s, extra=extra, depth=0)
    except Exception:
        return _inspect_emergency(text)


def inspect_argv(
    argv: list[str],
    *,
    home: str = "",
    cwd: str = "",
    extra_sensitive: tuple[str, ...] | list[str] = (),
    depth: int = 0,
) -> Hit | None:
    """Same as `inspect`, for an already-split argv (`rm`, `-rf`, `/`).

    `depth` counts nested `sh -c` scripts so a pathological
    `sh -c "sh -c \"…\""` chain stops being unwrapped after a few levels.
    """
    argv = [str(a) for a in argv if a is not None]
    if not argv:
        return None
    unwrapped = _unwrap(argv)
    if not unwrapped:
        return None
    verb = posixpath.basename(unwrapped[0]).lower()
    extra = tuple(_posix(p) for p in extra_sensitive if p)
    if verb == "rm":
        return _inspect_rm(unwrapped, home=home, cwd=cwd, extra=extra)
    if verb == "find":
        return _inspect_find(unwrapped, home=home, cwd=cwd, extra=extra)
    if verb in _SHELLS and depth < _MAX_SHELL_DEPTH:
        script = _inline_script(unwrapped)
        if script:
            return _inspect_text(script, home=home, cwd=cwd, extra=extra, depth=depth + 1)
    return None


def _inspect_text(
    text: str,
    *,
    home: str,
    cwd: str,
    extra: tuple[str, ...],
    depth: int,
) -> Hit | None:
    for argv in _commands(text):
        hit = inspect_argv(argv, home=home, cwd=cwd, extra_sensitive=extra, depth=depth)
        if hit:
            return hit
    return None


def _inline_script(argv: list[str]) -> str | None:
    """The string `sh -c` would run, or None when the shell reads a file.

    `-c` may be bundled with other short flags (`-lc`, `-xec`); the script
    is the word after it, whatever else follows as `$0`/positional args.
    """
    for i in range(1, len(argv) - 1):
        tok = argv[i]
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            return argv[i + 1]
    return None


def _inspect_emergency(text: str) -> Hit | None:
    if _EMERGENCY.search(text):
        return Hit("rm", "/", "destructive delete of a sensitive path")
    return None


def _commands(text: str) -> list[list[str]]:
    """Split a script into simple argv lists.

    Boundaries are `;` `&&` `||` `|` `&` (and `;;`, `|&`), newlines, and
    the `( ) { }` grouping tokens — the same places `/bin/sh -c` starts a
    new simple command. Newlines have to count: shlex treats them as
    whitespace, which folded `true\nrm -rf /` into one argv whose verb
    was `true`. A redirection and its target word are dropped so
    `2>/dev/null` is never read as an rm operand.
    """
    out: list[list[str]] = []
    for line in _CONTINUATION.sub(" ", text).splitlines():
        tokens = _tokenize(line)
        current: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if _is_separator(tok):
                if current:
                    out.append(current)
                    current = []
                i += 1
                continue
            if _is_redirect(tok):
                i += 2
                continue
            current.append(tok)
            i += 1
        if current:
            out.append(current)
    return out


def _is_separator(tok: str) -> bool:
    return bool(tok) and (tok in _GROUPING or set(tok) <= _SEPARATOR_CHARS)


def _is_redirect(tok: str) -> bool:
    """`>`, `>>`, `2>`-style `>` after a split-off fd, `>&`, `&>`, `<<`."""
    return bool(tok) and set(tok) <= set("<>&|") and ("<" in tok or ">" in tok)


def _tokenize(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = False
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _unwrap(argv: list[str]) -> list[str]:
    """Drop `FOO=bar`, `sudo -n`, `timeout 10`, `then`, `!` so the real verb
    is argv[0]."""
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        base = posixpath.basename(tok).lower()
        if tok in _LEADING_NOISE or base in _LEADING_NOISE:
            i += 1
            continue
        if "=" in tok and not tok.startswith("-") and base not in _WRAPPERS:
            i += 1
            continue
        if base not in _WRAPPERS:
            return argv[i:]
        i += 1
        while i < n and argv[i].startswith("-") and argv[i] != "-":
            opt = argv[i]
            i += 1
            # `sudo -u agent`, `nice -n 10` consume a value when the flag
            # is a known one that takes an operand and is not `--flag=val`.
            if "=" in opt:
                continue
            if (
                opt in {"-u", "-g", "-C", "-n", "-p", "-a", "--user", "--group", "--preserve-env"}
                and i < n
            ):
                if not argv[i].startswith("-"):
                    i += 1
        if base == "timeout" and i < n and not argv[i].startswith("-"):
            i += 1
        if base == "env":
            while i < n and "=" in argv[i] and not argv[i].startswith("-"):
                i += 1
    return []


def _inspect_rm(
    argv: list[str],
    *,
    home: str,
    cwd: str,
    extra: tuple[str, ...],
) -> Hit | None:
    no_preserve_root = False
    operands: list[str] = []
    end_opts = False
    for tok in argv[1:]:
        if not end_opts and tok == "--":
            end_opts = True
            continue
        if not end_opts and tok.startswith("-") and tok != "-":
            if tok.startswith("--"):
                name = tok.split("=", 1)[0]
                if name == "--no-preserve-root":
                    no_preserve_root = True
                continue
            continue
        operands.append(tok)
    if no_preserve_root:
        return Hit("rm", "/", "rm --no-preserve-root")
    for raw in operands:
        path = (
            _glob_root(raw, home=home, cwd=cwd)
            if any(ch in raw for ch in "*?[")
            else _resolve(raw, home=home, cwd=cwd)
        )
        if _is_sensitive(path, home=home, extra=extra):
            return Hit("rm", path or raw, "destructive delete of a sensitive path")
    return None


def _inspect_find(
    argv: list[str],
    *,
    home: str,
    cwd: str,
    extra: tuple[str, ...],
) -> Hit | None:
    destructive = False
    roots: list[str] = []
    for tok in argv[1:]:
        if tok in {"-delete", "-exec", "-ok", "-execdir", "-okdir"}:
            destructive = True
            continue
        if tok.startswith("-"):
            continue
        roots.append(tok)
    if not destructive:
        return None
    if not roots:
        roots = [cwd or "."]
    for raw in roots:
        path = _resolve(raw, home=home, cwd=cwd)
        if _is_sensitive(path, home=home, extra=extra):
            return Hit("find", path or raw, "find -delete of a sensitive path")
    return None


def _expand_home(text: str, home: str) -> str:
    """Turn `$HOME` / `${HOME}` into the jail home before tokenising.

    shlex splits `$` off `HOME` (it is not a word character), so leaving
    the dollar form for `_resolve` to rewrite would never see a single
    operand. A quoted `"$HOME"` becomes `"/home/agent"` after this, which
    is the path the shell would have passed to rm.
    """
    if not home or not text:
        return text
    return re.sub(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])", home, text)


def _resolve(raw: str, *, home: str, cwd: str) -> str:
    p = raw.strip()
    if not p:
        return ""
    if home:
        if p == "~" or p.startswith("~/"):
            p = home + p[1:]
        p = p.replace("${HOME}", home).replace("$HOME", home)
    if p.startswith("~"):
        p = posixpath.expanduser(p)
    if not p.startswith("/") and cwd:
        p = posixpath.join(cwd, p)
    if p.startswith("/"):
        return posixpath.normpath(p)
    return posixpath.normpath(p)


def _posix(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    if text != "/":
        text = text.rstrip("/")
    return text or "/"


def _glob_root(raw: str, *, home: str, cwd: str) -> str:
    """Parent a glob would expand in: `/*` → `/`, `~/Downloads/*` → that dir."""
    cut = next((i for i, ch in enumerate(raw) if ch in "*?["), len(raw))
    prefix = raw[:cut].rstrip("/")
    if not prefix:
        return _posix(cwd) if not raw.startswith("/") else "/"
    if raw.startswith("/") and not prefix.startswith("/"):
        prefix = "/" + prefix
    return _resolve(prefix or "/", home=home, cwd=cwd)


def _is_sensitive(path: str, *, home: str, extra: tuple[str, ...]) -> bool:
    """True when deleting `path` would take a sensitive directory with it.

    Extra paths (the host `$HARNESS_HOME`) are checked before the `/tmp`
    carve-out, because a throwaway home is often `/tmp/harness-demo`.
    """
    candidate = _posix(path)
    if not candidate:
        return False

    if candidate == "/":
        return True

    for extra_path in extra:
        if extra_path and (candidate == extra_path or candidate.startswith(extra_path + "/")):
            return True

    for safe in _SAFE_UNDER_SYSTEM:
        if candidate == safe or candidate.startswith(safe + "/"):
            return False

    for prefix in _SYSTEM_PREFIXES:
        if candidate == prefix or candidate.startswith(prefix + "/"):
            return True

    for parent in _HOME_PARENTS:
        if candidate == parent:
            return True

    if home and candidate == home:
        return True
    if home:
        for suffix in _HOME_STATE_SUFFIXES:
            state = home + suffix
            if candidate == state or candidate.startswith(state + "/"):
                return True
    return False
