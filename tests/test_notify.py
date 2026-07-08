"""Tests for the CRITICAL alerter (notify.py) and its watchdog wiring.

Pure logic, no network: we stub the email/webhook sinks and assert on throttling,
fail-open behavior, sink dispatch, and that watchdog CRITICAL paths actually page.

Runnable two ways:
    .venv/bin/python tests/test_notify.py     # standalone, no pytest
    .venv/bin/pytest tests/                    # if pytest is installed
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import AccountSnapshot, Position
from investment_strategy.monitor.watchdog import Watchdog
from investment_strategy.notify import AlertConfig, Alerter, load_alert_config
from investment_strategy.state import PortfolioState


def _alert_cfg(enabled=True, email_to="me@example.com", webhook_url="",
               cooldown_s=900.0) -> AlertConfig:
    return AlertConfig(
        enabled=enabled,
        smtp_host="smtp.gmail.com", smtp_port=587,
        smtp_user="bot@example.com", smtp_password="pw",
        email_to=email_to, webhook_url=webhook_url, cooldown_s=cooldown_s,
    )


class _RecordingAlerter(Alerter):
    """Alerter with the real throttle/dispatch logic but stubbed sinks."""
    def __init__(self, cfg, email_ok=True, webhook_ok=True):
        super().__init__(cfg)
        self.emails: list[tuple[str, str]] = []
        self.webhooks: list[tuple[str, str]] = []
        self._email_ok = email_ok
        self._webhook_ok = webhook_ok

    def _send_email(self, subject, body):
        if self._email_ok:
            self.emails.append((subject, body))
        return self._email_ok

    def _send_webhook(self, subject, body):
        if self._webhook_ok:
            self.webhooks.append((subject, body))
        return self._webhook_ok


# -- Alerter unit behavior --------------------------------------------------- #

def test_disabled_never_sends():
    a = _RecordingAlerter(_alert_cfg(enabled=False))
    a.critical("k", "subj", "body")
    assert a.emails == [] and a.webhooks == []


def test_email_sink_dispatch():
    a = _RecordingAlerter(_alert_cfg())
    a.critical("k", "subj", "body")
    assert a.emails == [("subj", "body")]
    assert a.webhooks == []          # no webhook configured


def test_webhook_sink_dispatch():
    a = _RecordingAlerter(_alert_cfg(email_to="", webhook_url="https://hook"))
    a.critical("k", "subj", "body")
    assert a.webhooks == [("subj", "body")]
    assert a.emails == []            # email not configured => not attempted


def test_throttle_dedupes_same_key():
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0))
    a.critical("same", "s1", "b1")
    a.critical("same", "s2", "b2")   # within cooldown -> suppressed
    assert len(a.emails) == 1
    a.critical("other", "s3", "b3")  # different key -> sends
    assert len(a.emails) == 2


def test_throttle_resends_after_cooldown():
    a = _RecordingAlerter(_alert_cfg(cooldown_s=0.0))  # no cooldown
    a.critical("k", "s", "b")
    a.critical("k", "s", "b")
    assert len(a.emails) == 2


def test_failed_sink_does_not_latch_throttle():
    # If every sink fails, we must NOT record it as sent, so the next tick retries.
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0), email_ok=False)
    a.critical("k", "s", "b")        # email fails, nothing recorded as sent
    a._email_ok = True               # sink recovers
    a.critical("k", "s", "b")        # retry should now go through despite cooldown
    assert a.emails == [("s", "b")]


def test_partial_sink_success_still_throttles():
    # Webhook works, email fails -> overall delivered, so throttle DOES engage.
    a = _RecordingAlerter(
        _alert_cfg(webhook_url="https://hook", cooldown_s=900.0),
        email_ok=False, webhook_ok=True,
    )
    a.critical("k", "s", "b")
    a.critical("k", "s", "b")        # suppressed: previous delivery succeeded
    assert len(a.webhooks) == 1


def test_load_alert_config_from_env():
    env = {
        "ALERTS_ENABLED": "on",
        "ALERT_EMAIL_TO": "you@example.com",
        "ALERT_WEBHOOK_URL": "https://hook ",
        "ALERT_COOLDOWN_SECONDS": "60",
    }
    cfg = load_alert_config(lambda k, d=None: env.get(k, d if d is not None else ""))
    assert cfg.enabled is True
    assert cfg.email_to == "you@example.com"
    assert cfg.webhook_url == "https://hook"      # trimmed
    assert cfg.cooldown_s == 60.0
    assert cfg.smtp_host == "smtp.gmail.com"      # default


# -- watchdog wiring: CRITICAL paths must page ------------------------------- #

class _FailingBroker:
    """Every exit path fails (market close AND the marketable-limit fallback) =>
    the genuinely-naked position path that must page (1B.5)."""
    def cancel_open_orders_for(self, symbol):
        pass

    def close_position(self, symbol):
        return None

    def latest_price(self, symbol):
        return 50.0

    def close_position_marketable_limit(self, symbol, qty, ref_price):
        return None                      # fallback also fails => truly naked

    confirm_equity = 1000.0   # what the floor-breach confirming re-read reports

    def get_account(self):
        return _acct(self.confirm_equity)


def _pos(symbol="AAPL", pl_pct=-50.0):
    return Position(symbol=symbol, qty=10.0, avg_entry_price=100.0,
                    current_price=50.0, market_value=500.0,
                    unrealized_pl=-500.0, unrealized_pl_pct=pl_pct)


def _acct(equity, positions=None):
    return AccountSnapshot(equity=equity, last_equity=equity, cash=0.0,
                           buying_power=0.0,
                           positions=positions if positions is not None
                           else [_pos("AAPL")])


def _cfg():
    return SimpleNamespace(
        risk=SimpleNamespace(equity_floor_pct=60.0, max_daily_loss_pct=3.0),
        state_file="state/risk_state.json", monitor_interval_s=30,
    )


def _state() -> PortfolioState:
    p = os.path.join(tempfile.gettempdir(), f"_nf_{uuid.uuid4().hex}.json")
    return PortfolioState(path=p)


def test_failed_flatten_pages():
    state = _state()
    state.peak_equity = 1000.0
    a = _RecordingAlerter(_alert_cfg())
    wd = Watchdog(_cfg(), _FailingBroker(), state=state, alerter=a)
    wd._flatten_all(_acct(500.0), "EQUITY FLOOR")
    assert len(a.emails) == 1
    assert "NAKED" in a.emails[0][0] and "AAPL" in a.emails[0][0]


def test_equity_floor_halt_pages():
    state = _state()
    state.peak_equity = 1000.0        # floor $600
    a = _RecordingAlerter(_alert_cfg())
    broker = _FailingBroker()
    broker.confirm_equity = 500.0     # re-read agrees: genuinely below the floor
    wd = Watchdog(_cfg(), broker, state=state, alerter=a)
    assert wd._equity_floor_breached(_acct(500.0)) is True
    subjects = [s for s, _ in a.emails]
    assert any("EQUITY FLOOR" in s and "HALTED" in s for s in subjects)


def test_no_alerter_is_safe():
    # A watchdog with no alerter must not raise on a CRITICAL path.
    state = _state()
    wd = Watchdog(_cfg(), _FailingBroker(), state=state, alerter=None)
    wd._flatten_all(_acct(500.0), "DAILY LOSS")   # should not raise


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
