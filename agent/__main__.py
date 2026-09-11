"""Bot process entrypoint: `python -m agent --bot <name>`.

The orchestrator's process isolation backend spawns one of these per bot.
"""

from __future__ import annotations

import argparse
import sys

from harness.netguard import install_safe_redirects
from harness.paths import HarnessPaths
from harness.roster import RosterError, load_roster
from isolation.process_identity import boot_identity_error

from .runtime import build_agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent")
    parser.add_argument("--bot", required=True, help="bot name from the roster")
    parser.add_argument("--roster", default="roster.toml", help="path to roster.toml")
    parser.add_argument(
        "--home", default=None, help="harness home (default: $HARNESS_HOME or ./shared)"
    )
    parser.add_argument("--reply-timeout", type=float, default=30.0)
    parser.add_argument(
        "--generation-token",
        default=None,
        help="spawn identity token; must match $HARNESS_GENERATION_TOKEN",
    )
    args = parser.parse_args(argv)

    # Connector and provider calls run in this process with credentials in
    # their headers; urllib's stock opener would forward those to whatever
    # host a 3xx named. Install the origin-checking opener before any of
    # them can run.
    install_safe_redirects()

    # Generation-token identity: the spawning backend passes the same random
    # token via env AND argv; a lone or mismatched half means this process is
    # not the child that backend minted, so refuse to boot.
    problem = boot_identity_error(args.generation_token)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    paths = HarnessPaths.resolve(args.home)
    try:
        roster = load_roster(args.roster)
        bot = roster.get(args.bot)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    paths.ensure_layout(roster.names())
    agent = build_agent(paths, bot, reply_timeout=args.reply_timeout)
    agent.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
