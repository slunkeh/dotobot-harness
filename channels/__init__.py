"""User-facing channels (desktop app first; chat platforms later)."""

from .base import available_channels
from .viewmodel import DesktopViewModel

__all__ = ["DesktopViewModel", "available_channels"]
