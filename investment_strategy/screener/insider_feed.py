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

Resilience (run-8 C-2): sec.gov times out in short bursts — on 2026-09-25 three
timeouts made 3/8 cycles read `insider UNHEALTHY`, a pre-registered validity
breach for what was a network blip. The feed GET is therefore tried twice (a
1 s pause between), every successful scan leaves `insider_snapshot.json` next
to the state file, and with INSIDER_CACHE_MAX_AGE_H > 0 a pull that still fails
serves that snapshot (exposed as `cached_age_h`, never as `degraded`) when it is
young enough. Knob 0 = run-7 behaviour: a failed pull is UNHEALTHY, period.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone

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
# Last good scan, written next to the state file (see Config.state_file).
_SNAPSHOT_NAME = "insider_snapshot.json"


class InsiderFeedScreener(Screener):
    name = "insider"

    # An EDGAR pull that fails or times out (>= this many seconds with 0 rows)
    # is a feed OUTAGE for the cycle, not "no insider activity".
    _DEGRADED_AFTER_S = 15.0
    # C-2: the current-filings GET is tried this many times, pausing between
    # attempts, before the pull is declared degraded. Class attributes so a
    # test can zero the pause on its instance.
    _ATTEMPTS = 2
    _RETRY_PAUSE_S = 1.0

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._headers = {"User-Agent": cfg.sec_user_agent}
        # Set per scan when the feed pull failed; read by the orchestrator's
        # FEEDS line so the cycle reports 'insider UNHEALTHY (edgar: timeout)'
        # instead of a silent 0-candidate healthy feed (run-6, Aug 25 review).
        self.degraded: str | None = None
        # C-2: age in hours of the snapshot served in place of a failed pull;
        # None on every normal AND every degraded cycle. The orchestrator's
        # FEEDS line renders it as 'insider=cached(<h>h)'.
        self.cached_age_h: float | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.sec_user_agent)

    def scan(self) -> list[Candidate]:
        self.degraded = None
        self.cached_age_h = None
        filings = self._recent_form4_filings()
        limit = self.cfg.screener.insider_scan_limit
        # Surface the raw feed size so an empty/blocked EDGAR pull is visible
        # rather than silently becoming "0 candidates" downstream.
        log.info("EDGAR Form-4 feed -> %d filing(s) (scanning up to %d).",
                 len(filings), limit)
        if not filings:
            if self.degraded:
                cached = self._cached_candidates()
                if cached is not None:
                    return cached
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
        # A pull that returned filings is a good scan — whatever it netted to —
        # and becomes the snapshot a later outage can serve.
        self._write_snapshot(candidates)
        return candidates

    # -- EDGAR "latest filings" feed --------------------------------------- #
    def _recent_form4_filings(self) -> list[tuple[str, str, str]]:
        """Return (cik, accession_nodash, accession_dashed) for the newest Form 4s.
        The GET is tried `_ATTEMPTS` times with `_RETRY_PAUSE_S` between them;
        only when the LAST attempt fails is the pull marked degraded."""
        t0 = time.monotonic()
        for attempt in range(1, self._ATTEMPTS + 1):
            reason, detail, text = self._fetch_current_feed()
            if reason is None:
                out = self._parse_feed(text)
                if out or (time.monotonic() - t0) < self._DEGRADED_AFTER_S:
                    return out
                reason = "timeout"               # slow 200 with an empty body
            if attempt < self._ATTEMPTS:
                log.info("EDGAR Form-4 feed %s on attempt %d/%d — retrying in %.0fs.",
                         reason, attempt, self._ATTEMPTS, self._RETRY_PAUSE_S)
                time.sleep(self._RETRY_PAUSE_S)
                continue
            self._mark_degraded(reason, t0, detail=detail)
        return []

    def _fetch_current_feed(self) -> tuple[str | None, str, str]:
        """One GET of the current-filings atom feed -> (failure_reason | None,
        detail, body). A non-200 is a failure like an exception is."""
        try:
            r = requests.get(_CURRENT_URL, headers=self._headers, timeout=20, params={
                "action": "getcurrent", "type": "4", "owner": "include",
                "count": "100", "output": "atom",
            })
        except Exception as e:
            reason = "timeout" if isinstance(e, requests.exceptions.Timeout) else "error"
            return reason, str(e), ""
        if r.status_code != 200:
            return f"http {r.status_code}", "", ""
        return None, "", r.text

    @staticmethod
    def _parse_feed(text: str) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for m in _HREF_RE.finditer(text):
            cik, accno, acc_dash = m.group(1), m.group(2), m.group(3)
            if acc_dash in seen:
                continue
            seen.add(acc_dash)
            out.append((cik, accno, acc_dash))
        return out

    def _mark_degraded(self, reason: str, t0: float, detail: str = "") -> None:
        """Latch the per-cycle outage reason and say so at WARNING — an empty
        EDGAR pull must surface as a feed failure, never as 'no insiders'."""
        self.degraded = f"edgar: {reason}"
        log.warning(
            "EDGAR Form-4 feed returned 0 filings after %.0fs (%s)%s — insider "
            "screener UNHEALTHY this cycle.",
            time.monotonic() - t0, reason, f": {detail}" if detail else "",
        )

    # -- C-2: last-good snapshot ------------------------------------------- #
    def _snapshot_path(self) -> str | None:
        """`<dirname(state_file)>/insider_snapshot.json`, or None when the config
        carries no state file (bare test fixtures) — then no snapshot I/O at all."""
        state_file = getattr(self.cfg, "state_file", "") or ""
        if not state_file:
            return None
        return os.path.join(os.path.dirname(state_file) or ".", _SNAPSHOT_NAME)

    def _write_snapshot(self, candidates: list[Candidate]) -> None:
        """Persist the scan atomically (tmp + os.replace) so a crash mid-write
        can never leave a half-written file for the next outage to serve."""
        path = self._snapshot_path()
        if path is None:
            return
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "candidates": [c.model_dump() for c in candidates],
                }, fh)
            os.replace(tmp, path)
        except Exception as e:
            log.warning("insider snapshot write failed (%s): %s", path, e)

    def _cached_candidates(self) -> list[Candidate] | None:
        """On a degraded pull: the snapshot's candidates when INSIDER_CACHE_MAX_AGE_H
        > 0 and the snapshot is at most that old — the cycle is then NOT degraded
        and `cached_age_h` says how stale the rows are. None = fall through to
        today's behaviour (degraded stays latched, [] goes out)."""
        max_age_h = float(getattr(self.cfg, "insider_cache_max_age_h", 0.0) or 0.0)
        path = self._snapshot_path()
        if max_age_h <= 0 or path is None:
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                snap = json.load(fh)
            ts = datetime.fromisoformat(snap["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_h = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0)
            rows = [Candidate.model_validate(r) for r in snap.get("candidates", [])]
        except FileNotFoundError:
            return None
        except Exception as e:
            log.warning("insider snapshot unreadable (%s): %s — not serving it.", path, e)
            return None
        if age_h > max_age_h:
            log.info("insider snapshot is %.1fh old (cap %.1fh) — too stale to serve; "
                     "cycle stays UNHEALTHY.", age_h, max_age_h)
            return None
        reason = (self.degraded or "").removeprefix("edgar: ")
        self.degraded = None
        self.cached_age_h = age_h
        log.info("INSIDER CACHED: serving the %.1fh-old Form-4 snapshot (%d candidates) "
                 "— EDGAR %s", age_h, len(rows), reason)
        return rows

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
