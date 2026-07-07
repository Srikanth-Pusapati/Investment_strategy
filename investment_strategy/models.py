"""Shared data models that flow between the four stages.

Signals (per symbol)  ->  Claude  ->  TradeProposal  ->  RiskManager  ->
RiskDecision  ->  Alpaca order.  Positions/Account feed back as context.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


# 1-5 letters plus an optional class/ADR suffix (BRK.B, BF-B). Crypto pairs
# (BTC-USD) fail the 1-2 letter suffix rule by design.
_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")
# EDGAR Form-4 filings for unlisted issuers put a literal "N/A" in
# issuerTradingSymbol; other feeds render missing symbols as these strings.
_PLACEHOLDER_SYMS = {"N/A", "NA", "NONE", "NULL"}


def is_valid_ticker(sym: str) -> bool:
    """True for a plausible US-listed symbol; rejects feed placeholders."""
    return bool(sym) and sym not in _PLACEHOLDER_SYMS and bool(_TICKER_RE.match(sym))


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
class SignalKind(str, Enum):
    FUNDAMENTALS = "fundamentals"
    TECHNICAL = "technical"   # price-action indicators (RSI, MACD, trend)
    NEWS = "news"
    INSIDER = "insider"
    CONGRESS = "congress"
    OFFEXCHANGE = "offexchange"  # dark-pool / off-exchange short volume (~1d lag)
    GOVCONTRACTS = "govcontracts"  # federal contract awards (revenue catalyst; days lag)
    MACRO = "macro"
    DISCOVERY = "discovery"   # why a symbol was surfaced by the market scanner


class Signal(BaseModel):
    """One normalized observation about a symbol (or the market, for macro)."""
    kind: SignalKind
    symbol: Optional[str] = None          # None for market-wide macro signals
    summary: str                          # human/LLM-readable one-liner
    score: Optional[float] = None         # -1.0 (bearish) .. +1.0 (bullish)
    data: dict[str, Any] = Field(default_factory=dict)  # raw provider payload
    source: str = ""
    as_of: datetime = Field(default_factory=_now)


class SignalBundle(BaseModel):
    """All signals relevant to a single symbol, plus shared market context."""
    symbol: str
    signals: list[Signal] = Field(default_factory=list)
    market_context: list[Signal] = Field(default_factory=list)  # macro etc.


class Candidate(BaseModel):
    """A ticker surfaced by the market scanner BEFORE any per-symbol signals are
    gathered. The scanner discovers *which* names to evaluate; the signal layer
    then scores them and Claude decides. `score` is the combined smart-money lean
    (-1 bearish .. +1 bullish); `sources` lists which screeners flagged it."""
    symbol: str
    sources: list[str] = Field(default_factory=list)
    reason: str                          # human/LLM-readable "why surfaced"
    score: float = 0.0

    def to_signal(self) -> Signal:
        """Render as a DISCOVERY signal so the candidate's rationale rides through
        the normal bundle -> prompt -> ledger path, and so a discovered name with
        no other signals still carries at least one (and isn't dropped by gather)."""
        return Signal(
            kind=SignalKind.DISCOVERY,
            symbol=self.symbol,
            summary=self.reason,
            score=self.score,
            source="+".join(self.sources) or "scanner",
        )


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Instrument(str, Enum):
    """What kind of thing the proposal trades. Equity covers stocks AND ETFs
    (an ETF is just a symbol). OPTION engages the defined-risk options path."""
    EQUITY = "equity"
    OPTION = "option"


class OptionStrategy(str, Enum):
    """Only DEFINED-RISK strategies are supported — max loss is known up front.
    No naked selling. Bull/bear verticals cap both risk and reward."""
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL_SPREAD = "bull_call_spread"   # buy lower call, sell higher call
    BEAR_PUT_SPREAD = "bear_put_spread"     # buy higher put, sell lower put


class OptionLeg(BaseModel):
    """One leg of an options order. `right` is C/P, OCC-style symbol resolved
    at execution from underlying+expiry+strike."""
    expiry: str                # YYYY-MM-DD
    strike: float
    right: str                 # "call" | "put"
    side: Action               # BUY or SELL (the leg direction)
    ratio: int = 1             # contracts per unit of the strategy


class TradeProposal(BaseModel):
    """Claude's recommendation for a symbol. Sizing is a REQUEST, not a guarantee
    — RiskManager has final say and may shrink or veto it."""
    symbol: str                                 # underlying for options
    action: Action
    conviction: float = Field(ge=0.0, le=1.0)   # how strong the signal is
    target_weight_pct: float = Field(ge=0.0, le=100.0)  # desired % of equity
    stop_loss_pct: Optional[float] = None       # override default if set
    take_profit_pct: Optional[float] = None
    rationale: str                              # why — for the audit log
    key_signals: list[str] = Field(default_factory=list)

    # --- instrument / options (equity is the default) ---
    instrument: Instrument = Instrument.EQUITY
    option_strategy: Optional[OptionStrategy] = None
    option_legs: list[OptionLeg] = Field(default_factory=list)
    max_premium_usd: Optional[float] = None     # cap on debit paid for the play


class RiskVerdict(str, Enum):
    APPROVED = "approved"
    RESIZED = "resized"      # approved but quantity reduced to fit limits
    REJECTED = "rejected"


class RiskDecision(BaseModel):
    """RiskManager's final ruling on a proposal — what (if anything) executes."""
    proposal: TradeProposal
    verdict: RiskVerdict
    approved_qty: float = 0.0          # shares to actually trade (0 if rejected)
    approved_notional: float = 0.0     # dollar value
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    reason: str = ""                   # why resized/rejected — for audit log
    decided_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Account / positions (normalized from Alpaca)
# --------------------------------------------------------------------------- #
class Position(BaseModel):
    symbol: str
    qty: float
    qty_available: float = 0.0  # shares not locked in open orders
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    unrealized_pl_pct: float


class AccountSnapshot(BaseModel):
    equity: float
    last_equity: float          # equity at previous close — for day P/L
    cash: float
    buying_power: float
    positions: list[Position] = Field(default_factory=list)
    # Pattern-Day-Trader status (margin accounts only; a cash account stays
    # False/0 and is exempt). Feeds the risk PDT guard for small accounts.
    pattern_day_trader: bool = False   # already flagged as a PDT
    daytrade_count: int = 0            # day trades in the trailing 5 business days
    as_of: datetime = Field(default_factory=_now)

    @property
    def day_pl(self) -> float:
        return self.equity - self.last_equity

    @property
    def day_pl_pct(self) -> float:
        return (self.day_pl / self.last_equity * 100.0) if self.last_equity else 0.0

    def position_for(self, symbol: str) -> Optional[Position]:
        return next((p for p in self.positions if p.symbol == symbol), None)


# --------------------------------------------------------------------------- #
# Orders — generalized request the execution layer knows how to place
# --------------------------------------------------------------------------- #
class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TIF(str, Enum):
    DAY = "day"
    GTC = "gtc"          # good-till-canceled
    IOC = "ioc"


class OrderRequest(BaseModel):
    """One concrete order for the broker. Either `qty` (whole or fractional) or
    `notional` (dollar amount, enables fractional) — not both. Bracket levels are
    optional and only valid for whole-share equity orders on Alpaca."""
    symbol: str
    side: Action
    order_type: OrderType = OrderType.MARKET
    tif: TIF = TIF.DAY
    qty: Optional[float] = None
    notional: Optional[float] = None        # dollar-based (fractional) order
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    # bracket exits (whole-share equity only)
    take_profit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None

    @property
    def is_fractional(self) -> bool:
        return self.notional is not None or (
            self.qty is not None and self.qty != int(self.qty)
        )


# --------------------------------------------------------------------------- #
# Benchmark / performance vs SPY/QQQ
# --------------------------------------------------------------------------- #
class BenchmarkStats(BaseModel):
    """Account performance RELATIVE to a benchmark over a lookback window. The
    point of the bot is excess return, not raw return — this is what we feed
    Claude and what we judge ourselves on."""
    benchmark: str                       # e.g. "QQQ"
    period_days: int
    account_return_pct: float
    benchmark_return_pct: float
    information_ratio: Optional[float] = None  # excess return / tracking error

    @property
    def excess_return_pct(self) -> float:
        return self.account_return_pct - self.benchmark_return_pct


# --------------------------------------------------------------------------- #
# External (read-only) holdings — e.g. imported from Robinhood for context
# --------------------------------------------------------------------------- #
class ExternalHolding(BaseModel):
    source: str                          # "robinhood"
    symbol: str
    qty: float
    market_value: float
    unrealized_pl_pct: Optional[float] = None
