"""SignalProvider interface. Each data source implements fetch() and returns
normalized Signal objects. Providers must degrade gracefully — a missing API key
or a flaky endpoint returns [] (logged), never raises into the main loop.
"""
from __future__ import annotations

import abc
import logging

from ..models import Signal

log = logging.getLogger("signals")


class SignalProvider(abc.ABC):
    """Base for all signal sources."""

    name: str = "base"
    #: True for market-wide sources (macro) whose signals are not per-symbol.
    market_wide: bool = False

    @abc.abstractmethod
    def fetch(self, symbols: list[str]) -> list[Signal]:
        """Return signals for the given symbols (ignored if market_wide)."""

    @property
    def enabled(self) -> bool:
        """Override to gate on the presence of an API key, etc."""
        return True

    def safe_fetch(self, symbols: list[str]) -> list[Signal]:
        if not self.enabled:
            log.debug("%s disabled (no credentials); skipping.", self.name)
            return []
        try:
            return self.fetch(symbols)
        except Exception as e:
            log.warning("%s.fetch failed: %s", self.name, e)
            return []
