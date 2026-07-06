"""Central configuration. Reads .env once and exposes a typed, validated Config.

This module is the single source of truth for the paper/live switch, the kill
switch, and every hard risk limit. Nothing else should read os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from dotenv import load_dotenv

from .notify import AlertConfig, load_alert_config

load_dotenv()  # populate os.environ from .env if present


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _flag(name: str, default: str = "off") -> bool:
    """Env flag -> bool. Accepts on/true/1/yes (case-insensitive)."""
    return os.getenv(name, default).strip().lower() in {"on", "true", "1", "yes"}


@dataclass(frozen=True)
class RiskLimits:
    """Hard caps enforced by RiskManager. The LLM cannot exceed these."""
    max_position_pct: float          # max % equity in a single NEW position
    max_symbol_exposure_pct: float   # max total % equity per symbol
    max_gross_exposure_pct: float    # max total deployed across ALL names (<=100 = no leverage)
    max_sector_exposure_pct: float   # max total % equity in one sector (concentration cap)
    regime_filter_enabled: bool      # scale position size by market regime (SPY/200dma + VIX)
    regime_degraded_mult: float      # size multiplier when regime data (yfinance) is down (<1 = size down)
    regime_trim_enabled: bool        # on a flip INTO risk-off, trim the existing book (1B.6)
    regime_trim_pct: float           # % of each position to sell on entering risk-off
    max_daily_loss_pct: float        # halt new trades past this day loss
    max_drawdown_pct: float          # halt new buys past this PEAK-to-trough DD
    equity_floor_pct: float          # liquidate + latch halt below this % of PEAK equity (0=off)
    max_open_positions: int          # cap concurrent holdings
    min_cash_buffer_pct: float       # never deploy below this cash reserve
    min_trade_price_usd: float       # refuse buys below this price (liquidity guard)
    earnings_blackout_days: int      # block NEW buys within this many days of earnings (0=off)
    # Deterministic time-stop: recycle dead/flat capital rather than hold it
    # forever. After max_hold_days a position that has NOT gained at least
    # time_stop_min_gain_pct is closed by the watchdog (LLM-independent).
    max_hold_days: float             # max calendar days to hold a flat position (0=off)
    time_stop_min_gain_pct: float    # below this unrealized gain at max age = dead money -> recycle
    # Thesis-decay exit (1B.4b): deterministically SELL a held name whose entry
    # signals are no longer corroborated by fresh bullish data — independent of the
    # LLM being up. Off by default (a data outage can transiently blank signals).
    thesis_decay_enabled: bool
    thesis_decay_min_age_days: float # grace period before a held name can decay-exit
    thesis_min_score: float          # a signal at/above this score still corroborates the thesis
    # Pattern-Day-Trader guard for small MARGIN accounts (<$25k). Cash accounts are
    # exempt and stay inert. Blocks NEW opening buys near/over the PDT line so an
    # incidental same-day stop can't get the account flagged + restricted.
    pdt_guard_enabled: bool
    max_day_trades_under_25k: int    # pause new buys once day-trades in 5d hit this
    # --- small-account survival: per-trade $-risk cap + cost/slippage floor ---
    min_conviction: float            # reject buys below this Claude conviction (0..1; 0=off)
    max_trade_risk_pct: float        # cap $ at risk (notional*stop%) per trade as % equity (0=off)
    est_slippage_pct: float          # one-way spread+slippage estimate, % of notional (0=off)
    min_edge_ratio: float            # take-profit must beat round-trip cost by this multiple
    # --- fractional shares (for small accounts) ---
    fractional_enabled: bool         # allow sub-share notional buys (no exchange bracket)
    min_order_usd: float             # smallest $ order worth placing (Alpaca min is $1)
    default_stop_loss_pct: float     # bracket stop distance
    default_take_profit_pct: float   # bracket take-profit distance
    # Scale-out (1B.8): at the take-profit target, sell only part of a watchdog-
    # managed (fractional) position and let the rest ride the trailing stop, so a
    # runner isn't capped at the first target. Whole-share positions still take the
    # full exchange-bracket profit (their take rests at the exchange).
    scale_out_enabled: bool
    scale_out_pct: float             # % of the position to sell at the first target
    # --- survival-first sizing (vol-targeted, fractional-Kelly style) ---
    kelly_fraction: float            # fraction of full Kelly (0..1); 0 disables
    target_annual_vol_pct: float     # per-position volatility budget
    # --- options (defined-risk only) ---
    options_enabled: bool            # master gate for the options path
    max_option_premium_pct: float    # max % equity as debit on one options play
    # --- R.1 vol-scaled ("ATR-style") dynamic stops ---
    # One fixed stop % is too tight for volatile names (chopped out by normal
    # noise — the exact failure D.1 measured on the old 5% stop) and too loose
    # for quiet ones. When enabled, the stop scales to the name's realized
    # daily sigma (the same vol input sizing already uses — no extra fetch) and
    # the take is a fixed reward:risk multiple of it; both deterministic,
    # OVERRIDING the LLM's proposed levels, and clamped to [min, max]. The
    # per-trade $-risk cap (2d) then shrinks SIZE as the stop widens, keeping
    # dollar risk ~constant per position. Defaulted (not required) fields so
    # existing RiskLimits(...) call sites keep working.
    vol_stops_enabled: bool = False  # scale stop/take to each name's realized vol
    vol_stop_mult: float = 2.0       # stop = mult x daily sigma (in %); 2.0 won
                                     # the --sweep-stops evidence at BOTH lookbacks
    vol_stop_take_ratio: float = 2.5 # take = ratio x stop (reward:risk)
    vol_stop_min_pct: float = 4.0    # clamp: never tighter than this stop
    vol_stop_max_pct: float = 15.0   # clamp: never wider than this stop
    # Trailing-stop giveback: % of the peak gain surrendered before the
    # watchdog (and the backtest's mirror of it) closes a runner. Previously a
    # hardcoded 3.0 in both places.
    trail_giveback_pct: float = 3.0
    # --- R.2 pairwise-correlation guard ---
    # The sector cap's finer-grained sibling: two "different" names whose daily
    # returns move together are ONE bet. Reject a NEW buy whose return
    # correlation with any already-held satellite (core ETF excluded) is
    # at/above this. Fail-open when price history is unavailable. 0 = off.
    max_pairwise_corr: float = 0.85
    # --- GA-2.3 whole-shares mode (closes the stop-less-position hole) ---
    # A fractional (notional) buy cannot carry an exchange bracket, so its ONLY
    # stop is the 30s watchdog in a killable process. With this ON, satellite
    # buys round DOWN to whole shares so EVERY entry rests a GTC bracket at the
    # exchange; a budget under one share is rejected, not downgraded to an
    # unprotected fractional. Overrides fractional_enabled for NEW buys.
    # Partial sells (scale-out, regime trim) also round to whole shares so no
    # fractional dust is left behind.
    # DEFAULT OFF (2026-07-05 decision): this bot runs solo with a small live
    # float ($100-1000) where whole shares would exclude nearly every screened
    # name — fractional sizing + the watchdog/account brakes are the accepted
    # trade at that size (max loss is bounded by the float; the per-trade risk
    # cap bounds each position). Turn ON for a $10k+ account, and on the paper
    # RECORD account, where one share of most names is affordable and every
    # entry can rest a real exchange bracket.
    whole_shares_only: bool = False
    # --- churn guards (2026-07-06 log: 10 same-day LLY top-ups, incl. $2-$8
    # dust orders, while every diversifying buy starved on "Budget $0.00") ---
    # Dust guard: min order also scales with equity (max of min_order_usd and
    # this % of equity), so a $98k book can't fire $2 orders that pay spread
    # for nothing while a $500 float still trades. 0 = off.
    min_order_pct: float = 0.05
    # Same-symbol top-up spacing: refuse a BUY of a name we already bought less
    # than this many hours ago. Adds should be spaced decisions, not a reflex
    # every 30-min cycle. 0 = off.
    min_add_interval_hours: float = 4.0
    # Post-exit re-entry cooldown: refuse a fresh BUY of a name we EXITED less
    # than this many hours ago (trail/stop/take/time/decision). Instant re-buys
    # pay the spread twice and usually chase the same falling knife. 0 = off.
    reentry_cooldown_hours: float = 24.0


@dataclass(frozen=True)
class ScreenerConfig:
    """Market-discovery layer: scans for smart-money activity and surfaces NEW
    candidate tickers BEFORE the per-symbol signal layer runs. Bounded so a wide
    scan can't blow up API spend or the decision prompt."""
    enabled: bool                    # master gate for the discovery scan
    sources: tuple[str, ...]         # which screeners to run (congress/insider/options_flow)
    max_candidates: int              # hard cap on discovered names per cycle
    min_score: float                 # drop candidates whose |smart-money score| is below this
    options_flow_scan_limit: int     # size of the most-actives pool the flow screener scans
    insider_scan_limit: int          # how many recent EDGAR Form-4 filings the insider screener parses


