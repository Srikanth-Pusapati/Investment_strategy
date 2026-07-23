"""D.1 glue: real price history + entry streams for the backtest harness.

Everything here is pure date->index bookkeeping except fetch_price_history, which
delegates the network fetch to the broker's daily_close_series. Alignment is an
inner join on ISO dates, so every series in the returned dict has the SAME length
— the shared trading calendar the BacktestEngine indexes into.

Two entry sources, honest about what each proves:
  - entries_from_ledger: replay the REAL buys the live loop made (exact conviction/
    stop/take Claude proposed). Validates sizing+exits on what actually fired, but
    a young ledger is a tiny sample — treat the numbers as a smoke test, not proof.
  - breakout_entries: a deliberately simple rule (fresh N-day closing high) to
    generate enough entries over a year of history that a knob sweep has some
    statistical weight. It tunes SIZING/EXITS, not the live signal stack.
"""
from __future__ import annotations

import json
import logging
import statistics
from bisect import bisect_left
from pathlib import Path

from .backtest import EntrySignal

log = logging.getLogger("backtest.data")

_TRADING_DAYS = 252


def align_price_history(
    series: dict[str, list[tuple[str, float]]],
) -> tuple[list[str], dict[str, list[float]]]:
    """Inner-join dated close series on their common ISO dates.
    Returns (dates, prices) where every prices[sym] has len(dates)."""
    common: set[str] | None = None
    for pairs in series.values():
        ds = {d for d, _ in pairs}
        common = ds if common is None else common & ds
    if not common:
        return [], {}
    dates = sorted(common)
    idx = {d: i for i, d in enumerate(dates)}
    prices: dict[str, list[float]] = {}
    for sym, pairs in series.items():
        row = [0.0] * len(dates)
        for d, close in pairs:
            i = idx.get(d)
            if i is not None:
                row[i] = close
        prices[sym] = row
    return dates, prices


def fetch_price_history(
    broker, symbols: list[str], days: int,
) -> tuple[list[str], dict[str, list[float]]]:
    """Pull `days` of daily closes per symbol via broker.daily_close_series and
    align them. Symbols with <60% of the requested bars are dropped (a recent IPO
    or bad ticker would otherwise shrink EVERY series via the inner join)."""
    series: dict[str, list[tuple[str, float]]] = {}
    for sym in symbols:
        pairs = broker.daily_close_series(sym, days)
        if len(pairs) >= max(2, int(days * 0.6)):
            series[sym] = pairs
        else:
            log.warning(
                "Dropping %s from backtest: only %d/%d bars.", sym, len(pairs), days
            )
    return align_price_history(series)


def annualized_vol_at(
    closes: list[float], day: int, lookback: int = 30,
) -> float | None:
    """Trailing annualized vol at bar `day`, so backtested sizing sees the same
    vol input the live RiskManager gets. None if history is too short."""
    window = [c for c in closes[max(0, day - lookback): day + 1] if c > 0]
    if len(window) < 10:
        return None
    rets = [window[i] / window[i - 1] - 1.0 for i in range(1, len(window))]
    try:
        sd = statistics.stdev(rets)
    except statistics.StatisticsError:
        return None
    return sd * (_TRADING_DAYS ** 0.5)


def tech_at(closes: list[float], day: int) -> dict | None:
    """Technical context at bar `day` for the anti-chasing gate, computed from
    the same close series the replay runs on. ATR degrades to the close-to-
    close true range (no H/L bars offline) — a slightly tighter yardstick, so
    the gate fires a touch EARLIER in replay than live; document, don't hide.
    None when history is too short (the gate then fails open, exactly as live)."""
    from .signals.technical import TechnicalProvider

    window = [c for c in closes[max(0, day - 260): day + 1] if c > 0]
    if len(window) < 35:
        return None
    rsi = TechnicalProvider._rsi(window, 14)
    sma20 = TechnicalProvider._sma(window, 20)
    atr = TechnicalProvider._atr([], [], window, 14)
    price = window[-1]
    ext_pct = ((price / sma20 - 1.0) * 100.0) if sma20 else None
    ext_atr = ((price - sma20) / atr) if (sma20 and atr) else None
    return {
        "rsi14": round(rsi, 1),
        "ext_pct_sma20": round(ext_pct, 2) if ext_pct is not None else None,
        "ext_atr": round(ext_atr, 2) if ext_atr is not None else None,
    }


