"""Screener interface. Each source scans the market and returns Candidate
symbols it surfaced. Like SignalProvider, a screener must degrade gracefully —
a missing API key or a flaky endpoint returns [] (logged), never raises into the
decision cycle.
"""
from __future__ import annotations

import abc
import logging

from ..models import Candidate

log = logging.getLogger("screener")


class Screener(abc.ABC):
    """Base for all discovery sources."""

    name: str = "base"

    @abc.abstractmethod
    def scan(self) -> list[Candidate]:
        """Return candidate symbols surfaced from a market-wide scan."""

    @property
    def enabled(self) -> bool:
        """Override to gate on the presence of an API key, etc."""
        return True

    def safe_scan(self) -> list[Candidate]:
        if not self.enabled:
            log.info("%s screener disabled (no credentials); skipping.", self.name)
            return []
        try:
            return self.scan()
        except Exception as e:
            log.warning("%s.scan failed: %s", self.name, e)
            return []