@dataclass(frozen=True)
class Config:
    mode: TradingMode
    kill_switch: bool

    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str

    anthropic_api_key: str
    decision_model: str
    decision_effort: str
    decision_timeout_s: float        # hard cap on the LLM decision call

    fmp_api_key: str
    finnhub_api_key: str
    quiver_api_key: str
    fred_api_key: str
    polygon_api_key: str
    sec_user_agent: str

    # Read-only Robinhood via official Agentic Trading MCP (context only).
    # We call READ tools only; orders always go through Alpaca + RiskManager.
    robinhood_enabled: bool
    robinhood_mcp_url: str
    robinhood_mcp_token: str          # legacy: a pre-obtained OAuth access token (Bearer)
    robinhood_positions_tool: str     # MCP tool name that returns positions
    robinhood_account_number: str     # which RH account to read (blank = auto-pick agentic)
    # OAuth handshake (preferred over a pasted token). `robinhood_auth login`
    # runs the PKCE flow once and persists access+refresh tokens to this file;
    # the reader then loads + auto-refreshes them. Scope/port/name are the DCR
    # + authorization-request parameters.
    robinhood_oauth_file: str         # where the persisted OAuth tokens live
    robinhood_scope: str              # OAuth scope requested (RH advertises "internal")
    robinhood_callback_port: int      # localhost port for the redirect during login
    robinhood_client_name: str        # client_name shown at DCR / on the consent screen

    benchmark_symbol: str

    decision_interval_s: int
    monitor_interval_s: int

    # Runtime control plane — checked every loop so you can intervene WITHOUT
    # restarting. Creating kill_switch_file halts new buys; state_file persists
    # the drawdown high-water mark and the halt latch across restarts.
    kill_switch_file: str
    state_file: str
    dashboard_file: str              # auto-regen this HTML each cycle ("" = off)

    risk: RiskLimits
    screener: ScreenerConfig
    alerts: AlertConfig              # where watchdog CRITICALs page (email/webhook)

    # Core-satellite (Todo 1.6): if CORE_ETF is set, top the book up to
    # TARGET_INVESTED_PCT with that broad ETF after each decision cycle, so idle
    # cash isn't a structural short against the benchmark. Off when core_etf="".
    core_etf: str = ""
    target_invested_pct: float = 0.0
    # GA-2.3: standalone GTC stop protecting the CORE position at the exchange,
    # this % under its average basis (the core accumulates via notional buys and
    # previously had NO exchange-side stop — watchdog-only). Covers the whole-
    # share part of the position (Alpaca rejects GTC on fractional qty); the
    # sub-share residual stays watchdog-guarded. 0 = off (that is the written-
    # acceptance path: broad-ETF gap risk accepted, GA-2.2 paging compensates).
    core_stop_pct: float = 15.0
    # GA-1.2: auto-regenerated public track-record page ("" = off). Distinct
    # from dashboard_file: this one carries the benchmark comparison, per-source
    # attribution, and the baked-in hypothetical-performance disclaimers.
    track_record_file: str = ""

    # Ops hardening (goGA GA-2.1/2.2). heartbeat_url: an external dead-man
    # monitor (e.g. healthchecks.io ping URL) GET-pinged from the watchdog
    # thread each tick — but only while the MAIN loop is also fresh, so a hung
    # decision thread stops the pings and the external monitor pages. "" = off.
    # reconcile_halt_enabled: a reject/partial found at reconcile means the
    # ledger and the real book have DIVERGED — halt new buys (via the kill-
    # switch file) until a human deletes the file to acknowledge.
    heartbeat_url: str = ""
    reconcile_halt_enabled: bool = True

    @property
    def is_live(self) -> bool:
        return self.mode is TradingMode.LIVE

    @property
    def can_open_orders(self) -> bool:
        """True only when new orders are permitted to be placed at all."""
        return not self.kill_switch


