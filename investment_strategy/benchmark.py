"""Benchmark-relative performance tracking.

The bot's job is EXCESS return over SPY/QQQ, not raw return. This module pulls
the account's equity curve and the benchmark's price return over the same window,
computes excess return and an information ratio (excess return / tracking error),
and produces a one-line context string fed to Claude each decision cycle.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone

from alpaca.trading.requests import GetPortfolioHistoryRequest

from .config import Config
from .execution import AlpacaClient
from .models import BenchmarkStats

log = logging.getLogger("benchmark")


class BenchmarkTracker:
    def __init__(self, cfg: Config, broker: AlpacaClient, symbol: str = "QQQ",
                 period_days: int = 90):
        self.cfg = cfg
        self.broker = broker
        self.symbol = symbol
        self.period_days = period_days

    def compute(self) -> BenchmarkStats | None:
        bench_ret = self.broker.period_return_pct(self.symbol, self.period_days)
        acct_curve = self._account_equity_curve()
        if bench_ret is None or len(acct_curve) < 2:
            return None

        acct_ret = (acct_curve[-1] / acct_curve[0] - 1.0) * 100.0
        info_ratio = self._information_ratio(acct_curve)

        return BenchmarkStats(
            benchmark=self.symbol,
            period_days=self.period_days,
            account_return_pct=round(acct_ret, 2),
            benchmark_return_pct=round(bench_ret, 2),
            information_ratio=info_ratio,
        )

    def context_line(self, stats: BenchmarkStats | None) -> str:
        if stats is None:
            return f"Benchmark ({self.symbol}): insufficient history to compare yet."
        ir = f", IR {stats.information_ratio:+.2f}" if stats.information_ratio else ""
        return (
            f"Benchmark {stats.benchmark} ({stats.period_days}d): "
            f"account {stats.account_return_pct:+.2f}% vs "
            f"{stats.benchmark_return_pct:+.2f}% "
            f"= excess {stats.excess_return_pct:+.2f}%{ir}. "
            "Aim for positive excess return — selection alpha, not just market beta."
        )

    # -- internals ---------------------------------------------------------- #
    def _account_equity_curve(self) -> list[float]:
        try:
            req = GetPortfolioHistoryRequest(
                period=f"{max(1, self.period_days)}D", timeframe="1D",
            )
            hist = self.broker.trading.get_portfolio_history(req)
            # Drop None and non-positive points: a freshly funded account has
            # leading 0.0 equity samples, which would divide-by-zero in the
            # return and information-ratio math below.
            return [float(v) for v in (hist.equity or []) if v is not None and float(v) > 0.0]
        except Exception as e:
            log.warning("portfolio history failed: %s", e)
            return []

    def _information_ratio(self, acct_curve: list[float]) -> float | None:
        """Excess daily return mean / std vs benchmark daily returns, annualized.
        Approximation: aligns lengths and uses benchmark daily closes."""
        bench_closes = self.broker._daily_closes(self.symbol, len(acct_curve) + 2)
        n = min(len(acct_curve), len(bench_closes))
        if n < 10:
            return None
        a = acct_curve[-n:]
        b = bench_closes[-n:]
        excess = [
            (a[i] / a[i - 1] - 1.0) - (b[i] / b[i - 1] - 1.0)
            for i in range(1, n)
        ]
        try:
            mean, sd = statistics.mean(excess), statistics.stdev(excess)
        except statistics.StatisticsError:
            return None
        if sd == 0:
            return None
        return round((mean / sd) * (252 ** 0.5), 2)
