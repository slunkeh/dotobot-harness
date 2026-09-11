"""CLI entry that installs live-talk routes before serving."""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    from .voice import patch_server

    patch_server()
    from .cli import main as _main

    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
