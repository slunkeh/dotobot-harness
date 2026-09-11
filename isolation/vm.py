"""VM / microVM isolation backend — stub.

Intended shape: one KVM/QEMU (or Multipass/microVM) guest per bot, with the
shared volume mounted via virtio-fs/9p/NFS and a shared cookie jar from the
host. This is the home-server target; the laptop may only manage user-mode
QEMU/Multipass. Same interface as ProcessBackend.
"""

from __future__ import annotations

from harness.paths import HarnessPaths

from .base import BotHandle, IsolationBackend, IsolationUnavailable, Status


class VMBackend(IsolationBackend):
    id = "vm"

    def __init__(self, paths: HarnessPaths) -> None:
        self.paths = paths

    def spawn(self, bot: str, argv: list[str]) -> BotHandle:
        raise IsolationUnavailable(
            "vm backend is not implemented yet; "
            "use --backend process or container."
        )

    def stop(self, handle: BotHandle) -> None:
        raise IsolationUnavailable("vm backend is not implemented yet.")

    def status(self, handle: BotHandle) -> Status:
        return Status.UNKNOWN
