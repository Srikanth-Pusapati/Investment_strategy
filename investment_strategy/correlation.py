"""Pairwise-correlation guard (R.2) — the sector cap's finer-grained sibling.

The sector cap stops the book from stacking one SECTOR; it says nothing about
two names in different sectors that move together anyway (a high-beta software
name and a semiconductor both riding the same rates/AI factor). Under the
aggressive posture (12% positions, 50% sector cap) four or five "different"
tickers can quietly be one bet. This module measures that directly: the
correlation of daily returns between a BUY candidate and each already-held
satellite over ~90 trading days.

Deterministic and advisory-in, hard-out: the orchestrator computes the max
correlation here and hands the NUMBER to RiskManager, which enforces the cap
(Claude proposes, risk disposes — unchanged). Fail-open by design: missing
price history returns None and the guard is skipped, because a data outage
must not freeze all new buying (the same posture as the earnings/sector
lookups).

Per-cycle caching: return series are fetched at most once per symbol per
decision cycle (call new_cycle() at the top of each cycle), so guarding N
proposals against M holdings costs N+M history fetches, not N*M.
"""
from __future__ import annotations

import logging
import statistics

log = logging.getLogger("correlation")

#: Trading days of history the correlation is measured over.
LOOKBACK_DAYS = 90
#: Minimum overlapping return observations for a correlation to count — below
#: this the estimate is noise (a recent IPO, a big data gap) and we fail open.
MIN_OVERLAP = 40


class CorrelationGuard:
    """Max daily-return correlation between a candidate and the held book."""

    def __init__(self, broker):
        self.broker = broker                       # needs .daily_close_series
        self._returns: dict[str, dict[str, float] | None] = {}

    def new_cycle(self) -> None:
        """Drop the per-cycle series cache (prices move; cycles are ~15m)."""
        self._returns.clear()

    # -- public -------------------------------------------------------------- #
    def max_correlation(
        self, symbol: str, held: list[str],
    ) -> tuple[float, str] | None:
        """(highest correlation, held symbol it's with) between `symbol` and any
        name in `held`, or None when nothing is comparable (no holdings, or not
        enough overlapping history anywhere) — the caller treats None as
        fail-open. The caller is responsible for excluding the core ETF and the
        candidate itself from `held`."""
        cand = self._daily_returns(symbol)
        if not cand:
            return None
        best: tuple[float, str] | None = None
        for other in held:
            if other == symbol:
                continue        # a top-up isn't a new bet; never self-compare
            corr = self._corr(cand, self._daily_returns(other))
            if corr is None:
                continue
            if best is None or corr > best[0]:
                best = (corr, other)
        return best

    # -- internals ----------------------------------------------------------- #
    def _daily_returns(self, symbol: str) -> dict[str, float] | None:
        """ISO-date -> daily return for `symbol`, cached for the cycle. None on
        missing/short history (never raises — this feeds a fail-open guard)."""
        if symbol in self._returns:
            return self._returns[symbol]
        rets: dict[str, float] | None = None
        try:
            pairs = self.broker.daily_close_series(symbol, LOOKBACK_DAYS + 1)
            if len(pairs) >= MIN_OVERLAP + 1:
                rets = {}
                for (_, prev), (date, close) in zip(pairs, pairs[1:]):
                    if prev > 0 and close > 0:
                        rets[date] = close / prev - 1.0
                if len(rets) < MIN_OVERLAP:
                    rets = None
        except Exception as e:  # noqa: BLE001 — guard must never break a cycle
            log.warning("correlation history for %s failed: %s", symbol, e)
            rets = None
        self._returns[symbol] = rets
        return rets

    @staticmethod
    def _corr(
        a: dict[str, float] | None, b: dict[str, float] | None,
    ) -> float | None:
        """Pearson correlation over the dates both series share (inner join, so
        differing calendars/halts can't misalign the pairs)."""
        if not a or not b:
            return None
        dates = sorted(a.keys() & b.keys())
        if len(dates) < MIN_OVERLAP:
            return None
        xs = [a[d] for d in dates]
        ys = [b[d] for d in dates]
        try:
            return statistics.correlation(xs, ys)
        except statistics.StatisticsError:   # zero variance (e.g. a flat series)
            return None
