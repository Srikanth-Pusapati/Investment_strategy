"""Free insider-transaction signal from SEC EDGAR Form 4 filings.

A no-cost alternative to the Finnhub insider feed: pull each symbol's recent
Form 4 filings straight from EDGAR and parse the ownership XML ourselves. Only
open-market purchases (transaction code "P") and sales ("S") are counted —
option exercises (M), tax withholding (F), grants (A) and the like are ignored,
because clustered open-market BUYING is the actual smart-money tell.

Free, no API key. SEC only requires a descriptive User-Agent (SEC_USER_AGENT)
and ~10 req/s politeness. Always enabled — it's authoritative (straight from
SEC) and costs nothing, so it's the reliable insider source even when a (often
premium-gated) Finnhub key is also configured. If both this and the Finnhub
insider provider return data for the same symbol, Claude simply sees both.
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider

log = logging.getLogger("signals")

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accno}/{doc}"

_LOOKBACK_DAYS = 90
_MAX_FILINGS_PER_SYMBOL = 25     # bound SEC requests per symbol per refresh
_RESULT_TTL_S = 6 * 3600         # insider data moves slowly; cache per symbol


class EdgarInsiderProvider(SignalProvider):
    name = "insider-edgar"

    # Process-wide caches shared across instances/cycles.
    _cik_by_ticker: dict[str, int] | None = None
    _cache: dict[str, tuple[float, Signal | None]] = {}

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._headers = {"User-Agent": cfg.sec_user_agent}

    @property
    def enabled(self) -> bool:
        # Always on: free, no key, authoritative. We only need a User-Agent.
        return bool(self.cfg.sec_user_agent)

    def fetch(self, symbols: list[str]) -> list[Signal]:
        cik_map = self._ciks()
        if not cik_map:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).date()
        out: list[Signal] = []
        for symbol in symbols:
            sig = self._for_symbol(symbol.upper(), cik_map, cutoff)
            if sig is not None:
                out.append(sig)
        return out

    # -- per-symbol (cached) ------------------------------------------------ #
    def _for_symbol(self, symbol: str, cik_map: dict[str, int], cutoff) -> Signal | None:
        cached = self._cache.get(symbol)
        if cached and (time.time() - cached[0]) < _RESULT_TTL_S:
            return cached[1]

        cik = cik_map.get(symbol)
        if cik is None:
            self._cache[symbol] = (time.time(), None)
            return None

        buy_sh = sell_sh = 0
        for cik_int, accno, doc in self._recent_form4s(cik, cutoff):
            b, s = self._parse_form4(cik_int, accno, doc)
            buy_sh += b
            sell_sh += s

        total = buy_sh + sell_sh
        sig: Signal | None = None
        if total > 0:
            score = round((buy_sh - sell_sh) / total, 3)
            sig = Signal(
                kind=SignalKind.INSIDER,
                symbol=symbol,
                summary=(f"Insider open-market (90d): {buy_sh:,} sh bought / "
                         f"{sell_sh:,} sold (SEC Form 4)."),
                score=score,
                source="sec-edgar",
                data={"buy_shares": buy_sh, "sell_shares": sell_sh},
            )
        self._cache[symbol] = (time.time(), sig)
        return sig

    # -- EDGAR fetch/parse -------------------------------------------------- #
    def _recent_form4s(self, cik: int, cutoff):
        """Yield (cik, accession_nodash, raw_xml_doc) for recent Form 4 filings."""
        try:
            r = requests.get(_SUBMISSIONS_URL.format(cik=cik),
                             headers=self._headers, timeout=15)
            if r.status_code != 200:
                return
            recent = r.json().get("filings", {}).get("recent", {})
        except Exception as e:
            log.debug("EDGAR submissions failed for CIK %s: %s", cik, e)
            return

        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accs = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        count = 0
        for i, form in enumerate(forms):
            if form != "4":
                continue
            try:
                fdate = datetime.fromisoformat(dates[i][:10]).date()
            except (ValueError, IndexError):
                continue
            if fdate < cutoff:
                break  # recent arrays are newest-first; the rest are older
            accno = accs[i].replace("-", "")
            doc = docs[i]
            # primaryDocument may be the xsl-rendered path (xslF345X.../form4.xml);
            # the raw data XML is the same file without the xsl directory prefix.
            if doc.startswith("xsl") and "/" in doc:
                doc = doc.split("/", 1)[1]
            yield cik, accno, doc
            count += 1
            if count >= _MAX_FILINGS_PER_SYMBOL:
                break

    def _parse_form4(self, cik: int, accno: str, doc: str) -> tuple[int, int]:
        url = _ARCHIVE_URL.format(cik=cik, accno=accno, doc=doc)
        try:
            time.sleep(0.12)  # stay under SEC's ~10 req/s guidance
            r = requests.get(url, headers=self._headers, timeout=15)
            if r.status_code != 200:
                return 0, 0
            root = ET.fromstring(r.content)
        except Exception as e:
            log.debug("EDGAR Form 4 parse failed (%s): %s", url, e)
            return 0, 0

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
        return buy, sell

    # -- ticker -> CIK map (fetched once per process) ----------------------- #
    def _ciks(self) -> dict[str, int]:
        if EdgarInsiderProvider._cik_by_ticker is not None:
            return EdgarInsiderProvider._cik_by_ticker
        try:
            r = requests.get(_TICKERS_URL, headers=self._headers, timeout=15)
            r.raise_for_status()
            raw = r.json()
            EdgarInsiderProvider._cik_by_ticker = {
                v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()
            }
        except Exception as e:
            log.warning("EDGAR ticker map failed: %s", e)
            EdgarInsiderProvider._cik_by_ticker = {}
        return EdgarInsiderProvider._cik_by_ticker
