"""Preflight readiness check — does everything the bot needs ACTUALLY work?

    python -m investment_strategy.preflight
    python -m investment_strategy.preflight --no-alert-test   # skip the test page

Importing load_config only checks that keys are PRESENT, not that they WORK — so a
wrong / rotated / mis-pasted key still prints "ready" and then 401-loops once the
bot starts. This command goes one step further and actually talks to the vendors,
so a bad key fails HERE with a plain-English message instead of a stack trace at
runtime.

Breadth (goGA GA-2.7): beyond Alpaca + Anthropic, this also exercises every feed
the risk guards silently depend on — Quiver (signals/screeners), yfinance (the
regime + sector guards go BLIND when it's down), the Robinhood MCP token when
enabled — and SENDS a real test alert through the configured sink, so a broken
pager is discovered before market open, not during the incident it was for.
A feed check only fails the gate when that feature is configured/on.

Exit code 0 = good to run; 1 = fix the ❌ items first. Never raises.
"""
from __future__ import annotations

import sys


def _check_config():
    """Config loads + the paper/live interlock is satisfied and required keys set."""
    try:
        from .config import load_config
        cfg = load_config()
        mode = "LIVE 🔴" if cfg.is_live else "paper"
        return True, f"Config loads — mode: {mode}, benchmark {cfg.benchmark_symbol}", cfg
    except Exception as e:
        return False, f"Config error: {e}", None


def _check_alpaca(cfg):
    """Actually authenticate against Alpaca — the check the import trick can't do."""
    try:
        from .execution import AlpacaClient
        acct = AlpacaClient(cfg).get_account()
        return True, (
            f"Alpaca keys work — {cfg.mode.value} equity ${acct.equity:,.0f}, "
            f"${acct.cash:,.0f} cash, {len(acct.positions)} positions"
        )
    except Exception as e:
        msg = str(e)
        hint = ""
        if "401" in msg or "unauthorized" in msg.lower():
            hint = (" — your ALPACA_API_KEY / ALPACA_SECRET_KEY are wrong for this "
                    "endpoint. Check for a stray '#' or extra characters, and that "
                    "paper keys match the paper URL.")
        return False, f"Alpaca auth FAILED{hint}"


def _check_anthropic(cfg):
    """Claude key is present and well-formed (no paid call — format check only)."""
    key = cfg.anthropic_api_key or ""
    if not key:
        return False, "ANTHROPIC_API_KEY is missing."
    if not key.startswith("sk-ant-"):
        return False, "ANTHROPIC_API_KEY doesn't look like a Claude key (should start 'sk-ant-')."
    return True, "Anthropic key present and well-formed (billed per call at run time)."


def _check_quiver(cfg):
    """Exercise the Quiver key with a real (cached-dataset) call. Only gates when
    a key is configured — the bot runs without Quiver, but if you PAY for it and
    it's broken, you want to know before the open, not from an empty slate."""
    if not cfg.quiver_api_key:
        return True, "Quiver key not set (optional) — congress/insider feeds off."
    try:
        from .signals.quiver_client import QuiverClient
        rows = QuiverClient(cfg.quiver_api_key).live("congresstrading")
        if rows:
            return True, f"Quiver works — congresstrading returned {len(rows)} rows."
        return False, ("Quiver returned NO rows for congresstrading — key expired, "
                       "plan changed, or endpoint down. Signals would run blind.")
    except Exception as e:
        return False, f"Quiver call FAILED: {e}"


def _check_regime_feed(cfg):
    """Exercise the yfinance-backed regime read. A degraded read doesn't just
    lose the regime multiplier — the sector cap is blind in the same outage
    (1B.7), so surface it as a failure while the regime filter is enabled."""
    if not cfg.risk.regime_filter_enabled:
        return True, "Regime filter off — yfinance check skipped."
    try:
        from .regime import RegimeReader
        regime = RegimeReader(degraded_mult=cfg.risk.regime_degraded_mult).assess()
        if regime.label == "unknown":
            return False, (f"Regime read DEGRADED ({regime.reason}) — yfinance is "
                           "down/blocked; regime AND sector guards would fly blind.")
        return True, f"Regime feed works — {regime.reason}"
    except Exception as e:
        return False, f"Regime (yfinance) read FAILED: {e}"


