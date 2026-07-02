"""Preflight readiness check — does everything the bot needs ACTUALLY work?

    python -m investment_strategy.preflight

Importing load_config only checks that keys are PRESENT, not that they WORK — so a
wrong / rotated / mis-pasted key still prints "ready" and then 401-loops once the
bot starts. This command goes one step further and actually talks to Alpaca, so a
bad key fails HERE with a plain-English message instead of a stack trace at runtime.

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


def _check_alerts(cfg):
    """Advisory: are CRITICAL alerts wired? Never fatal."""
    a = cfg.alerts
    if not a.enabled:
        return True, "Alerts OFF (optional) — set ALERTS_ENABLED=on to get paged."
    sinks = []
    if a.smtp_host and a.smtp_user and a.smtp_password and a.email_to:
        sinks.append(f"email→{a.email_to}")
    if a.webhook_url:
        sinks.append("webhook")
    if not sinks:
        return True, "Alerts ON but no sink configured — will only log. Set ALERT_EMAIL_TO or ALERT_WEBHOOK_URL."
    return True, f"Alerts ON via {', '.join(sinks)}."


def main() -> int:
    print("Preflight — checking the bot is ready to run…\n")
    ok, msg, cfg = _check_config()
    print(f"  {'✅' if ok else '❌'} {msg}")
    if not ok:
        print("\n❌ Fix your .env, then re-run this check.")
        return 1

    critical_ok = True
    for check in (_check_alpaca, _check_anthropic):
        ok, msg = check(cfg)
        critical_ok = critical_ok and ok
        print(f"  {'✅' if ok else '❌'} {msg}")
    # Advisory (never blocks readiness)
    _, msg = _check_alerts(cfg)
    print(f"  ℹ️  {msg}")

    if critical_ok:
        print("\n✅ Ready to run:  python -m investment_strategy")
        return 0
    print("\n❌ Not ready — fix the ❌ item(s) above, then re-run this check.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