def load_config() -> Config:
    mode = TradingMode(os.getenv("TRADING_MODE", "paper").strip().lower())
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")

    # --- Safety interlock: refuse "live" pointed at the paper endpoint and
    #     vice-versa, so a half-edited .env can't silently trade real money. ---
    if mode is TradingMode.LIVE and "paper" in base_url:
        raise ValueError(
            "TRADING_MODE=live but ALPACA_BASE_URL points at paper. "
            "Set ALPACA_BASE_URL=https://api.alpaca.markets for live trading."
        )
    if mode is TradingMode.PAPER and "paper" not in base_url:
        raise ValueError(
            "TRADING_MODE=paper but ALPACA_BASE_URL is not the paper endpoint. "
            "Refusing to start to avoid accidental live trading."
        )

    cfg = Config(
        mode=mode,
        kill_switch=_flag("KILL_SWITCH"),
        alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        alpaca_base_url=base_url,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        decision_model=os.getenv("DECISION_MODEL", "claude-opus-4-8"),
        # Thinking depth / token spend for the decision call. low|medium|high|max
        # (also xhigh on Opus 4.7+). Lower = fewer thinking tokens = lower output
        # cost, which is the dominant cost driver. Defaults to medium.
        decision_effort=(
            os.getenv("DECISION_EFFORT", "medium").strip().lower()
            if os.getenv("DECISION_EFFORT", "medium").strip().lower()
            in {"low", "medium", "high", "xhigh", "max"}
            else "medium"
        ),
        decision_timeout_s=_f("DECISION_TIMEOUT_SECONDS", 90.0),
        fmp_api_key=os.getenv("FMP_API_KEY", ""),
        finnhub_api_key=os.getenv("FINNHUB_API_KEY", ""),
        quiver_api_key=os.getenv("QUIVER_API_KEY", ""),
        fred_api_key=os.getenv("FRED_API_KEY", ""),
        polygon_api_key=os.getenv("POLYGON_API_KEY", ""),
        sec_user_agent=os.getenv(
            "SEC_USER_AGENT", "investment-strategy-bot contact@example.com"
        ),
        robinhood_enabled=_flag("ROBINHOOD_ENABLED"),
        robinhood_mcp_url=os.getenv(
            "ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"
        ),
        robinhood_mcp_token=os.getenv("ROBINHOOD_MCP_TOKEN", ""),
        robinhood_positions_tool=os.getenv("ROBINHOOD_POSITIONS_TOOL", ""),
        robinhood_account_number=os.getenv("ROBINHOOD_ACCOUNT_NUMBER", "").strip(),
        robinhood_oauth_file=os.getenv(
            "ROBINHOOD_OAUTH_FILE", "state/robinhood_oauth.json"
        ),
        robinhood_scope=os.getenv("ROBINHOOD_SCOPE", "internal"),
        robinhood_callback_port=_i("ROBINHOOD_CALLBACK_PORT", 8765),
        robinhood_client_name=os.getenv(
            "ROBINHOOD_CLIENT_NAME", "Investment Strategy Bot"
        ),
        benchmark_symbol=os.getenv("BENCHMARK_SYMBOL", "QQQ").upper(),
        decision_interval_s=_i("DECISION_INTERVAL_SECONDS", 900),
        monitor_interval_s=_i("MONITOR_INTERVAL_SECONDS", 30),
        kill_switch_file=os.getenv("KILL_SWITCH_FILE", "state/KILL"),
        heartbeat_url=os.getenv("HEARTBEAT_URL", "").strip(),
        reconcile_halt_enabled=_flag("RECONCILE_HALT", "on"),
        state_file=os.getenv("STATE_FILE", "state/risk_state.json"),
        dashboard_file=os.getenv("DASHBOARD_FILE", "").strip(),
        risk=RiskLimits(
            max_position_pct=_f("MAX_POSITION_PCT", 5.0),
            max_symbol_exposure_pct=_f("MAX_SYMBOL_EXPOSURE_PCT", 10.0),
            # 100 = never deploy beyond equity (no margin/leverage). On a margin
            # account this is the explicit no-leverage guard; set <100 to hold back.
            max_gross_exposure_pct=_f("MAX_GROSS_EXPOSURE_PCT", 100.0),
            max_sector_exposure_pct=_f("MAX_SECTOR_EXPOSURE_PCT", 30.0),
            regime_filter_enabled=_flag("REGIME_FILTER_ENABLED", "on"),
            # When the regime read fails (yfinance down), the sector cap is almost
            # certainly blind too — so size DOWN to this fraction instead of failing
            # open to full size (1B.7). Still trades (never blocks), just smaller.
            regime_degraded_mult=_f("REGIME_DEGRADED_MULT", 0.5),
            # Regime-off book TRIM (1B.6): on the flip INTO risk-off, sell this % of
            # every held name to actively de-risk the EXISTING book (the regime
            # multiplier otherwise only shrinks NEW buys). Fires once per downturn.
            # Off by default: it re-protects the trimmed remainder via the watchdog
            # (the exchange bracket is released), so opt in deliberately.
            regime_trim_enabled=_flag("REGIME_TRIM_ENABLED", "off"),
            regime_trim_pct=_f("REGIME_TRIM_PCT", 25.0),
            max_daily_loss_pct=_f("MAX_DAILY_LOSS_PCT", 3.0),
            max_drawdown_pct=_f("MAX_DRAWDOWN_PCT", 15.0),
            # % of the PEAK high-water mark; below it the watchdog flattens + latches
            # a halt. As a % it auto-scales to any account size (paper or live) — no
            # need to re-tune a dollar value. 0 = off.
            equity_floor_pct=_f("EQUITY_FLOOR_PCT", 60.0),
            max_open_positions=_i("MAX_OPEN_POSITIONS", 15),
            min_cash_buffer_pct=_f("MIN_CASH_BUFFER_PCT", 10.0),
            min_trade_price_usd=_f("MIN_TRADE_PRICE_USD", 5.0),
            earnings_blackout_days=_i("EARNINGS_BLACKOUT_DAYS", 3),
            # Recycle dead money: a name held MAX_HOLD_DAYS that never got above
            # TIME_STOP_MIN_GAIN_PCT is closed so the capital can rotate to a live
            # thesis instead of sitting in a stalled position forever. 0 = off.
            max_hold_days=_f("MAX_HOLD_DAYS", 30.0),
            time_stop_min_gain_pct=_f("TIME_STOP_MIN_GAIN_PCT", 2.0),
            # Sell a held name whose fresh signals no longer corroborate the entry
            # thesis (no signal at/above thesis_min_score), past a grace age. Runs
            # in the decision cycle but does NOT need the LLM. Opt-in: a transient
            # data outage that blanks signals could otherwise force spurious exits.
            thesis_decay_enabled=_flag("THESIS_DECAY_ENABLED", "off"),
            thesis_decay_min_age_days=_f("THESIS_DECAY_MIN_AGE_DAYS", 3.0),
            thesis_min_score=_f("THESIS_MIN_SCORE", 0.1),
            pdt_guard_enabled=_flag("PDT_GUARD_ENABLED", "on"),
            max_day_trades_under_25k=_i("MAX_DAY_TRADES_UNDER_25K", 3),
            # Conviction floor: a barely-there 0.1 idea that merely clears the
            # friction floor still costs spread + slippage and dilutes the book.
            # Require a real edge before risking capital. 0 = off.
            min_conviction=_f("MIN_CONVICTION", 0.2),
            # The classic "risk 1% of the account per trade" rule. Bounds the
            # ABSOLUTE $ lost if the stop fires, independent of the % weight; as a
            # % it auto-scales from the $100 live float to the $100k paper book.
            max_trade_risk_pct=_f("MAX_TRADE_RISK_PCT", 1.0),
            # Estimated one-way friction (bid/ask spread + slippage) as % of
            # notional. Round-trip cost = 2x this; a profit target that can't beat
            # it by MIN_EDGE_RATIO is negative-expectancy on entry and refused.
            est_slippage_pct=_f("EST_SLIPPAGE_PCT", 0.10),
            min_edge_ratio=_f("MIN_EDGE_RATIO", 2.0),
            fractional_enabled=_flag("FRACTIONAL_ENABLED", "on"),
            min_order_usd=_f("MIN_ORDER_USD", 1.0),
            default_stop_loss_pct=_f("DEFAULT_STOP_LOSS_PCT", 5.0),
            default_take_profit_pct=_f("DEFAULT_TAKE_PROFIT_PCT", 12.0),
            # Sell half at the first target and trail the rest by default, so the
            # asymmetric winners that pay for the losers aren't capped at +12%.
            scale_out_enabled=_flag("SCALE_OUT_ENABLED", "on"),
            scale_out_pct=_f("SCALE_OUT_PCT", 50.0),
            kelly_fraction=_f("KELLY_FRACTION", 0.5),
            target_annual_vol_pct=_f("TARGET_ANNUAL_VOL_PCT", 25.0),
            options_enabled=_flag("OPTIONS_ENABLED"),
            max_option_premium_pct=_f("MAX_OPTION_PREMIUM_PCT", 1.0),
            # R.1 vol-scaled stops: off until the --sweep-stops evidence says
            # otherwise for this account's basket; flip in .env when it does.
            vol_stops_enabled=_flag("VOL_STOPS_ENABLED"),
            vol_stop_mult=_f("VOL_STOP_MULT", 2.0),
            vol_stop_take_ratio=_f("VOL_STOP_TAKE_RATIO", 2.5),
            vol_stop_min_pct=_f("VOL_STOP_MIN_PCT", 4.0),
            vol_stop_max_pct=_f("VOL_STOP_MAX_PCT", 15.0),
            trail_giveback_pct=_f("TRAIL_GIVEBACK_PCT", 3.0),
            # R.2: 0 disables; 0.85 = "effectively the same trade" line (two
            # normal tech megacaps sit ~0.6-0.8; near-clones sit above 0.85).
            max_pairwise_corr=_f("MAX_PAIRWISE_CORR", 0.85),
            # GA-2.3: OFF by default — small-float solo mode runs fractional
            # (see the RiskLimits field note). Set on for the paper record
            # account and any $10k+ live account so every entry rests an
            # exchange-resident GTC bracket.
            whole_shares_only=_flag("WHOLE_SHARES_ONLY", "off"),
            # Churn guards (see the RiskLimits field notes).
            min_order_pct=_f("MIN_ORDER_PCT", 0.05),
            min_add_interval_hours=_f("MIN_ADD_INTERVAL_HOURS", 4.0),
            reentry_cooldown_hours=_f("REENTRY_COOLDOWN_HOURS", 24.0),
        ),
        screener=ScreenerConfig(
            enabled=_flag("SCREENER_ENABLED", "on"),
            sources=tuple(
                s.strip().lower()
                for s in os.getenv(
                    "SCREENER_SOURCES", "congress,insider,options_flow"
                ).split(",")
                if s.strip()
            ),
            # X.4: 12 re-throttled the now-3-feed discovery at the aggregator;
            # 18 lets the full breadth actually reach the model.
            max_candidates=_i("MAX_DISCOVERED_CANDIDATES", 18),
            min_score=_f("SCREENER_MIN_SCORE", 0.2),
            options_flow_scan_limit=_i("OPTIONS_FLOW_SCAN_LIMIT", 40),
            # Open-market insider BUYS are rare in any small window, so scan a wide
            # slice of EDGAR's ~100-filing "latest filings" feed to actually catch a
            # cluster (was wrongly sharing options_flow_scan_limit=40).
            insider_scan_limit=_i("INSIDER_SCAN_LIMIT", 100),
        ),
        alerts=load_alert_config(os.getenv),
        # Core-satellite fill (Todo 1.6). CORE_ETF unset/"" disables it entirely;
        # TARGET_INVESTED_PCT is clamped to the no-leverage gross cap downstream.
        core_etf=os.getenv("CORE_ETF", "").strip().upper(),
        target_invested_pct=_f("TARGET_INVESTED_PCT", 0.0),
        core_stop_pct=_f("CORE_STOP_PCT", 15.0),
        track_record_file=os.getenv("TRACK_RECORD_FILE", "").strip(),
    )

    missing = [
        k for k, v in {
            "ALPACA_API_KEY": cfg.alpaca_api_key,
            "ALPACA_SECRET_KEY": cfg.alpaca_secret_key,
            "ANTHROPIC_API_KEY": cfg.anthropic_api_key,
        }.items() if not v
    ]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    return cfg
