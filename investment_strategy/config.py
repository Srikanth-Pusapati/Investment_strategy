"""Central configuration. Reads .env once and exposes a typed, validated Config.

This module is the single source of truth for the paper/live switch, the kill
switch, and every hard risk limit. Nothing else should read os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from dotenv import load_dotenv

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
    max_daily_loss_pct: float        # halt new trades past this day loss
    max_drawdown_pct: float          # halt new buys past this PEAK-to-trough DD
    equity_floor_usd: float          # liquidate + latch halt below this equity (0=off)
    max_open_positions: int          # cap concurrent holdings
    min_cash_buffer_pct: float       # never deploy below this cash reserve
    min_trade_price_usd: float       # refuse buys below this price (liquidity guard)
    earnings_blackout_days: int      # block NEW buys within this many days of earnings (0=off)
    # --- fractional shares (for small accounts) ---
    fractional_enabled: bool         # allow sub-share notional buys (no exchange bracket)
    min_order_usd: float             # smallest $ order worth placing (Alpaca min is $1)
    default_stop_loss_pct: float     # bracket stop distance
    default_take_profit_pct: float   # bracket take-profit distance
    # --- survival-first sizing (vol-targeted, fractional-Kelly style) ---
    kelly_fraction: float            # fraction of full Kelly (0..1); 0 disables
    target_annual_vol_pct: float     # per-position volatility budget
    # --- options (defined-risk only) ---
    options_enabled: bool            # master gate for the options path
    max_option_premium_pct: float    # max % equity as debit on one options play


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
    robinhood_mcp_token: str          # OAuth access token (Bearer)
    robinhood_positions_tool: str     # MCP tool name that returns positions

    benchmark_symbol: str

    decision_interval_s: int
    monitor_interval_s: int

    # Runtime control plane — checked every loop so you can intervene WITHOUT
    # restarting. Creating kill_switch_file halts new buys; state_file persists
    # the drawdown high-water mark and the halt latch across restarts.
    kill_switch_file: str
    state_file: str

    risk: RiskLimits
    screener: ScreenerConfig

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
        benchmark_symbol=os.getenv("BENCHMARK_SYMBOL", "QQQ").upper(),
        decision_interval_s=_i("DECISION_INTERVAL_SECONDS", 900),
        monitor_interval_s=_i("MONITOR_INTERVAL_SECONDS", 30),
        kill_switch_file=os.getenv("KILL_SWITCH_FILE", "state/KILL"),
        state_file=os.getenv("STATE_FILE", "state/risk_state.json"),
        risk=RiskLimits(
            max_position_pct=_f("MAX_POSITION_PCT", 5.0),
            max_symbol_exposure_pct=_f("MAX_SYMBOL_EXPOSURE_PCT", 10.0),
            max_daily_loss_pct=_f("MAX_DAILY_LOSS_PCT", 3.0),
            max_drawdown_pct=_f("MAX_DRAWDOWN_PCT", 15.0),
            equity_floor_usd=_f("EQUITY_FLOOR_USD", 0.0),
            max_open_positions=_i("MAX_OPEN_POSITIONS", 15),
            min_cash_buffer_pct=_f("MIN_CASH_BUFFER_PCT", 10.0),
            min_trade_price_usd=_f("MIN_TRADE_PRICE_USD", 5.0),
            earnings_blackout_days=_i("EARNINGS_BLACKOUT_DAYS", 3),
            fractional_enabled=_flag("FRACTIONAL_ENABLED", "on"),
            min_order_usd=_f("MIN_ORDER_USD", 1.0),
            default_stop_loss_pct=_f("DEFAULT_STOP_LOSS_PCT", 5.0),
            default_take_profit_pct=_f("DEFAULT_TAKE_PROFIT_PCT", 12.0),
            kelly_fraction=_f("KELLY_FRACTION", 0.5),
            target_annual_vol_pct=_f("TARGET_ANNUAL_VOL_PCT", 25.0),
            options_enabled=_flag("OPTIONS_ENABLED"),
            max_option_premium_pct=_f("MAX_OPTION_PREMIUM_PCT", 1.0),
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
            max_candidates=_i("MAX_DISCOVERED_CANDIDATES", 12),
            min_score=_f("SCREENER_MIN_SCORE", 0.2),
            options_flow_scan_limit=_i("OPTIONS_FLOW_SCAN_LIMIT", 40),
        ),
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
