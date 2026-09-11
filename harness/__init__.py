"""Dotobot: self-hosted multi-bot agent harness: orchestrator, roster, routing."""

from .orchestrator import Orchestrator
from .paths import HarnessPaths
from .roster import Bot, Roster, load_roster
from .version import __version__

__all__ = ["Bot", "HarnessPaths", "Orchestrator", "Roster", "__version__", "load_roster"]
