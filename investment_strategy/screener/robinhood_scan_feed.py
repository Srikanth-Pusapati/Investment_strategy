"""Discovery screener: the user's saved Robinhood scanners (C.6).

A momentum/technical discovery leg alongside congress/insider/movers: define
scans once by hand in the Robinhood app or Legend (e.g. "RSI breakout + volume
spike"), and this screener runs them read-only each cycle via the Agentic MCP
(get_scans -> run_scan). The scan criteria live server-side with Robinhood —
nothing to configure here beyond adding "robinhood_scans" to SCREENER_SOURCES.

Surfaced names are candidates only: the signal layer still scores them and the
risk gate still has final say. Read-only by construction — every call goes
through RobinhoodReader.call_json, which refuses non-read tools. Disabled (a
no-op) unless ROBINHOOD_ENABLED=on with working credentials; quietly returns
nothing when the user has no saved scans.
"""
from __future__ import annotations

import logging

from ..config import Config
from ..models import Candidate, is_valid_ticker
from ..portfolio import RobinhoodReader
from .base import Screener

log = logging.getLogger("screener")

_MAX_SCANS = 5            # run at most this many saved scans per cycle
_MAX_PER_SCAN = 15        # take the top rows of each scan's result
# Between "Daily movers" (0.6) and "100 most popular" (0.35): a user-authored
# technical screen is a deliberate criterion, but still only a discovery lean.
_SCORE = 0.5


class RobinhoodScanScreener(Screener):
    name = "robinhood_scans"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._reader = RobinhoodReader(cfg)

    @property
    def enabled(self) -> bool:
        return self._reader.enabled

    def scan(self) -> list[Candidate]:
        scans = self._reader.call_json("get_scans", {})
        rows = scans if isinstance(scans, list) else None
        if rows is None and isinstance(scans, dict):
            rows = next((v for v in scans.values() if isinstance(v, list)), None)
        if not rows:
            log.info("Robinhood scans: none saved (define scans in the RH app to use this leg).")
            return []

        merged: dict[str, Candidate] = {}
        for scan in rows[:_MAX_SCANS]:
            if not isinstance(scan, dict):
                continue
            scan_id = scan.get("id") or scan.get("scan_id")
            title = str(scan.get("title") or scan.get("name") or scan_id or "scan")
            if not scan_id:
                continue
            for sym in self._run(str(scan_id)):
                why = f"Robinhood scan '{title}' match (user-defined technical screen)"
                cand = merged.get(sym)
                if cand is None:
                    merged[sym] = Candidate(
                        symbol=sym, sources=[self.name], reason=why, score=_SCORE,
                    )
                elif why not in cand.reason:
                    # Hit by multiple scans -> corroboration worth surfacing.
                    cand.reason += f"; also '{title}'"
        log.info("Robinhood scans -> %d name(s) from %d scan(s).",
                 len(merged), min(len(rows), _MAX_SCANS))
        return list(merged.values())

    def _run(self, scan_id: str) -> list[str]:
        result = self._reader.call_json("run_scan", {"scan_id": scan_id})
        if not isinstance(result, dict):
            return []
        instruments = None
        for key in ("instruments", "results", "rows"):
            if isinstance(result.get(key), list):
                instruments = result[key]
                break
        if instruments is None:
            instruments = next(
                (v for v in result.values() if isinstance(v, list)), [],
            )
        out: list[str] = []
        for row in instruments[:_MAX_PER_SCAN]:
            sym = ""
            if isinstance(row, dict):
                sym = str(row.get("ticker") or row.get("symbol") or "").upper()
            elif isinstance(row, str):
                sym = row.upper()
            if sym and is_valid_ticker(sym):
                out.append(sym)
        return out
