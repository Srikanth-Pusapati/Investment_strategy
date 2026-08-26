"""Book beta measurement (run-6 item 7a, Aug 25 review §6.6).

The Aug 12-21 and run-5 windows could not be judged against exposure because
nothing ever measured it: the book's realized down-capture (3.9x SPY on
Aug 18) was only visible after the fact. This module measures the book's
ex-ante market exposure every decision cycle:

  * per-holding beta = cov(r_i, r_bench) / var(r_bench) over the last
    BETA_LOOKBACK_DAYS daily returns, vs SPY, QQQ and IWM;
  * shrunk toward 1.0 (beta_shrunk = SHRINK * beta + (1 - SHRINK)) — 60
    daily observations of a single name are noisy, and a Bayesian pull to
    the market beta is the standard remedy (Vasicek / Bloomberg 2/3-1/3);
  * book beta = sum(w_i * beta_i) with w_i = market value / equity, so a
    50%-invested book of beta-1 names reads 0.5 (cash is beta 0 and the
    hedge ETF enters at its own, negative, measured beta).

Option rows: a per-leg delta proxy needs a quote + greeks read per
contract per cycle, which is NOT cheap on the Alpaca data plan we use, so
option structures are counted at their DEBIT NOTIONAL (net market value of
the legs, signed) at the UNDERLYING's beta. That overstates a spread's
exposure a little and understates a deep-ITM long call's; both errors are
bounded by the premium caps (~1% of equity per underlying) and are stated
in the reading's `notes` so the log line is honest about them.

Return series come from CorrelationGuard's per-cycle cache when one is
supplied (same broker.daily_close_series fetch — no double-fetch), else
from a private cache on this object. Everything fails open: an unavailable
series makes the holding an `unknown` (assumed beta 1.0, listed on the log
line); a failed benchmark makes that column None; the caller never blocks
trading on this reading.
"""
from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("beta")

#: Trading days of daily returns the beta is measured over.
BETA_LOOKBACK_DAYS = 60
#: Minimum overlapping observations for a beta to count (else unknown).
MIN_OVERLAP = 30
#: Shrinkage weight on the sample beta (the rest goes to 1.0).
SHRINK = 0.8
#: The three benchmark columns, in log-line order.
BENCHMARKS: tuple[str, ...] = ("SPY", "QQQ", "IWM")
#: Beta assumed for a holding whose series could not be read.
ASSUMED_BETA = 1.0

# Mirror of ledger._OCC_RE: an OCC option symbol is <root><yymmdd><C|P><8>.
_OCC_RE = re.compile(r"^([A-Z][A-Z0-9.]{0,5})\d{6}[CP]\d{8}$")


def occ_underlying(symbol: str) -> str | None:
    """Underlying root of an OCC option symbol, or None for an equity."""
    m = _OCC_RE.match((symbol or "").strip().upper())
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Pure arithmetic (unit-tested with synthetic series)
# --------------------------------------------------------------------------- #
def daily_returns(pairs: list[tuple[str, float]]) -> dict[str, float]:
    """ISO-date -> simple daily return from (date, close) pairs."""
    out: dict[str, float] = {}
    for (_, prev), (date, close) in zip(pairs, pairs[1:]):
        if prev and prev > 0 and close and close > 0:
            out[date] = close / prev - 1.0
    return out


def raw_beta(
    asset: dict[str, float] | None, bench: dict[str, float] | None,
    lookback: int = BETA_LOOKBACK_DAYS, min_overlap: int = MIN_OVERLAP,
) -> float | None:
    """Sample beta over the last `lookback` dates both series share (inner
    join so differing calendars can't misalign the pairs). None when the
    overlap is too short or the benchmark has no variance."""
    if not asset or not bench:
        return None
    dates = sorted(asset.keys() & bench.keys())[-lookback:]
    if len(dates) < min_overlap:
        return None
    xs = [bench[d] for d in dates]
    ys = [asset[d] for d in dates]
    try:
        var = statistics.variance(xs)
    except statistics.StatisticsError:
        return None
    if var <= 0:
        return None
    return statistics.covariance(xs, ys) / var


def shrink(beta: float | None, factor: float = SHRINK) -> float | None:
    """beta_shrunk = factor * beta + (1 - factor) * 1.0 (None passes through)."""
    if beta is None:
        return None
    return factor * beta + (1.0 - factor) * 1.0


