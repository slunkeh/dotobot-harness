"""Test doubles that used to live in production modules."""

from dataclasses import dataclass, field


@dataclass
class LoggingComputer:
    """Records intended computer actions instead of touching a display."""

    actions: list[str] = field(default_factory=list)

    def act(self, action: str, **params) -> str:
        detail = action + (f" {params}" if params else "")
        self.actions.append(detail)
        return f"ok: {detail}"

    def screenshot(self) -> tuple[bytes, str] | None:
        self.actions.append("screenshot")
        return None