def _check_robinhood(cfg):
    """Exercise the Robinhood MCP token by actually reading holdings. Only gates
    when ROBINHOOD_ENABLED=on."""
    if not cfg.robinhood_enabled:
        return True, "Robinhood MCP off (optional)."
    try:
        from .portfolio import RobinhoodReader
        holdings = RobinhoodReader(cfg).holdings()
        if holdings:
            return True, f"Robinhood MCP works — {len(holdings)} external holding(s)."
        return False, ("Robinhood MCP returned no holdings — the OAuth token may "
                       "be expired (re-run `robinhood_auth login`) or the account "
                       "is empty; check the logs to tell which.")
    except Exception as e:
        return False, f"Robinhood MCP read FAILED: {e}"


def _check_options(cfg):
    """Options readiness: the account must be options-approved at Alpaca AND the
    option-chain data feed must actually serve snapshots — flipping
    OPTIONS_ENABLED=on without either means every proposal dies at submit time.
    Skipped (advisory) while options are off."""
    if not cfg.risk.options_enabled:
        return True, "Options OFF (optional) — set OPTIONS_ENABLED=on for defined-risk options."
    try:
        from alpaca.trading.client import TradingClient
        raw = TradingClient(
            cfg.alpaca_api_key, cfg.alpaca_secret_key, paper=not cfg.is_live,
        ).get_account()
        level = int(getattr(raw, "options_trading_level", 0) or 0)
        if level < 2:
            return False, (
                f"Alpaca options_trading_level={level} — long options need level 2+. "
                "Enable options on the account (dashboard -> settings)."
            )
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionChainRequest
        from datetime import date, timedelta
        chain = OptionHistoricalDataClient(
            cfg.alpaca_api_key, cfg.alpaca_secret_key,
        ).get_option_chain(OptionChainRequest(
            underlying_symbol="SPY",
            expiration_date_gte=date.today() + timedelta(days=7),
            expiration_date_lte=date.today() + timedelta(days=35),
        ))
        if not chain:
            return False, ("Options data feed returned an EMPTY SPY chain — the "
                           "options_chain signal and premium estimates would run blind.")
        with_iv = sum(
            1 for s in list(chain.values())[:100]
            if getattr(s, "implied_volatility", None)
        )
        spread_note = "" if level >= 3 else (
            " NOTE: level<3 — single-leg only; Alpaca will refuse MLEG spreads."
        )
        return True, (
            f"Options ready — trading level {level}; SPY chain {len(chain)} "
            f"contracts ({with_iv}/100 sampled carry IV).{spread_note}"
        )
    except Exception as e:
        return False, f"Options preflight FAILED: {e}"


def _check_alert_send(cfg, send: bool):
    """SEND a real test page through the configured sink — the only way to know
    the pager works is to page. Skipped (advisory) when alerts are off/unwired."""
    a = cfg.alerts
    if not a.enabled:
        return True, "Alerts OFF (optional) — set ALERTS_ENABLED=on to get paged."
    has_sink = bool(a.webhook_url) or bool(
        a.smtp_host and a.smtp_user and a.smtp_password and a.email_to
    )
    if not has_sink:
        return True, ("Alerts ON but no sink configured — will only log. "
                      "Set ALERT_EMAIL_TO or ALERT_WEBHOOK_URL.")
    if not send:
        return True, "Alert sink configured (test send skipped: --no-alert-test)."
    try:
        from .notify import Alerter
        Alerter(a).critical(
            "preflight-test", "Preflight test alert",
            "This is a TEST page from `python -m investment_strategy.preflight`. "
            "If you are reading it, the alert path works.",
        )
        return True, ("Test alert SENT — confirm it arrived (inbox/webhook). "
                      "No arrival = broken pager, fix before market open.")
    except Exception as e:
        return False, f"Test alert send FAILED: {e}"


def main() -> int:
    send_test = "--no-alert-test" not in sys.argv[1:]
    print("Preflight — checking the bot is ready to run…\n")
    ok, msg, cfg = _check_config()
    print(f"  {'✅' if ok else '❌'} {msg}")
    if not ok:
        print("\n❌ Fix your .env, then re-run this check.")
        return 1

    critical_ok = True
    for check in (_check_alpaca, _check_anthropic, _check_quiver,
                  _check_regime_feed, _check_robinhood, _check_options):
        ok, msg = check(cfg)
        critical_ok = critical_ok and ok
        print(f"  {'✅' if ok else '❌'} {msg}")
    ok, msg = _check_alert_send(cfg, send_test)
    critical_ok = critical_ok and ok
    print(f"  {'✅' if ok else '❌'} {msg}")

    if critical_ok:
        print("\n✅ Ready to run:  python -m investment_strategy")
        return 0
    print("\n❌ Not ready — fix the ❌ item(s) above, then re-run this check.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