def aggregate(weights: dict[str, float], betas: dict[str, float | None]) -> float:
    """sum(w_i * beta_i); an unknown (None) beta counts at ASSUMED_BETA."""
    total = 0.0
    for sym, w in weights.items():
        b = betas.get(sym)
        total += w * (ASSUMED_BETA if b is None else b)
    return total


def post_trade_beta(
    book_beta: float, add_notional: float, equity: float, cand_beta: float,
) -> float:
    """Book beta after adding `add_notional` of a `cand_beta` name."""
    if equity <= 0:
        return book_beta
    return book_beta + (add_notional / equity) * cand_beta


def beta_cap_room(
    book_beta: float, cap: float, equity: float, cand_beta: float,
) -> float | None:
    """Largest notional of a `cand_beta` name that keeps the post-trade book
    beta <= cap. None = unconstrained (a zero/negative-beta buy can't breach
    the cap); 0.0 when the book is already at/over it."""
    if cand_beta <= 0:
        return None
    room = (cap - book_beta) * equity / cand_beta
    return max(0.0, room)


def hedge_target_notional(
    beta_spy: float, target: float, equity: float, ceiling_pct: float,
) -> float:
    """Inverse-ETF notional that would bring the book's SPY-beta from
    `beta_spy` down to `target`, i.e. max(0, beta - target) * equity, capped
    at ceiling_pct% of equity. Assumes the hedge instrument carries a SPY-
    beta of about -1 (PSQ, the 1x inverse QQQ, measures ~-1.1: a ~10%
    over-hedge on the gap, well inside the hysteresis band)."""
    gap = max(0.0, beta_spy - target) * equity
    ceiling = max(0.0, ceiling_pct) / 100.0 * max(0.0, equity)
    return min(gap, ceiling)


def hedge_signal(
    beta_spy: float | None, target: float, band: float, hedge_held: bool,
) -> str:
    """Hysteresis read for the beta hedge: 'arm' above target + band,
    'unwind' below target - band while a hedge is held, else 'hold'
    (no change; an unavailable beta always holds)."""
    if beta_spy is None:
        return "hold"
    if beta_spy > target + band:
        return "arm"
    if hedge_held and beta_spy < target - band:
        return "unwind"
    return "hold"


# --------------------------------------------------------------------------- #
# The per-cycle reading
# --------------------------------------------------------------------------- #
@dataclass
class BookBetaReading:
    spy: float | None = None
    qqq: float | None = None
    iwm: float | None = None
    invested_pct: float = 0.0
    # symbol -> {bench: shrunk beta or None}
    betas: dict[str, dict[str, float | None]] = field(default_factory=dict)
    # symbol -> weight (market value / equity), options folded into their underlying
    weights: dict[str, float] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reason: str = ""          # non-empty = unavailable

    @property
    def available(self) -> bool:
        return not self.reason and self.spy is not None

    def line(self) -> str:
        """The ONE greppable log line per decision cycle."""
        if not self.available:
            return f"BOOK BETA: unavailable ({self.reason or 'no SPY beta'})"
        def _f(v):
            return "n/a" if v is None else f"{v:.2f}"
        s = (
            f"BOOK BETA: spy={_f(self.spy)} qqq={_f(self.qqq)} "
            f"iwm={_f(self.iwm)} invested={self.invested_pct:.1f}%"
        )
        if self.unknown:
            s += (
                f" unknown={','.join(self.unknown)}"
                f"(assumed {ASSUMED_BETA:.1f})"
            )
        if self.notes:
            s += " [" + "; ".join(self.notes) + "]"
        return s

    def to_dict(self) -> dict:
        return {
            "spy": self.spy, "qqq": self.qqq, "iwm": self.iwm,
            "invested_pct": round(self.invested_pct, 3),
            "betas": {
                s: {k: (None if v is None else round(v, 4)) for k, v in d.items()}
                for s, d in self.betas.items()
            },
            "weights": {s: round(w, 5) for s, w in self.weights.items()},
            "unknown": list(self.unknown),
            "reason": self.reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }


