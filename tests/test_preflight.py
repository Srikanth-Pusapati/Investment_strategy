"""Tests for the preflight readiness check (offline pieces).

The Alpaca auth check needs the network, so we test only the pure/offline checks
here: the Anthropic key format check and the alert-config advisory.

Runnable two ways:
    .venv/bin/python tests/test_preflight.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.preflight import _check_alerts, _check_anthropic


def _cfg(anthropic_key="sk-ant-abc", alerts=None):
    return SimpleNamespace(
        anthropic_api_key=anthropic_key,
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
    ok, msg = _check_alerts(_cfg())
    assert ok and "OFF" in msg


def test_alerts_on_with_email_reports_sink():
    alerts = SimpleNamespace(
        enabled=True, smtp_host="smtp.gmail.com", smtp_user="a@b.com",
        smtp_password="pw", email_to="me@x.com", webhook_url="",
    )
    ok, msg = _check_alerts(_cfg(alerts=alerts))
    assert ok and "email→me@x.com" in msg


def test_alerts_on_without_sink_warns():
    alerts = SimpleNamespace(
        enabled=True, smtp_host="", smtp_user="", smtp_password="",
        email_to="", webhook_url="",
    )
    ok, msg = _check_alerts(_cfg(alerts=alerts))
    assert ok and "no sink" in msg.lower()


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
