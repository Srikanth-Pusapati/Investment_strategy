"""Discovery screener: clustered open-market insider BUYING, across all issuers.

signals/insider_edgar.py asks "are insiders buying THIS symbol?"; this asks
"which symbols are insiders buying right now?" — the discovery direction. It
pulls SEC EDGAR's market-wide "latest filings" feed for Form 4, then reads each
filing's own XML for the issuer's ticker and the P-coded (open-market purchase)
share counts. Clustered buying is the smart-money tell; sells are ignored for
surfacing (execs sell for many reasons, buy for one).

Free, no API key — only a descriptive SEC User-Agent and ~10 req/s politeness.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict

import requests

from ..config import Config
from ..models import Candidate
from .base import Screener

log = logging.getLogger("screener")

_CURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
_SUBMISSION_TXT = "https://www.sec.gov/Archives/edgar/data/{cik}/{accno}/{acc_dash}.txt"
# href in the atom feed: .../Archives/edgar/data/<cik>/<accno>/<acc-dash>-index.htm
_HREF_RE = re.compile(r"/edgar/data/(\d+)/(\d+)/([\d-]+)-index")
_OWNERSHIP_RE = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.DOTALL)


class InsiderFeedScreener(Screener):
    name = "insider"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._headers = {"User-Agent": cfg.sec_user_agent}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.sec_user_agent)

    def scan(self) -> list[Candidate]:
        filings = self._recent_form4_filings()
        if not filings:
            return []

        buys: dict[str, int] = defaultdict(int)
        filers: dict[str, int] = defaultdict(int)   # distinct filings per ticker
        for cik, accno, acc_dash in filings[: self.cfg.screener.options_flow_scan_limit]:
            sym, buy_sh = self._parse_submission(cik, accno, acc_dash)
            if sym and buy_sh > 0:
                buys[sym] += buy_sh
                filers[sym] += 1

        candidates: list[Candidate] = []
        for sym, sh in buys.items():
            n = filers[sym]
            # Score on cluster breadth (number of buying filings), saturating at 3+;
            # a single small buy is weak, several is a real cluster.
            score = round(min(n, 3) / 3.0, 3)
            candidates.append(Candidate(
                symbol=sym,
                sources=[self.name],
                reason=f"Insider: {n} open-market buy filing(s), {sh:,} sh "
                       "(SEC Form 4, last filings feed).",
                score=score,
            ))
        return candidates

    # -- EDGAR "latest filings" feed --------------------------------------- #
    def _recent_form4_filings(self) -> list[tuple[str, str, str]]:
        """Return (cik, accession_nodash, accession_dashed) for the newest Form 4s."""
        try:
            r = requests.get(_CURRENT_URL, headers=self._headers, timeout=20, params={
                "action": "getcurrent", "type": "4", "owner": "include",
                "count": "100", "output": "atom",
            })
            if r.status_code != 200:
                log.debug("EDGAR getcurrent HTTP %s", r.status_code)
                return []
        except Exception as e:
            log.debug("EDGAR getcurrent failed: %s", e)
            return []

        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for m in _HREF_RE.finditer(r.text):
            cik, accno, acc_dash = m.group(1), m.group(2), m.group(3)
            if acc_dash in seen:
                continue
            seen.add(acc_dash)
            out.append((cik, accno, acc_dash))
        return out

    # -- per-filing parse (ticker + buys straight from the XML) ------------ #
    def _parse_submission(self, cik: str, accno: str, acc_dash: str) -> tuple[str | None, int]:
        url = _SUBMISSION_TXT.format(cik=cik, accno=accno, acc_dash=acc_dash)
        try:
            time.sleep(0.12)  # stay under SEC's ~10 req/s guidance
            r = requests.get(url, headers=self._headers, timeout=15)
            if r.status_code != 200:
                return None, 0
            match = _OWNERSHIP_RE.search(r.text)
            if not match:
                return None, 0
            root = ET.fromstring(match.group(0))
        except Exception as e:
            log.debug("EDGAR submission parse failed (%s): %s", url, e)
            return None, 0

        sym = (root.findtext(".//issuer/issuerTradingSymbol") or "").strip().upper()
        if not sym:
            return None, 0
        buy = 0
        for t in root.findall(".//nonDerivativeTransaction"):
            code = (t.findtext(".//transactionCoding/transactionCode") or "").upper()
            if code != "P":      # open-market purchase only
                continue
            shares = t.findtext(".//transactionAmounts/transactionShares/value")
            try:
                buy += int(float(shares))
            except (TypeError, ValueError):
                continue
        return sym, buy