class BookBeta:
    """Per-cycle-cached beta reader. `corr_guard` (CorrelationGuard) shares
    its daily-return cache when given; otherwise this object caches on its
    own — call new_cycle() at the top of each decision cycle either way."""

    def __init__(
        self, broker, corr_guard=None, lookback: int = BETA_LOOKBACK_DAYS,
        shrink_factor: float = SHRINK, min_overlap: int = MIN_OVERLAP,
    ):
        self.broker = broker
        self.corr_guard = corr_guard
        self.lookback = int(lookback)
        self.shrink_factor = float(shrink_factor)
        self.min_overlap = int(min_overlap)
        self._returns: dict[str, dict[str, float] | None] = {}
        self._betas: dict[tuple[str, str], float | None] = {}

    def new_cycle(self) -> None:
        self._returns.clear()
        self._betas.clear()

    # -- series ---------------------------------------------------------- #
    def _daily_returns(self, symbol: str) -> dict[str, float] | None:
        symbol = symbol.upper()
        guard = self.corr_guard
        if guard is not None and hasattr(guard, "_daily_returns"):
            try:
                rets = guard._daily_returns(symbol)
                if rets:
                    return rets
            except Exception as e:  # noqa: BLE001 — fall through to own fetch
                log.debug("corr-guard series for %s failed: %s", symbol, e)
        if symbol in self._returns:
            return self._returns[symbol]
        rets: dict[str, float] | None = None
        try:
            pairs = self.broker.daily_close_series(symbol, self.lookback + 1)
            rets = daily_returns(pairs) or None
        except Exception as e:  # noqa: BLE001 — a measurement, never a blocker
            log.warning("beta history for %s failed: %s", symbol, e)
            rets = None
        self._returns[symbol] = rets
        return rets

    # -- betas ----------------------------------------------------------- #
    def beta_of(self, symbol: str, bench: str = "SPY") -> float | None:
        """Shrunk beta of `symbol` vs `bench`, cached for the cycle. None =
        unknown (short/absent history, flat benchmark)."""
        key = (symbol.upper(), bench.upper())
        if key in self._betas:
            return self._betas[key]
        if key[0] == key[1]:
            b: float | None = 1.0
        else:
            b = shrink(
                raw_beta(
                    self._daily_returns(key[0]), self._daily_returns(key[1]),
                    lookback=self.lookback, min_overlap=self.min_overlap,
                ),
                self.shrink_factor,
            )
        self._betas[key] = b
        return b

    # -- the book -------------------------------------------------------- #
    @staticmethod
    def book_weights(account) -> tuple[dict[str, float], list[str]]:
        """symbol -> market value / equity. Option legs are folded into
        their UNDERLYING at signed net market value (debit-notional proxy,
        see module docstring); returns the list of underlyings so folded
        for the reading's notes."""
        equity = float(getattr(account, "equity", 0.0) or 0.0)
        weights: dict[str, float] = {}
        folded: list[str] = []
        if equity <= 0:
            return weights, folded
        for p in getattr(account, "positions", []) or []:
            sym = str(p.symbol).upper()
            mv = float(p.market_value or 0.0)
            if getattr(p, "is_option", False):
                under = occ_underlying(sym) or sym[:-15]
                if under not in folded:
                    folded.append(under)
                weights[under] = weights.get(under, 0.0) + mv / equity
            else:
                weights[sym] = weights.get(sym, 0.0) + mv / equity
        return weights, folded

    def read(self, account) -> BookBetaReading:
        """Measure the book. Never raises."""
        reading = BookBetaReading()
        try:
            equity = float(getattr(account, "equity", 0.0) or 0.0)
            if equity <= 0:
                reading.reason = "equity <= 0"
                return reading
            weights, folded = self.book_weights(account)
            reading.weights = weights
            reading.invested_pct = 100.0 * sum(
                max(0.0, w) for w in weights.values()
            )
            if folded:
                reading.notes.append(
                    "options at debit notional x underlying beta: "
                    + ",".join(sorted(folded))
                )
            for bench in BENCHMARKS:
                if self._daily_returns(bench) is None:
                    reading.notes.append(f"{bench} series unavailable")
            for sym in weights:
                reading.betas[sym] = {
                    b: self.beta_of(sym, b) for b in BENCHMARKS
                }
                if reading.betas[sym]["SPY"] is None and sym not in reading.unknown:
                    reading.unknown.append(sym)
            per_bench: dict[str, float | None] = {}
            for bench in BENCHMARKS:
                if self._daily_returns(bench) is None and weights:
                    per_bench[bench] = None
                    continue
                per_bench[bench] = aggregate(
                    weights, {s: d[bench] for s, d in reading.betas.items()},
                )
            reading.spy = per_bench.get("SPY")
            reading.qqq = per_bench.get("QQQ")
            reading.iwm = per_bench.get("IWM")
            if reading.spy is None:
                reading.reason = "SPY series unavailable"
        except Exception as e:  # noqa: BLE001 — a measurement, never a blocker
            reading.reason = f"{type(e).__name__}: {e}"[:120]
        return reading
