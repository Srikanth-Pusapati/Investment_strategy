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
    # Option positions have NO exchange bracket and per-share P&L math, so the
    # watchdog runs dedicated deterministic exits on the NET PREMIUM of each
    # (underlying, expiry) structure. Defaulted fields — existing RiskLimits(...)
    # call sites keep working; all inert while options_enabled is off.
    option_stop_loss_pct: float = 50.0    # exit at -X% of premium paid (0=off)
    option_take_profit_pct: float = 100.0 # exit at +X% of premium paid (0=off)
    option_close_dte: float = 3.0         # exit <= X days to expiry (0=off)
    # Entry-side sanity for the options gate (all 0=off):
    min_option_dte: float = 7.0           # no lottery-ticket weeklies
    max_option_dte: float = 60.0          # no far-dated time-value sinks
    min_option_open_interest: float = 100.0  # per-leg OI floor (exit liquidity)
    max_option_spread_pct: float = 10.0   # per-leg bid-ask spread ceiling
    max_option_positions: int = 3         # distinct underlyings with open options
    # Premium/size sanity (the 2026-07-23 T blowup: a stale $0.01 bid with no
    # ask was accepted as a real mid, sized to 900 dead contracts, and rode to
    # -100%). Floor the cheapest LEG (not the net — a spread's small net can
    # hide a junk penny leg) and hard-cap the contract count so a mis-estimated
    # premium can never translate into a thin-book monster order. Both 0=off.
    min_option_premium: float = 0.10      # per-leg mid floor $/share; sub-floor = deep-OTM/illiquid junk
    max_option_contracts: int = 50        # hard ceiling on contracts per structure
    # Direction discipline: option debits must trade WITH the long-run market
    # trend (SPY vs 200dma) — calls in an up market, puts in a down market.
    # Puts in an uptrend bleed theta against the tape (the Jul-13..23 trial
    # held zero puts through risk-on precisely because the prompt said so;
    # this makes it deterministic and adds the symmetric call block). Carve-
    # outs: puts on a name in its OWN 200dma breakdown (the bearish-slate
    # pipeline shorts single names in any tape), puts that HEDGE a held
    # equity, and puts when the regime label reads risk-off (the put mandate
    # must never be fought by its own gate). Inert unless
    # regime_filter_enabled — the trend is only read under that flag.
    option_direction_gate: bool = True    # OPTION_DIRECTION_GATE (on/off)
    # Per-underlying premium concentration cap (Aug 12-21 forensic review):
    # max_option_premium_pct bounds each PLAY's debit, but nothing bounded the
    # PILE — AMZN stacked ~$29.8k of open premium across structures on one
    # underlying and lost -$14,956. Cap the TOTAL open net premium per
    # underlying (existing lots + the new debit) at this % of equity; a new
    # entry is clamped into the remaining headroom and rejected when even one
    # contract no longer fits. Sanctioned hedges (falling-market index put /
    # put-liquidity proxy) are exempt — they concentrate on a fixed venue by
    # design. 0 = off.
    per_underlying_premium_pct: float = 0.5
    # Exit-side mark hardening (Aug 12-21 forensic: HL premium-stopped -67.6%
    # 14s after the open on a junk one-sided auction quote while the underlying
    # traded UP — the Jul-23 failure mode, which PR #40 hardened on ENTRIES
    # only). The watchdog's premium stop/take must never fire off one bad tick:
    # require consecutive breach ticks, refuse one-sided/absurd NBBOs as
    # countable evidence, and distrust marks in the open-auction minutes.
    option_exit_max_spread_pct: float = 10.0  # exit-mark NBBO spread ceiling for a countable breach tick (0=no ceiling; same number as the entry-side cap)
    option_stop_confirm_ticks: int = 2        # consecutive watchdog ticks (~30s apart) a premium stop/take breach must persist (1=old single-tick behavior)
    option_stop_open_mute_min: float = 5.0    # minutes after 09:30 ET to suppress premium-stop closes unless the UNDERLYING gapped adversely (0=off)
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
    vol_stop_max_pct: float = 10.0   # clamp: never wider than this stop. Lowered
    # from 15 (2026-07 audit): a 15% clamp let high-IV names (MU ~14%) carry ~3x
    # the dollar risk of quiet peers because the position-weight cap binds before
    # the per-trade $-risk cap. A tighter clamp keeps dollar risk more uniform;
    # the trade-off is more noise stop-outs on volatile names — monitor.
    # Trailing-stop giveback: % of the peak gain surrendered before the
    # watchdog (and the backtest's mirror of it) closes a runner. Previously a
    # hardcoded 3.0 in both places.
    trail_giveback_pct: float = 3.0
    # --- R-scaled trail geometry (Jul-25 calibration: winners captured a
    # median 8% of their target and ZERO trips reached 80% of take — the fixed
    # 3% giveback armed at any +3.1% peak and clipped every runner at ~+1%
    # while vol-scaled stops risked 5-7%. Payoff 0.56 needs a 64% win rate to
    # break even.) Both knobs express the trail in units of the position's OWN
    # planned stop width (R): 0 = off = legacy fixed-% behavior; positions with
    # no known stop (bracket-carrying whole-share names) always use legacy.
    # Arm the trail only once peak gain >= this many R (peak has covered its
    # own risk), so sub-1R pops are governed by stop/take/decision instead of
    # being micro-banked at +0.x%.
    trail_arm_r: float = 0.0
    # Giveback = max(trail_giveback_pct, this x stop width) — volatile names
    # get proportionally more room, exactly like their stops do.
    trail_giveback_r: float = 0.0
    # Gate the trailing-stop ratchet + trigger to regular trading hours. Thin
    # pre/post-market prints are unreliable — a bad mark either ratchets the
    # high-water mark to a phantom peak or fires the trail into an
    # extended-hours limit that can't fill (LPLA 2026-07-20: trail fired 07:16
    # premarket, the exit sat "new" 74+ min). Hard stops, the equity floor, and
    # the daily-loss flatten stay 24/7 — only the trail is RTH-gated. Off =
    # legacy always-on behavior.
    trail_rth_only: bool = True
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
    # Price-aware re-entry guard: re-buying a recently exited name AT OR ABOVE
    # the price we sold it for is chasing (CVX +3.9%, PATH +6.4%, HUBB +2.3%
    # re-entries the time-only cooldown couldn't see). Blocked while the exit
    # clock is warm unless the composite clears the override (genuine new edge).
    reentry_price_guard_enabled: bool = True
    reentry_price_override_composite: float = 0.5
    # --- daily concentration brake (2026-07-06: LLY took ~81% of the day's buy
    # dollars across 10 orders; the guards above space the orders but nothing
    # capped the DAY). All three are hard, deterministic, and per ET trading
    # day; accumulators persist in PortfolioState so a restart can't refresh
    # the budget mid-session. ---
    # Max % of equity deployed into ONE symbol per trading day. Conviction can
    # still build a position — over days, not hours. 0 = off.
    max_daily_symbol_deploy_pct: float = 0.0   # 0 = off; load_config enables 8.0
    # Max submitted BUY orders per symbol per trading day. 0 = off.
    max_daily_buys_per_symbol: int = 0          # 0 = off; load_config enables 3
    # Max share of one CYCLE's deployable cash a single symbol may take when
    # two or more distinct buys compete (the fair-share split can otherwise
    # still hand one name nearly everything via conviction weighting). Single-
    # buy cycles are uncapped — the daily ceiling above covers that grind.
    # 100 = off.
    max_cycle_symbol_share_pct: float = 100.0  # 100 = off; load_config enables 60.0
    # Top-up evidence gate: an ADD to a held name must show conviction at least
    # this much ABOVE the prior entry's — "adding to a winner" with the same
    # number is a reflex, not a signal. Fails open on a missing prior. 0 = off.
    topup_min_conviction_delta: float = 0.0    # 0 = off; load_config enables 0.05
    # Fail-closed sizing when a concentration guard is BLIND: if sector or
    # correlation data is missing for a new buy, multiply size by this instead
    # of skipping the guard (multipliers stack — blinder = smaller). 1.0
    # restores the old fail-open behavior.
    missing_data_mult: float = 1.0
    # --- anti-chasing overextension gate (week of 2026-07-13: 68% of realized
    # losses were momentum entries near local tops — CDW/SOFI/PATH — bought on
    # high RSI + bullish flow and run straight to their stops). Fires when RSI
    # AND price extension over the 20d SMA are BOTH elevated; fails open on
    # missing technicals. ---
    overextension_gate_enabled: bool = True
    overextension_mode: str = "haircut"  # "haircut" (downsize) | "block" (reject)
    overext_rsi: float = 65.0            # RSI14 leg of the hot-AND-extended trigger
    overext_atr_mult: float = 2.0        # extension leg: price >= this many ATRs over SMA20
    overext_pct: float = 8.0             # fallback extension leg (%) when ATR unavailable
    overext_haircut: float = 0.5         # size multiplier in haircut mode
    # Extreme extension fires ALONE, regardless of RSI: the actual Jul-13
    # losers entered at RSI 61-64 but 3.4-4.0 ATRs over the 20d SMA — an RSI
    # floor must not muzzle a screaming extension. 0 = off.
    overext_extreme_atr_mult: float = 3.0
    # How the EXTREME leg acts, independent of overextension_mode. In the shared
    # "haircut" mode the extreme trigger only halved size — CVX still bought
    # $2,799 at 3.2xATR on Jul 17, exactly the >=3xATR trade class this leg
    # exists to STOP. A screaming extension is a different risk than a mild one,
    # so it hard-rejects by default. "haircut" restores the old shared behavior.
    overext_extreme_mode: str = "block"  # "block" (reject) | "haircut" (downsize)
    # --- deterministic weighted composite index (signals/composite.py) ---
    composite_enabled: bool = True       # compute + render the per-candidate index
    composite_budget_blend: bool = True  # blend into the cycle budget split weights
    composite_gate_enabled: bool = False # opt-in deterministic buy floor (backtest first)
    min_composite_score: float = 0.0     # floor value when the gate is on
    composite_perf_min_trips: int = 3    # closed trips before a source's perf weight != 1.0
    # --- rotation loss guard (Jul-13 week: UNH -$204 / HUBB -$158 realized
    # purely to free a slot). Enforces the +0.10 edge the prompt only asks
    # for, ONLY on cap-forced sells that lock in a real loss. ---
    rotation_loss_guard_enabled: bool = True
    rotation_guard_min_loss_pct: float = 4.0    # only sells losing more than this are guarded
    rotation_min_conviction_edge: float = 0.10  # incoming must beat incumbent entry by this
    rotation_require_composite_edge: bool = False  # also demand a composite edge (opt-in)
    # A SELL whose OWN conviction is at/above this is a risk-off exit, never a
    # slot-freeing rotation — exempt it so a co-occurring unrelated buy can't
    # get a thesis-broken exit vetoed ("never block a legitimate exit"). 0=off.
    rotation_guard_exempt_sell_conviction: float = 0.65
    # Deterioration releases (SPCX 2026-07-22: the guard vetoed the model's
    # exit at -5.4% -> -6.6% -> -9.3% and the position rode into its bracket
    # stop at -9.8%; the guard's job is stopping LUKEWARM loss-locking, not
    # pinning a sinking position until the stop fires). Two escapes:
    # (a) depth: a sell losing MORE than this never gets vetoed — past this
    #     point the "avoided" loss is already worse than the rotation it
    #     blocked, and only the bracket stop remains. 0 = off.
    rotation_guard_max_loss_pct: float = 8.0
    # (b) persistence: a sell the guard ALREADY vetoed earlier the same day is
    #     released once the loss has deteriorated by at least this many
    #     percentage points since the first veto — a repeated exit request
    #     across cycles with a worsening loss is a thesis-break, not churn.
    #     0 = off.
    rotation_guard_repeat_release_pct: float = 0.75
    # New-name conviction floor (Jul 17-22: every ~-10% realized loss — MU
    # -$441, SPCX -$83, SPCX -$228 — was a FRESH position opened at 0.45-0.50
    # conviction on lagged/crowd theses; the model itself was sub-coin-flip).
    # A fresh name must clear this; top-ups keep the lower min_conviction
    # floor (the position already earned its slot). 0 = off.
    min_new_name_conviction: float = 0.5
    # --- starter haircut (Jul 27-29: BEP -$3,189 and NU -$3,291 were both
    # fresh names entered AT the conviction floor with the stop clamped at the
    # 4% vol-stop floor — the recurring loss geometry of this book. Raising
    # the conviction floor would have blocked PATH, the only winner, so the
    # fix is SIZE: a floor-conviction or floor-stop starter deploys at half
    # size until the thesis earns a top-up.) ---
    starter_haircut_enabled: bool = True
    starter_full_conviction: float = 0.65  # starters below this conviction are halved
    starter_haircut_mult: float = 0.5      # size multiplier for haircut starters
    # Widen a vol-scaled stop to at least the entry's extension over the 20d
    # SMA (still clamped to vol_stop_max_pct). NU Jul 28: entered 6.8% above
    # the SMA with a 4.1% stop — the stop rested INSIDE the base it broke out
    # from, so ordinary mean reversion tagged it at the session low. A stop
    # that at least reaches back to the mean is not tagged by noise; the
    # per-trade $-risk cap shrinks SIZE to keep dollar risk flat.
    stop_cover_extension: bool = True
    # Gap-day chase trigger: an entry more than this % above the PRIOR daily
    # close fires the overextension gate's extreme leg regardless of RSI/ATR
    # (VRRM Jul 29: bought +28% over prior close; the gap bar inflated its own
    # ATR denominator 41%, deflating a 4.1x extension read to 2.93x — under
    # the 3.0x block). 0 = off.
    overext_gap_pct: float = 15.0
    # Loss-streak re-entry bar: a symbol whose last N closed trips ALL lost
    # needs the composite override bar (reentry_price_override_composite) to
    # open a fresh position — the book must stop paying the same name's spread
    # to lose a third time. 0 = off.
    loss_streak_guard: int = 2
    # --- expectancy gate on SIGNAL FAMILIES (Jul 30 review, roadmap item
    # #10): NU, NOK and BEP were all fresh entries whose cited thesis came
    # from the same families (options-flow momentum / congress) while those
    # families' trailing realized expectancy was negative. A family that is
    # demonstrably losing money right now doesn't earn NEW starters; top-ups
    # are exempt (the position already cleared entry), and a family with
    # fewer than min_trips closed trips in the window is never judged. ---
    expectancy_gate_enabled: bool = True   # EXPECTANCY_GATE (on/off)
    expectancy_gate_min_trips: int = 8     # closed trips before a family is judged
    expectancy_gate_window_days: int = 14  # trailing window for the read
    # --- corroboration gate (Aug 12-21 forensic review): insider-cited
    # entries ran -$18,681 across 13 trades, and every single-soft-signal
    # starter (QNT, LFTO, INTC, F, AVBC) failed fast. Conviction floors
    # provably cannot express this — the autotune sweeps moved 0 trades at
    # every candidate floor. A FRESH name whose cited signal set is exactly
    # ONE soft family (insider / congress / options_flow) with zero
    # fundamentals/news/technical corroboration deploys at the starter-
    # haircut fraction AND needs the composite at/above the bar to enter at
    # all (fails open on a missing composite — best-effort feed, not a
    # required one). Top-ups exempt; the expectancy gate stays the family-
    # level backstop. ---
    corroboration_gate_enabled: bool = True   # CORROBORATION_GATE_ENABLED
    corroboration_min_composite: float = 1.25 # composite bar for a solo soft signal
    # Red-day rotation release (Jul 29: the guard held NOK's -4.8% exit open
    # ~2h into a losing session). On a day the BOOK is losing, a loss-cut the
    # model asks for is defense, not lukewarm churn — the guard yields to any
    # sell already past the guard's own min-loss band. On/off only; the band
    # edges stay rotation_guard_min/max_loss_pct.
    rotation_guard_red_day_release: bool = True
    # Put carve-out for broken MOMENTUM names (Jul 30 review): the direction
    # gate only kept a put's candidacy when the name sat below its own 200dma,
    # but the names that actually break (NU, NOK) are recent runners still far
    # ABOVE their 200dma — so every recognized breakdown died at HOLD. A name
    # trading at least this % BELOW its 20d SMA is in a sharp short-term
    # breakdown and keeps its put candidacy in an up market too. 0 = off.
    put_breakdown_ext_pct: float = 5.0
    # --- regime EXPOSURE LADDER (Jul 30 review): the regime multiplier only
    # shrinks NEW buys, so the book's floor posture stays fully-invested-long
    # through any decline. The ladder caps GROSS exposure by regime label —
    # risk-on keeps max_gross_exposure_pct; neutral and risk-off clamp lower
    # (new buys AND the core fill respect it; existing positions are not
    # force-sold — the regime trim / core defense handle that). ---
    exposure_ladder_enabled: bool = True
    exposure_neutral_pct: float = 60.0     # gross-exposure cap in a neutral regime
    exposure_risk_off_pct: float = 30.0    # gross-exposure cap in risk-off


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
    # Downside discovery: guarantee up to `bearish_reserve` slots for the
    # strongest bearish (negative-score) names so a bull-heavy tape can't crowd
    # every short setup off the capped slate — but only for names clearing
    # `bearish_reserve_bar` (a real cluster / strong imbalance), so weak bearish
    # names are never forced in. reserve=0 → pure |score| ranking (old behavior).
    bearish_reserve: int = 4
    bearish_reserve_bar: float = 0.4


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
    polygon_api_key: str    # Options Starter plan: options-flow reads Polygon snapshot (OI + volume)
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
    # Ceiling on the CORE position as a % of equity. The core is exempt from the
    # single-name cap (it IS the diversified core), so idle satellite cash swept
    # it to ~47% of equity with nothing to stop it (2026-07 audit). This caps the
    # per-cycle core BUY so the position never exceeds the ceiling; it does not
    # trim an existing overweight (that stays a manual/decision action to avoid
    # the pending-cancel wedge on the resting GTC stop). 0 = no ceiling.
    core_max_pct: float = 30.0
    # Per-CYCLE ceiling on the core BUY as a % of equity — a DCA throttle. On
    # reset day 2026-07-27 the sweep bought $300k of QQQ (30% of the fresh
    # book) two minutes after the open of a down day, -$3.4k by the close and
    # ~44% of the day's loss. Spreading the fill across cycles averages the
    # entry instead of concentrating it at one print. 0 = no throttle.
    core_fill_max_pct: float = 5.0
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
    # --- core defense (Jul 29: the QQQ core is pure beta — on a falling tape
    # it drags the book down while the DCA fill keeps BUYING the decline).
    # When the market is falling (regime risk-off, long-run trend down, or an
    # intraday benchmark drop beyond market_drop_defense_pct), the core fill
    # PAUSES and the core is trimmed core_defense_trim_pct once per day. The
    # same falling-market read sanctions a defined-risk index put in the
    # decision prompt (profit from the fall, not just less bleed). ---
    core_defense_enabled: bool = True
    market_drop_defense_pct: float = 1.5   # intraday SPY drop that reads "falling"
    core_defense_trim_pct: float = 25.0    # % of the core sold per defense day
    # Per-NAME falling read (Jul 30 review, Phase-1 gap): a HELD name down
    # this % on the day is itself falling, whatever the index reads — Jul 29
    # bottomed -1.2% (under the 1.5% trigger) while NU/NOK broke -5%+ alone.
    # Arms the HELD-line defense note and the rotation guard's loss-cut
    # release for that name. 0 disables. Default 4.0: NOK sat pinned at -4.8%
    # for ~2h on the day this read would have released it.
    name_drop_defense_pct: float = 4.0     # NAME_DROP_DEFENSE_PCT
    # --- deterministic AUTO-HEDGE (Jul 30 review). The Jul-29 index-put
    # sanction is model-discretionary and has fired zero times; the model
    # demonstrably skips discretionary defense. When the falling read holds
    # for auto_hedge_min_cycles consecutive decision cycles, the orchestrator
    # ITSELF buys a 1x inverse ETF (hedge_etf) sized to auto_hedge_ratio x the
    # book's net long exposure — plain-equity path, so brackets/watchdog/
    # cooldowns all apply and it works with OPTIONS_ENABLED off. Unwinds
    # symmetrically once the read clears for the same number of cycles.
    # hedge_etf="" disables. Managed by the system: excluded from the model's
    # slate exactly like the core ETF. ---
    hedge_etf: str = ""                    # HEDGE_ETF (e.g. PSQ / SH; "" = off)
    auto_hedge_ratio: float = 0.30         # hedge notional / net long exposure
    auto_hedge_min_cycles: int = 2         # falling cycles before arming (and clearing)
    auto_hedge_max_pct: float = 15.0       # hedge ceiling as % of equity
    # --- BREADTH trigger for the falling-tape defenses (Aug 18 forensic:
    # -$26,844 at 3.9x SPY down-capture with ELEVEN per-name NAME FALLING
    # reads in one cycle while every defense slept — _market_falling keyed
    # ONLY on the index and SPY never breached the intraday trigger). The
    # core defense / auto-hedge / index-put sanction now ALSO arm when the
    # BOOK itself is falling: >= breadth_falling_names_min held names carry
    # the cycle's falling read, or intraday book P/L is at or below
    # breadth_book_drawdown_pct (% of equity, equity vs last_equity — the
    # same numbers as the risk layer's daily-loss halt). Whipsaw bounds are
    # unchanged: auto_hedge_min_cycles persistence and the auto_hedge_max_pct
    # ceiling apply to whichever source arms the read. 0 disables a leg;
    # the drawdown knob is sign-agnostic (-1.25 and 1.25 both mean a 1.25%
    # intraday loss). ---
    breadth_falling_names_min: int = 3     # BREADTH_FALLING_NAMES_MIN (0 = off)
    breadth_book_drawdown_pct: float = -1.25  # BREADTH_BOOK_DRAWDOWN_PCT (0 = off)
    # --- PUT LIQUIDITY PROXY (Aug 14, window-end ship): the bearish slate
    # surfaces micro-caps whose own chains fail the OI/spread liquidity floor
    # — in the Aug 3-14 window every model-proposed put (EXTR, TDC) died on
    # exactly that gate, so the funnel produced 0 filled puts while red days
    # still bled. When a model-proposed put is rejected FOR LIQUIDITY, the
    # system re-expresses the same bearish read as a deterministic near-ATM
    # bear put spread on a LIQUID proxy ETF (small-cap slate -> IWM default).
    # Deterministic like the auto-hedge; every other option gate (DTE,
    # premium caps, slots, the proxy's own liquidity) still applies. "" = off.
    put_proxy_etf: str = "IWM"             # PUT_PROXY_ETF ("" = off)
    # --- DEFENSIVE CORE (Jul 30 review): while the core defense is active the
    # QQQ fill pauses — but the freed/idle cash then earns nothing. Redirect
    # the core fill into a short-duration T-bill ETF instead (SGOV/BIL), and
    # rotate it back out when the falling read clears so the QQQ core can
    # refill. "" = off (old behavior: cash just sits). ---
    defensive_core_etf: str = ""           # DEFENSIVE_CORE_ETF (e.g. SGOV; "" = off)

    # Ops hardening (goGA GA-2.1/2.2). heartbeat_url: an external dead-man
    # monitor (e.g. healthchecks.io ping URL) GET-pinged from the watchdog
    # thread each tick — but only while the MAIN loop is also fresh, so a hung
    # decision thread stops the pings and the external monitor pages. "" = off.
    # reconcile_halt_enabled: a reject/partial found at reconcile means the
    # ledger and the real book have DIVERGED — halt new buys (via the kill-
    # switch file) until a human deletes the file to acknowledge.
    heartbeat_url: str = ""
    reconcile_halt_enabled: bool = True
    # Close fence (CRITICAL-1): don't START a fresh decision inside this many
    # minutes of the session close, and discard any proposals the LLM returns
    # after the bell — a buy placed this late can't complete before close, and a
    # cycle that spans the close executed after hours (Jul 20: 5 proposals 41
    # min past the close). 0 = off. Reconcile/backfill still run near the close.
    close_fence_minutes: float = 5.0
    # Post-wake settle (CRITICAL-1): after a wall-clock jump larger than this
    # many seconds (laptop sleep/suspend), wait briefly for the network to come
    # back before the first decision cycle, so the read-retry budget isn't burnt
    # while Wi-Fi is still reconnecting. 0 = off.
    wake_settle_seconds: float = 20.0
    # Nightly self post-mortem (B2): on the first market-closed decision tick
    # after a day that has journal entries, feed the day's decisions to Claude
    # and fold its one-line lessons back into the next day's decision prompt.
    postmortem_enabled: bool = True
    postmortem_max_lessons: int = 15
    # Run-6 measurement plumbing (item 1 — no strategy effect). Each knob
    # only changes what gets MEASURED/RECORDED; run-6 default = on.
    #   POSTMORTEM_OPTION_MARKS: nightly post-mortem marks OPEN option groups
    #     close-to-close from option daily bars (broker.option_close_series)
    #     and reports 'unmarked' explicitly when it can't; off = legacy
    #     realized-only option lines.
    postmortem_option_marks: bool = True
    #   EQUITY_CLOSE_FIXED_STAMP: write the day's equity_history row ONCE, at
    #     the first closed-market tick after 16:00 ET, basis='close', and never
    #     overwrite it (Aug 24/25 rows were re-stamped with after-hours marks
    #     every closed tick). off = legacy overwrite-every-closed-tick.
    equity_close_fixed_stamp: bool = True
    #   LEDGER_FILL_PRICES: on FILLED confirmation, write the broker's
    #     filled_avg_price / filled qty / fill time onto the ledger row
    #     (fill_price / fill_qty / fill_ts) and log them in the FILLED line.
    ledger_fill_prices: bool = True
    # Weekly ledger-driven auto-tune report (Jul 22 upgrade): a deterministic,
    # no-LLM replay of the ledger + decisions journal against the entry-quality
    # risk knobs, fired once per ET weekend. Report-only — writes
    # state/autotune/{iso-week}.md; never changes a knob itself.
    autotune_enabled: bool = True
    autotune_days: int = 14
    autotune_min_sample: int = 5
    # C.4 options-chain positioning signal: per-name ATM IV, put-call IV skew
    # and put/call open-interest lean from Alpaca's option snapshots (the free
    # 'indicative' feed the execution path already uses — no extra key). Feeds
    # Claude the options DATA that turns the OPTIONS_ENABLED path from blind
    # guessing into an informed put/call/spread choice. Off by default; flip
    # together with OPTIONS_ENABLED.
    options_chain_signal: bool = False
    options_chain_max_symbols: int = 25   # per-cycle chain-fetch cap (2 calls/name)

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
        close_fence_minutes=_f("CLOSE_FENCE_MINUTES", 5.0),
        wake_settle_seconds=_f("WAKE_SETTLE_SECONDS", 20.0),
        kill_switch_file=os.getenv("KILL_SWITCH_FILE", "state/KILL"),
        heartbeat_url=os.getenv("HEARTBEAT_URL", "").strip(),
        reconcile_halt_enabled=_flag("RECONCILE_HALT", "on"),
        postmortem_enabled=_flag("POSTMORTEM_ENABLED", "on"),
        postmortem_max_lessons=_i("POSTMORTEM_MAX_LESSONS", 15),
        postmortem_option_marks=_flag("POSTMORTEM_OPTION_MARKS", "on"),
        equity_close_fixed_stamp=_flag("EQUITY_CLOSE_FIXED_STAMP", "on"),
        ledger_fill_prices=_flag("LEDGER_FILL_PRICES", "on"),
        autotune_enabled=_flag("AUTOTUNE_ENABLED", "on"),
        autotune_days=_i("AUTOTUNE_DAYS", 14),
        autotune_min_sample=_i("AUTOTUNE_MIN_SAMPLE", 5),
        options_chain_signal=_flag("OPTIONS_CHAIN_SIGNAL"),
        options_chain_max_symbols=_i("OPTIONS_CHAIN_MAX_SYMBOLS", 25),
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
            # Default flipped ON (Jul 30 review): losers were held 3.8d vs
            # winners 2.5d across the full ledger — the deterministic decay
            # exit is the anti-disposition backstop, and the signal-outage
            # concern is covered by the grace period + corroboration check.
            thesis_decay_enabled=_flag("THESIS_DECAY_ENABLED", "on"),
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
            option_stop_loss_pct=_f("OPTION_STOP_LOSS_PCT", 50.0),
            option_take_profit_pct=_f("OPTION_TAKE_PROFIT_PCT", 100.0),
            option_close_dte=_f("OPTION_CLOSE_DTE", 3.0),
            min_option_dte=_f("MIN_OPTION_DTE", 7.0),
            max_option_dte=_f("MAX_OPTION_DTE", 60.0),
            min_option_open_interest=_f("MIN_OPTION_OPEN_INTEREST", 100.0),
            max_option_spread_pct=_f("MAX_OPTION_SPREAD_PCT", 10.0),
            max_option_positions=int(_f("MAX_OPTION_POSITIONS", 3.0)),
            min_option_premium=_f("MIN_OPTION_PREMIUM", 0.10),
            max_option_contracts=int(_f("MAX_OPTION_CONTRACTS", 50.0)),
            option_direction_gate=_flag("OPTION_DIRECTION_GATE", "on"),
            # Per-underlying premium concentration cap (Aug 12-21: AMZN piled
            # ~$29.8k of premium into ONE underlying, -$14,956).
            per_underlying_premium_pct=_f("PER_UNDERLYING_PREMIUM_PCT", 0.5),
            option_exit_max_spread_pct=_f("OPTION_EXIT_MAX_SPREAD_PCT", 10.0),
            option_stop_confirm_ticks=int(_f("OPTION_STOP_CONFIRM_TICKS", 2.0)),
            option_stop_open_mute_min=_f("OPTION_STOP_OPEN_MUTE_MIN", 5.0),
            # R.1 vol-scaled stops: off until the --sweep-stops evidence says
            # otherwise for this account's basket; flip in .env when it does.
            vol_stops_enabled=_flag("VOL_STOPS_ENABLED"),
            vol_stop_mult=_f("VOL_STOP_MULT", 2.0),
            vol_stop_take_ratio=_f("VOL_STOP_TAKE_RATIO", 2.5),
            vol_stop_min_pct=_f("VOL_STOP_MIN_PCT", 4.0),
            vol_stop_max_pct=_f("VOL_STOP_MAX_PCT", 10.0),
            trail_giveback_pct=_f("TRAIL_GIVEBACK_PCT", 3.0),
            trail_arm_r=_f("TRAIL_ARM_R", 0.0),
            trail_giveback_r=_f("TRAIL_GIVEBACK_R", 0.0),
            trail_rth_only=_flag("TRAIL_RTH_ONLY", "on"),
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
            reentry_price_guard_enabled=_flag("REENTRY_PRICE_GUARD_ENABLED", "on"),
            # 0.5 let SPCX re-enter Jul 21 at composite +0.66 (rank ~8 of 30,
            # all lagged congress/crowd weight) $3.58 ABOVE its Jul 17 exit —
            # straight to a -9.8% bracket stop. The override should mean "top
            # decile new edge", not "mildly positive".
            reentry_price_override_composite=_f(
                "REENTRY_PRICE_OVERRIDE_COMPOSITE", 1.25),
            # Daily concentration brake (see the RiskLimits field notes).
            max_daily_symbol_deploy_pct=_f("MAX_DAILY_SYMBOL_DEPLOY_PCT", 8.0),
            max_daily_buys_per_symbol=_i("MAX_DAILY_BUYS_PER_SYMBOL", 3),
            max_cycle_symbol_share_pct=_f("MAX_CYCLE_SYMBOL_SHARE_PCT", 60.0),
            topup_min_conviction_delta=_f("TOPUP_MIN_CONVICTION_DELTA", 0.05),
            missing_data_mult=_f("MISSING_DATA_MULT", 1.0),
            # Anti-chasing overextension gate (see the RiskLimits field notes).
            overextension_gate_enabled=_flag("OVEREXTENSION_GATE_ENABLED", "on"),
            overextension_mode=os.getenv("OVEREXTENSION_MODE", "haircut").strip().lower(),
            overext_rsi=_f("OVEREXT_RSI", 65.0),
            overext_atr_mult=_f("OVEREXT_ATR_MULT", 2.0),
            overext_pct=_f("OVEREXT_PCT", 8.0),
            overext_haircut=_f("OVEREXT_HAIRCUT", 0.5),
            overext_extreme_atr_mult=_f("OVEREXT_EXTREME_ATR_MULT", 3.0),
            overext_extreme_mode=os.getenv(
                "OVEREXT_EXTREME_MODE", "block").strip().lower(),
            # Deterministic weighted composite index (signals/composite.py).
            composite_enabled=_flag("COMPOSITE_ENABLED", "on"),
            composite_budget_blend=_flag("COMPOSITE_BUDGET_BLEND", "on"),
            composite_gate_enabled=_flag("COMPOSITE_GATE_ENABLED", "off"),
            min_composite_score=_f("MIN_COMPOSITE_SCORE", 0.0),
            composite_perf_min_trips=_i("COMPOSITE_PERF_MIN_TRIPS", 3),
            # Rotation loss guard (see the RiskLimits field notes).
            rotation_loss_guard_enabled=_flag("ROTATION_LOSS_GUARD_ENABLED", "on"),
            rotation_guard_min_loss_pct=_f("ROTATION_GUARD_MIN_LOSS_PCT", 4.0),
            rotation_min_conviction_edge=_f("ROTATION_MIN_CONVICTION_EDGE", 0.10),
            rotation_require_composite_edge=_flag("ROTATION_REQUIRE_COMPOSITE_EDGE", "off"),
            rotation_guard_exempt_sell_conviction=_f(
                "ROTATION_GUARD_EXEMPT_SELL_CONVICTION", 0.65
            ),
            rotation_guard_max_loss_pct=_f("ROTATION_GUARD_MAX_LOSS_PCT", 8.0),
            rotation_guard_repeat_release_pct=_f(
                "ROTATION_GUARD_REPEAT_RELEASE_PCT", 0.75
            ),
            min_new_name_conviction=_f("MIN_NEW_NAME_CONVICTION", 0.5),
            # Starter haircut + stop-geometry fixes (Jul 29 loss diagnosis).
            starter_haircut_enabled=_flag("STARTER_HAIRCUT_ENABLED", "on"),
            starter_full_conviction=_f("STARTER_FULL_CONVICTION", 0.65),
            starter_haircut_mult=_f("STARTER_HAIRCUT_MULT", 0.5),
            stop_cover_extension=_flag("STOP_COVER_EXTENSION", "on"),
            overext_gap_pct=_f("OVEREXT_GAP_PCT", 15.0),
            loss_streak_guard=_i("LOSS_STREAK_GUARD", 2),
            # All-weather upgrades (Jul 30 review).
            expectancy_gate_enabled=_flag("EXPECTANCY_GATE", "on"),
            expectancy_gate_min_trips=_i("EXPECTANCY_GATE_MIN_TRIPS", 8),
            expectancy_gate_window_days=_i("EXPECTANCY_GATE_WINDOW_DAYS", 14),
            # Corroboration gate on single-soft-signal starters (Aug 12-21
            # forensic review; see the RiskLimits field notes).
            corroboration_gate_enabled=_flag("CORROBORATION_GATE_ENABLED", "on"),
            corroboration_min_composite=_f("CORROBORATION_MIN_COMPOSITE", 1.25),
            rotation_guard_red_day_release=_flag(
                "ROTATION_GUARD_RED_DAY_RELEASE", "on"
            ),
            put_breakdown_ext_pct=_f("PUT_BREAKDOWN_EXT_PCT", 5.0),
            exposure_ladder_enabled=_flag("EXPOSURE_LADDER", "on"),
            exposure_neutral_pct=_f("EXPOSURE_NEUTRAL_PCT", 60.0),
            exposure_risk_off_pct=_f("EXPOSURE_RISK_OFF_PCT", 30.0),
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
            bearish_reserve=_i("BEARISH_RESERVE", 4),
            bearish_reserve_bar=_f("BEARISH_RESERVE_BAR", 0.4),
        ),
        alerts=load_alert_config(os.getenv),
        # Core-satellite fill (Todo 1.6). CORE_ETF unset/"" disables it entirely;
        # TARGET_INVESTED_PCT is clamped to the no-leverage gross cap downstream.
        core_etf=os.getenv("CORE_ETF", "").strip().upper(),
        target_invested_pct=_f("TARGET_INVESTED_PCT", 0.0),
        core_max_pct=_f("CORE_MAX_PCT", 30.0),
        core_fill_max_pct=_f("CORE_FILL_MAX_PCT", 5.0),
        core_stop_pct=_f("CORE_STOP_PCT", 15.0),
        core_defense_enabled=_flag("CORE_DEFENSE_ENABLED", "on"),
        market_drop_defense_pct=_f("MARKET_DROP_DEFENSE_PCT", 1.5),
        name_drop_defense_pct=_f("NAME_DROP_DEFENSE_PCT", 4.0),
        core_defense_trim_pct=_f("CORE_DEFENSE_TRIM_PCT", 25.0),
        hedge_etf=os.getenv("HEDGE_ETF", "").strip().upper(),
        auto_hedge_ratio=_f("AUTO_HEDGE_RATIO", 0.30),
        auto_hedge_min_cycles=_i("AUTO_HEDGE_MIN_CYCLES", 2),
        auto_hedge_max_pct=_f("AUTO_HEDGE_MAX_PCT", 15.0),
        breadth_falling_names_min=_i("BREADTH_FALLING_NAMES_MIN", 3),
        breadth_book_drawdown_pct=_f("BREADTH_BOOK_DRAWDOWN_PCT", -1.25),
        put_proxy_etf=os.getenv("PUT_PROXY_ETF", "IWM").strip().upper(),
        defensive_core_etf=os.getenv("DEFENSIVE_CORE_ETF", "").strip().upper(),
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
