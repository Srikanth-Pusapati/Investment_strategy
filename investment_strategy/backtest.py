"""Backtest harness (2.1) — the GATE before risking real money or buying data.

Replays a stream of entry signals through the REAL RiskManager sizing gate and a
deterministic position lifecycle (stop / take-profit / scale-out / trailing stop /
time-stop — the same exits the live watchdog enforces) over historical daily
closes, and reports the edge. That lets you tune KELLY_FRACTION,
TARGET_ANNUAL_VOL_PCT, and the stop/take levels against history BEFORE spending
money on execution or data.

Design choices (honest about what this does and doesn't model):
  - Sizing is the LIVE RiskManager, so the caps/vol-targeting/per-trade-risk you
    run in production are exactly what's tuned here — not a reimplementation.
  - The signal->Claude step is NOT replayed: historical smart-money signals aren't
    available offline and the LLM is non-deterministic/costly. Instead you feed
    EntrySignals (conviction + weight + stop/take), sourced from a simple rule, a
    sweep, or the real ledger. The harness measures the SIZING + EXIT edge, which
    is the part a backtest can honestly evaluate.
  - Daily closes only: an intrabar stop fills at that day's CLOSE (models
    gap-through / slippage conservatively), not the exact stop price. Feeding OHLC
    for precise stop fills is a future refinement.
  - `max_hold_days` is measured in TRADING days here (one bar = one step).

No network, no keys — prices are injected, so it is fully deterministic + testable.
"""
from __future__ import annotations

import logging
import os
import statistics
import tempfile
import uuid
from dataclasses import dataclass

from .config import RiskLimits
from .models import (
    AccountSnapshot,
    Action,
    Position,
    RiskVerdict,
    TradeProposal,
)
from .risk import RiskManager
from .state import PortfolioState

log = logging.getLogger("backtest")

_TRADING_DAYS = 252
_TRAIL_GIVEBACK_PCT = 3.0    # mirrors Watchdog.trail_giveback_pct


@dataclass
class EntrySignal:
    """A buy idea to replay: `day` is the index into the aligned price history at
    which it fires. conviction/target_weight/stops mirror a TradeProposal — i.e.
    what Claude would have proposed — so the RiskManager sizes it exactly as live."""
    day: int
    symbol: str
    conviction: float = 0.6
    target_weight_pct: float = 10.0
    stop_loss_pct: float | None = None      # None -> RiskManager default
    take_profit_pct: float | None = None
    volatility: float | None = None         # annualized; None -> risk assumes high vol


@dataclass
class _OpenPosition:
    symbol: str
    qty: float
    entry_price: float          # slippage-adjusted cost basis
    stop_pct: float
    take_pct: float
    entry_day: int
    peak_pl_pct: float = 0.0
    scaled: bool = False


@dataclass
class ClosedTrade:
    symbol: str
    entry_day: int
    exit_day: int
    entry_price: float
    exit_price: float
    qty: float
    pl_usd: float
    pl_pct: float
    reason: str                 # stop | take | scale | trail | time | end


