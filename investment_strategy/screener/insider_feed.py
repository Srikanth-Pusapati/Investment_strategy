"""Discovery screener: clustered open-market insider BUYING and SELLING.

signals/insider_edgar.py asks "are insiders trading THIS symbol?"; this asks
"which symbols are insiders trading right now?" — the discovery direction. It
pulls SEC EDGAR's market-wide "latest filings" feed for Form 4, then reads each
filing's own XML for the issuer's ticker and both the P-coded (open-market
purchase) and S-coded (open-market sale) share counts.

Clustered buying is the classic smart-money tell (positive score). Selling is
the discovery path for the DOWNSIDE — a bot that can't short must express a
bearish thesis with a long put, and it can only do that if bearish names are
surfaced in the first place. But sells are genuinely noisier than buys (10b5-1
plans, tax, diversification — "execs sell for many reasons, buy for one"), so
we discount a sell cluster by _SELL_WEIGHT and require a real cluster
(_MIN_SELLERS distinct sellers) before surfacing a negative score. Using only
open-market S (not F=tax, M=exercise, G=gift) already strips most routine noise;
the corroboration bar downstream (the LLM won't buy a put on one signal alone)
is the second line of defence.

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
from ..models import Candidate, is_valid_ticker
from .base import Screener

log = logging.getLogger("screener")

# Sells are noisier than buys — discount a sell cluster and require a real one.
_SELL_WEIGHT = 0.6      # a max sell cluster scores 0.6 where a max buy cluster scores 1.0
_MIN_SELLERS = 2        # a lone open-market seller is noise; need >=2 distinct sellers

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
        limit = self.cfg.screener.insider_scan_limit
        # Surface the raw feed size so an empty/blocked EDGAR pull is visible
        # rather than silently becoming "0 candidates" downstream.
        log.info("EDGAR Form-4 feed -> %d filing(s) (scanning up to %d).",
                 len(filings), limit)
        if not filings:
            return []

        buys: dict[str, int] = defaultdict(int)
        sells: dict[str, int] = defaultdict(int)
        n_buyers: dict[str, int] = defaultdict(int)   # distinct BUY filings per ticker
        n_sellers: dict[str, int] = defaultdict(int)  # distinct SELL filings per ticker
        for cik, accno, acc_dash in filings[:limit]:
            sym, buy_sh, sell_sh = self._parse_submission(cik, accno, acc_dash)
            if not sym:
                continue
            if buy_sh > 0:
                buys[sym] += buy_sh
                n_buyers[sym] += 1
            if sell_sh > 0:
                sells[sym] += sell_sh
                n_sellers[sym] += 1

        candidates: list[Candidate] = []
        for sym in set(buys) | set(sells):
            nb, ns = n_buyers[sym], n_sellers[sym]
            # Buy score on cluster breadth, saturating at 3+ filings (a single
            # buy is weak, several is a real cluster). Identical to the old
            # buy-only behaviour whenever there is no qualifying sell cluster.
            buy_score = min(nb, 3) / 3.0 if nb else 0.0
            # Sell score: same cluster shape, but discounted and gated on a real
            # cluster of distinct sellers so a lone routine sale never surfaces.
            sell_score = (
                min(ns, 3) / 3.0 * _SELL_WEIGHT if ns >= _MIN_SELLERS else 0.0
            )
            net = round(buy_score - sell_score, 3)
            if net == 0.0:
                continue  # no buys and no qualifying sell cluster — nothing to say
            if net > 0:
                reason = (
                    f"Insider: {nb} open-market buy filing(s), {buys[sym]:,} sh"
                    + (f" vs {ns} sell filing(s)" if ns else "")
                    + " (SEC Form 4, last filings feed)."
                )
            else:
                reason = (
                    f"Insider: {ns} open-market SELL filing(s), {sells[sym]:,} sh"
                    + (f" vs {nb} buy filing(s)" if nb else "")
                    + " — bearish cluster (SEC Form 4, last filings feed)."
                )
            candidates.append(Candidate(
                symbol=sym, sources=[self.name], reason=reason, score=net,
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

    # -- per-filing parse (ticker + buy/sell shares straight from the XML) -- #
    def _parse_submission(
        self, cik: str, accno: str, acc_dash: str
    ) -> tuple[str | None, int, int]:
        """Return (ticker, open_market_buy_shares, open_market_sell_shares).
        Only P (purchase) and S (sale) codes count — F=tax, M=exercise, G=gift
        are deliberately excluded as non-discretionary / non-informational."""
        url = _SUBMISSION_TXT.format(cik=cik, accno=accno, acc_dash=acc_dash)
        try:
            time.sleep(0.12)  # stay under SEC's ~10 req/s guidance
            r = requests.get(url, headers=self._headers, timeout=15)
            if r.status_code != 200:
                return None, 0, 0
            match = _OWNERSHIP_RE.search(r.text)
            if not match:
                return None, 0, 0
            root = ET.fromstring(match.group(0))
        except Exception as e:
            log.debug("EDGAR submission parse failed (%s): %s", url, e)
            return None, 0, 0

        sym = (root.findtext(".//issuer/issuerTradingSymbol") or "").strip().upper()
        # Unlisted issuers file with a literal "N/A" symbol — drop those too.
        if not is_valid_ticker(sym):
            return None, 0, 0
        buy = sell = 0
        for t in root.findall(".//nonDerivativeTransaction"):
            code = (t.findtext(".//transactionCoding/transactionCode") or "").upper()
            shares = t.findtext(".//transactionAmounts/transactionShares/value")
            try:
                n = int(float(shares))
            except (TypeError, ValueError):
                continue
            if code == "P":        # open-market purchase
                buy += n
            elif code == "S":      # open-market sale
                sell += n
        return sym, buy, sell
