"""Tests for the preflight readiness check (offline pieces).

The Alpaca/Quiver/yfinance/Robinhood checks need the network, so we test only
the pure/offline behavior here: the Anthropic key format check, the alert-send
check's gating (off / no sink / skipped), and that each breadth check (GA-2.7)
correctly SKIPS — instead of failing — when its feature isn't configured.

Runnable two ways:
    .venv/bin/python tests/test_preflight.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.preflight import (
    _check_alert_send,
    _check_anthropic,
    _check_quiver,
    _check_regime_feed,
    _check_robinhood,
)


def _cfg(anthropic_key="sk-ant-abc", alerts=None, quiver_key="",
         regime_on=False, robinhood_on=False):
    return SimpleNamespace(
        anthropic_api_key=anthropic_key,
        quiver_api_key=quiver_key,
        robinhood_enabled=robinhood_on,
        risk=SimpleNamespace(
            regime_filter_enabled=regime_on, regime_degraded_mult=0.5,
        ),
        alerts=alerts or SimpleNamespace(
            enabled=False, smtp_host="", smtp_user="", smtp_password="",
            email_to="", webhook_url="",
        ),
    )


def test_anthropic_key_wellformed_passes():
    ok, msg = _check_anthropic(_cfg(anthropic_key="sk-ant-api03-xyz"))
    assert ok and "well-formed" in msg


def test_anthropic_key_missing_fails():
    ok, msg = _check_anthropic(_cfg(anthropic_key=""))
    assert not ok and "missing" in msg.lower()


def test_anthropic_key_wrong_shape_fails():
    ok, msg = _check_anthropic(_cfg(anthropic_key="oops-not-a-claude-key"))
    assert not ok and "sk-ant-" in msg


def test_alerts_off_is_ok_advisory():
    ok, msg = _check_alert_send(_cfg(), send=True)
    assert ok and "OFF" in msg


def test_alerts_on_without_sink_warns_but_passes():
    alerts = SimpleNamespace(
        enabled=True, smtp_host="", smtp_user="", smtp_password="",
        email_to="", webhook_url="",
    )
    ok, msg = _check_alert_send(_cfg(alerts=alerts), send=True)
    assert ok and "no sink" in msg.lower()


def test_alert_test_send_can_be_skipped():
    alerts = SimpleNamespace(
        enabled=True, smtp_host="smtp.gmail.com", smtp_user="a@b.com",
        smtp_password="pw", email_to="me@x.com", webhook_url="",
    )
    ok, msg = _check_alert_send(_cfg(alerts=alerts), send=False)
    assert ok and "skipped" in msg.lower()


# -- GA-2.7 breadth checks: each SKIPS (passes) when its feature is off ------ #
def test_quiver_skipped_without_key():
    ok, msg = _check_quiver(_cfg(quiver_key=""))
    assert ok and "not set" in msg


def test_regime_check_skipped_when_filter_off():
    ok, msg = _check_regime_feed(_cfg(regime_on=False))
    assert ok and "skipped" in msg.lower()


def test_robinhood_skipped_when_disabled():
    ok, msg = _check_robinhood(_cfg(robinhood_on=False))
    assert ok and "off" in msg.lower()


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