@dataclass
class BacktestResult:
    initial_equity: float
    final_equity: float
    equity_curve: list[float]
    trades: list[ClosedTrade]
    benchmark_return_pct: float | None = None
    _cfg_note: str = ""

    # -- headline metrics --------------------------------------------------- #
    @property
    def total_return_pct(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return (self.final_equity / self.initial_equity - 1.0) * 100.0

    @property
    def max_drawdown_pct(self) -> float:
        peak = self.equity_curve[0] if self.equity_curve else 0.0
        worst = 0.0
        for e in self.equity_curve:
            peak = max(peak, e)
            if peak > 0:
                worst = max(worst, (peak - e) / peak * 100.0)
        return worst

    @property
    def win_rate_pct(self) -> float:
        closed = [t for t in self.trades if t.reason != "end"]
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if t.pl_usd > 0)
        return wins / len(closed) * 100.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pl_usd for t in self.trades if t.pl_usd > 0)
        losses = -sum(t.pl_usd for t in self.trades if t.pl_usd < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def sharpe(self) -> float:
        """Annualized Sharpe of the daily equity curve (risk-free = 0)."""
        rets = [
            self.equity_curve[i] / self.equity_curve[i - 1] - 1.0
            for i in range(1, len(self.equity_curve))
            if self.equity_curve[i - 1] > 0
        ]
        if len(rets) < 2:
            return 0.0
        sd = statistics.pstdev(rets)
        if sd == 0:
            return 0.0
        return statistics.fmean(rets) / sd * (_TRADING_DAYS ** 0.5)

    @property
    def excess_return_pct(self) -> float | None:
        if self.benchmark_return_pct is None:
            return None
        return self.total_return_pct - self.benchmark_return_pct

    def summary(self) -> str:
        lines = [
            "== Backtest result ==",
            self._cfg_note,
            f"Return:        {self.total_return_pct:+.2f}%  "
            f"(final ${self.final_equity:,.0f} from ${self.initial_equity:,.0f})",
            f"Max drawdown:  {self.max_drawdown_pct:.2f}%",
            f"Sharpe (ann.): {self.sharpe:.2f}",
            f"Trades:        {len([t for t in self.trades if t.reason != 'end'])} "
            f"closed | win rate {self.win_rate_pct:.0f}% | "
            f"profit factor {self.profit_factor:.2f}",
        ]
        if self.excess_return_pct is not None:
            lines.append(
                f"Vs benchmark:  {self.excess_return_pct:+.2f}% excess "
                f"(benchmark {self.benchmark_return_pct:+.2f}%)"
            )
        by_reason: dict[str, int] = {}
        for t in self.trades:
            by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
        if by_reason:
            lines.append("Exits:         " + ", ".join(
                f"{k}={v}" for k, v in sorted(by_reason.items())))
        return "\n".join(x for x in lines if x)


class BacktestEngine:
    """Deterministic replay of sizing + exits over aligned daily closes.

    `prices` maps symbol -> list of daily closes, all the SAME length (the aligned
    trading calendar). `entries` fire on their `day` index. Sizing goes through the
    real RiskManager so the same caps/vol-targeting apply; exits mirror the live
    watchdog. `benchmark` (optional) is a same-length close series to beat.
    """

    def __init__(
        self, limits: RiskLimits, prices: dict[str, list[float]],
        initial_equity: float = 100_000.0, slippage_pct: float | None = None,
        benchmark: list[float] | None = None, scale_out: bool | None = None,
    ):
        self.limits = limits
        self.prices = prices
        self.initial_equity = initial_equity
        # Round-trip friction: default to the configured one-way slippage estimate.
        self.slippage_pct = (
            limits.est_slippage_pct if slippage_pct is None else slippage_pct
        )
        self.benchmark = benchmark
        self.scale_out = limits.scale_out_enabled if scale_out is None else scale_out
        self._n = min((len(v) for v in prices.values()), default=0)
        # Fresh, UNIQUE throwaway risk state per engine so the drawdown/halt latch
        # engages exactly as live (peak-to-trough halts are part of the strategy
        # being tested) without sharing stale peak equity across runs or ever
        # touching the live state/risk_state.json.
        state_path = os.path.join(
            tempfile.gettempdir(), f"backtest_{uuid.uuid4().hex}.json"
        )
        self.risk = RiskManager(limits, state=PortfolioState(path=state_path))

    def run(self, entries: list[EntrySignal]) -> BacktestResult:
        by_day: dict[int, list[EntrySignal]] = {}
        for e in entries:
            by_day.setdefault(e.day, []).append(e)

        cash = self.initial_equity
        open_pos: dict[str, _OpenPosition] = {}
        curve: list[float] = []
        trades: list[ClosedTrade] = []

        for day in range(self._n):
            # 1) Mark to market + run exits on the existing book.
            for sym in list(open_pos.keys()):
                pos = open_pos[sym]
                price = self.prices[sym][day]
                proceeds, closed = self._apply_exits(pos, price, day, trades)
                if proceeds:
                    cash += proceeds
                if closed:
                    del open_pos[sym]

            equity = cash + sum(
                p.qty * self.prices[p.symbol][day] for p in open_pos.values()
            )
            self.risk.state.update_equity(equity)

            # 2) Fire the day's entry signals through the REAL sizing gate.
            prev_equity = curve[-1] if curve else self.initial_equity
            for sig in by_day.get(day, []):
                if sig.symbol not in self.prices:
                    continue
                price = self.prices[sig.symbol][day]
                if price <= 0 or sig.symbol in open_pos:
                    continue
                account = self._snapshot(cash, open_pos, day, equity, prev_equity)
                decision = self.risk.evaluate(
                    self._proposal(sig), account, price, sig.volatility,
                )
                if decision.verdict == RiskVerdict.REJECTED or decision.approved_qty <= 0:
                    continue
                fill = price * (1 + self.slippage_pct / 100.0)  # pay up on entry
                spend = decision.approved_qty * fill
                if spend > cash:
                    continue
                cash -= spend
                open_pos[sig.symbol] = _OpenPosition(
                    symbol=sig.symbol, qty=decision.approved_qty, entry_price=fill,
                    stop_pct=decision.stop_loss_pct, take_pct=decision.take_profit_pct,
                    entry_day=day,
                )

            equity = cash + sum(
                p.qty * self.prices[p.symbol][day] for p in open_pos.values()
            )
            curve.append(equity)

        # Liquidate whatever's left at the last close so the equity is realized.
        last = self._n - 1
        for sym, pos in open_pos.items():
            price = self.prices[sym][last]
            exit_px = price * (1 - self.slippage_pct / 100.0)
            cash += pos.qty * exit_px
            trades.append(self._closed(pos, exit_px, last, "end"))
        final_equity = cash if self._n else self.initial_equity
        if curve:
            curve[-1] = final_equity

        return BacktestResult(
            initial_equity=self.initial_equity, final_equity=final_equity,
            equity_curve=curve, trades=trades,
            benchmark_return_pct=self._benchmark_return(),
            _cfg_note=(
                f"kelly={self.limits.kelly_fraction:g} "
                f"vol_target={self.limits.target_annual_vol_pct:g}% "
                f"stop={self.limits.default_stop_loss_pct:g}% "
                f"take={self.limits.default_take_profit_pct:g}% "
                f"scale_out={'on' if self.scale_out else 'off'} "
                f"slippage={self.slippage_pct:g}%"
            ),
        )

    # -- exits (mirror the live watchdog ordering) -------------------------- #
    def _apply_exits(
        self, pos: _OpenPosition, price: float, day: int, trades: list[ClosedTrade],
    ) -> tuple[float, bool]:
        """Returns (cash_proceeds, fully_closed). Order: stop, take/scale-out,
        time-stop, trailing — the same precedence the watchdog uses."""
        pl_pct = (price / pos.entry_price - 1.0) * 100.0
        pos.peak_pl_pct = max(pos.peak_pl_pct, pl_pct)
        exit_px = price * (1 - self.slippage_pct / 100.0)

        # Stop-loss: always a FULL exit.
        if pos.stop_pct > 0 and pl_pct <= -pos.stop_pct:
            trades.append(self._closed(pos, exit_px, day, "stop"))
            return pos.qty * exit_px, True

        # Take-profit: scale out a slice and trail the rest, or full close.
        if pos.take_pct > 0 and pl_pct >= pos.take_pct:
            if self.scale_out and not pos.scaled:
                frac = self.limits.scale_out_pct / 100.0
                sell_qty = pos.qty * frac
                if sell_qty > 0:
                    proceeds = sell_qty * exit_px
                    trades.append(self._closed(pos, exit_px, day, "scale", qty=sell_qty))
                    pos.qty -= sell_qty
                    pos.take_pct = 0.0      # let the remainder ride the trailing stop
                    pos.scaled = True
                    return proceeds, False
            trades.append(self._closed(pos, exit_px, day, "take"))
            return pos.qty * exit_px, True

        # Time-stop: recycle dead/flat capital.
        age = day - pos.entry_day
        if (
            self.limits.max_hold_days > 0
            and age >= self.limits.max_hold_days
            and pl_pct < self.limits.time_stop_min_gain_pct
        ):
            trades.append(self._closed(pos, exit_px, day, "time"))
            return pos.qty * exit_px, True

        # Trailing stop: give back at most _TRAIL_GIVEBACK_PCT of the peak gain.
        if pos.peak_pl_pct > _TRAIL_GIVEBACK_PCT and pl_pct <= pos.peak_pl_pct - _TRAIL_GIVEBACK_PCT:
            trades.append(self._closed(pos, exit_px, day, "trail"))
            return pos.qty * exit_px, True

        return 0.0, False

    # -- helpers ------------------------------------------------------------ #
    def _closed(
        self, pos: _OpenPosition, exit_px: float, day: int, reason: str,
        qty: float | None = None,
    ) -> ClosedTrade:
        q = pos.qty if qty is None else qty
        pl_usd = (exit_px - pos.entry_price) * q
        pl_pct = (exit_px / pos.entry_price - 1.0) * 100.0
        return ClosedTrade(
            symbol=pos.symbol, entry_day=pos.entry_day, exit_day=day,
            entry_price=pos.entry_price, exit_price=exit_px, qty=q,
            pl_usd=pl_usd, pl_pct=pl_pct, reason=reason,
        )

    def _proposal(self, sig: EntrySignal) -> TradeProposal:
        return TradeProposal(
            symbol=sig.symbol, action=Action.BUY, conviction=sig.conviction,
            target_weight_pct=sig.target_weight_pct, stop_loss_pct=sig.stop_loss_pct,
            take_profit_pct=sig.take_profit_pct, rationale="backtest",
        )

    def _snapshot(
        self, cash: float, open_pos: dict[str, _OpenPosition], day: int,
        equity: float, prev_equity: float,
    ) -> AccountSnapshot:
        positions = [
            Position(
                symbol=p.symbol, qty=p.qty, avg_entry_price=p.entry_price,
                current_price=self.prices[p.symbol][day],
                market_value=p.qty * self.prices[p.symbol][day],
                unrealized_pl=(self.prices[p.symbol][day] - p.entry_price) * p.qty,
                unrealized_pl_pct=(self.prices[p.symbol][day] / p.entry_price - 1.0) * 100.0,
            )
            for p in open_pos.values()
        ]
        # No leverage in the backtest: buying power == cash.
        return AccountSnapshot(
            equity=equity, last_equity=prev_equity, cash=cash,
            buying_power=cash, positions=positions,
        )

    def _benchmark_return(self) -> float | None:
        if not self.benchmark or len(self.benchmark) < 2 or self.benchmark[0] <= 0:
            return None
        return (self.benchmark[-1] / self.benchmark[0] - 1.0) * 100.0


def run_backtest(
    limits: RiskLimits, prices: dict[str, list[float]], entries: list[EntrySignal],
    initial_equity: float = 100_000.0, benchmark: list[float] | None = None,
) -> BacktestResult:
    """Convenience wrapper: build the engine and run it. Returns a BacktestResult
    whose .summary() prints the headline edge metrics."""
    engine = BacktestEngine(
        limits, prices, initial_equity=initial_equity, benchmark=benchmark,
    )
    return engine.run(entries)


def _demo() -> None:
    """`python -m investment_strategy.backtest` — a deterministic smoke run on a
    synthetic trend so you can see the harness + summary end to end. Swap in real
    daily closes (e.g. from AlpacaClient._daily_closes) and ledger-sourced entries
    to backtest the live config."""
    import logging as _logging
    _logging.basicConfig(level="INFO", format="%(message)s")

    from .config import load_config

    # A gently trending name with a mid-run pullback + a laggard, 60 sessions.
    up = [100.0 * (1.004 ** i) for i in range(60)]
    up = [p * (0.92 if 20 <= i < 25 else 1.0) for i, p in enumerate(up)]  # a dip
    flat = [50.0 + (i % 3) for i in range(60)]                            # chops sideways
    prices = {"TRND": up, "CHOP": flat}
    bench = [100.0 * (1.002 ** i) for i in range(60)]                     # +~12.7%
    entries = [
        EntrySignal(day=0, symbol="TRND", conviction=0.7, target_weight_pct=10.0),
        EntrySignal(day=2, symbol="CHOP", conviction=0.5, target_weight_pct=10.0),
        EntrySignal(day=30, symbol="TRND", conviction=0.8, target_weight_pct=10.0),
    ]
    try:
        limits = load_config().risk         # backtest the SAME knobs you run live
    except Exception:                       # no .env / keys -> harmless defaults
        limits = _demo_limits()
    result = run_backtest(limits, prices, entries, benchmark=bench)
    print(result.summary())


def _demo_limits() -> RiskLimits:
    return RiskLimits(
        max_position_pct=10.0, max_symbol_exposure_pct=100.0,
        max_gross_exposure_pct=100.0, max_sector_exposure_pct=100.0,
        regime_filter_enabled=False, regime_degraded_mult=0.5,
        regime_trim_enabled=False, regime_trim_pct=25.0,
        max_daily_loss_pct=100.0, max_drawdown_pct=100.0, equity_floor_pct=0.0,
        max_open_positions=15, min_cash_buffer_pct=0.0, min_trade_price_usd=1.0,
        earnings_blackout_days=0, max_hold_days=30.0, time_stop_min_gain_pct=2.0,
        thesis_decay_enabled=False, thesis_decay_min_age_days=3.0, thesis_min_score=0.1,
        pdt_guard_enabled=False, max_day_trades_under_25k=3, min_conviction=0.0,
        max_trade_risk_pct=1.0, est_slippage_pct=0.1, min_edge_ratio=2.0,
        fractional_enabled=True, min_order_usd=1.0,
        default_stop_loss_pct=5.0, default_take_profit_pct=12.0,
        scale_out_enabled=True, scale_out_pct=50.0,
        kelly_fraction=0.5, target_annual_vol_pct=25.0,
        options_enabled=False, max_option_premium_pct=1.0,
    )


if __name__ == "__main__":
    _demo()