def entries_from_ledger(
    ledger_path: str | Path, dates: list[str],
    prices: dict[str, list[float]] | None = None,
) -> list[EntrySignal]:
    """Map the ledger's BUY records onto the aligned calendar: each buy fires on
    the first trading date >= its timestamp, carrying the conviction/stop/take
    Claude actually proposed. Buys outside the calendar or for symbols without
    price history are skipped (logged)."""
    path = Path(ledger_path)
    if not path.exists() or not dates:
        return []
    entries: list[EntrySignal] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("action") != "buy" or rec.get("instrument") == "option":
            continue
        sym = rec.get("symbol", "")
        ts = str(rec.get("ts", ""))[:10]  # ISO date prefix
        if not sym or not ts:
            continue
        day = bisect_left(dates, ts)
        if day >= len(dates):
            log.info("Ledger buy %s @ %s is after the price window; skipped.", sym, ts)
            continue
        if prices is not None and sym not in prices:
            log.info("Ledger buy %s has no price history; skipped.", sym)
            continue
        vol = (
            annualized_vol_at(prices[sym], day) if prices is not None else None
        )
        entries.append(EntrySignal(
            day=day, symbol=sym,
            conviction=float(rec.get("conviction") or 0.6),
            target_weight_pct=float(rec.get("target_weight_pct") or 10.0),
            stop_loss_pct=rec.get("stop_loss_pct"),
            take_profit_pct=rec.get("take_profit_pct"),
            volatility=vol,
            tech=tech_at(prices[sym], day) if prices is not None else None,
        ))
    return entries


def crash_overlay(
    prices: dict[str, list[float]], start: int, daily_pct: float = 4.0,
    crash_days: int = 80,
    gap_days: tuple[int, ...] = (1, 2, 3, 5, 8, 12, 18), gap_pct: float = 15.0,
) -> dict[str, list[float]]:
    """Deterministic market-wide crash grafted onto real closes (D.3): from bar
    `start`, every series glides down `daily_pct`/day for `crash_days` bars, with
    extra `gap_pct` gap-downs on the given crash-relative days (so stops gap
    through instead of filling politely), then stays at the crushed level. Keeps
    the real day-to-day texture — it's a scale factor, not synthetic prices. This
    is a BRAKE test path, not a return forecast.

    Defaults model a 1987/2008/COVID-scale collapse: a ~-50% slide with limit-down
    gaps FRONT-LOADED into the first weeks. That front-loading is deliberate and
    load-bearing — the account-level brakes (daily-loss flatten, equity-floor
    latch) only exist for a crash the PER-POSITION stops can't front-run. With the
    stops now tight (vol clamp ~[4,10]%), a gentle grind lets every cohort stop out
    to cash within a bar or two, so the account brakes never engage and the D.3
    gate proves nothing. The gaps must land while the book is still FULL and be
    large enough to gap THROUGH the stops, or this test is vacuous. If you tighten
    the stops further, re-verify these defaults still drive the brakes (see
    tests/test_backtest_data.py::test_default_crash_still_engages_account_brakes)."""
    out: dict[str, list[float]] = {}
    for sym, closes in prices.items():
        row = list(closes)
        mult = 1.0
        for d in range(start, len(row)):
            k = d - start
            if k < crash_days:
                mult *= 1.0 - daily_pct / 100.0
                if k in gap_days:
                    mult *= 1.0 - gap_pct / 100.0
            row[d] = closes[d] * mult
        out[sym] = row
    return out


def stubborn_entries(
    dates: list[str], prices: dict[str, list[float]],
    every: int = 5, benchmark: str = "",
) -> list[EntrySignal]:
    """Worst-case entry stream for the D.3 brake test: every symbol re-fires a
    buy every `every` bars for the WHOLE window, regardless of trend — a strategy
    that keeps buying straight into a crash. The point is that the ACCOUNT-LEVEL
    guards (daily-loss halt, drawdown halt, equity floor) must be what stops the
    bleeding, not polite entry logic."""
    entries: list[EntrySignal] = []
    for sym, closes in prices.items():
        if sym == benchmark:
            continue
        for day in range(0, len(dates), every):
            if closes[day] > 0:
                entries.append(EntrySignal(
                    day=day, symbol=sym, conviction=0.7, target_weight_pct=12.0,
                    volatility=annualized_vol_at(closes, day),
                ))
    entries.sort(key=lambda e: e.day)
    return entries


def breakout_entries(
    dates: list[str], prices: dict[str, list[float]],
    lookback: int = 20, cooldown: int = 10, benchmark: str = "",
) -> list[EntrySignal]:
    """Rule-based entry stream for knob sweeps: buy a symbol when it closes at a
    fresh `lookback`-day high, then re-arm only after `cooldown` bars (so one
    trend doesn't fire daily). The benchmark symbol is excluded — it's the thing
    being beaten, not a pick. Deliberately naive: this exists to exercise the
    SIZING + EXIT knobs over many entries, not to be the live signal."""
    entries: list[EntrySignal] = []
    for sym, closes in prices.items():
        if sym == benchmark:
            continue
        last_fire = -cooldown
        for day in range(lookback, len(dates)):
            window = closes[day - lookback: day]
            price = closes[day]
            if price <= 0 or not window or max(window) <= 0:
                continue
            if price > max(window) and day - last_fire >= cooldown:
                entries.append(EntrySignal(
                    day=day, symbol=sym,
                    conviction=0.6, target_weight_pct=10.0,
                    volatility=annualized_vol_at(closes, day),
                    tech=tech_at(closes, day),
                ))
                last_fire = day
    entries.sort(key=lambda e: e.day)
    return entries
