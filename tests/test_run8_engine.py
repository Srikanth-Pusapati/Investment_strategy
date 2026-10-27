"""Run-8 B-4: DecisionEngine.decide_bearish_fallback — the scoped follow-up
for an ELIGIBLE bearish name the main call returned no verdict for. Mirrors
the option-fallback tests in tests/test_decision_prompt.py (mocked client)."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_decision_prompt import _acct, _bundle, _decide_engine, _mock_response, engine_mod  # noqa: E402

_PUT = (
    '{"proposals": [{"symbol": "HYG", "action": "buy", "instrument": "option", '
    '"conviction": 0.65, "target_weight_pct": 0, "stop_loss_pct": null, '
    '"take_profit_pct": null, "rationale": "put fallback", "key_signals": [], '
    '"option_strategy": "long_put", "option_legs": [{"expiry": "2026-11-20", '
    '"strike": 78, "right": "put", "side": "buy", "ratio": 1}], '
    '"max_premium_usd": 4900}]}'
)
_CALL = _PUT.replace('"long_put"', '"long_call"').replace('"right": "put"', '"right": "call"')
_HOLD = (
    '{"proposals": [{"symbol": "HYG", "action": "hold", "instrument": "equity", '
    '"conviction": 0.3, "target_weight_pct": 0, "stop_loss_pct": null, '
    '"take_profit_pct": null, "rationale": "only flow is bearish", "key_signals": []}]}'
)


def _call(eng, payload, **kw):
    eng.client = MagicMock()
    eng.client.messages.create.return_value = _mock_response(payload)
    with patch.object(engine_mod, "record_usage"):
        out = eng.decide_bearish_fallback(
            _bundle("HYG"), _acct([]), "below its 200dma", price=78.4, **kw,
        )
    return out, eng.client.messages.create.call_args.kwargs


def test_bearish_fallback_returns_a_put_structure():
    eng = _decide_engine(options_enabled=True)
    p, kwargs = _call(eng, _PUT, max_premium_usd=4_900.0)
    assert p is not None and p.option_strategy.value == "long_put"
    assert all(l.right.lower().startswith("p") for l in p.option_legs)
    dynamic = kwargs["messages"][0]["content"][1]["text"]
    assert "## BEARISH FALLBACK" in dynamic
    assert "found HYG ELIGIBLE" in dynamic and "below its 200dma" in dynamic
    assert "long_put or bear_put_spread" in dynamic and "Do NOT propose equity or calls" in dynamic
    assert "max_premium_usd <= 4,900" in dynamic and "latest price $78.40" in dynamic


def test_bearish_fallback_refuses_calls_and_treats_hold_as_no_trade():
    eng = _decide_engine(options_enabled=True)
    assert _call(eng, _CALL)[0] is None
    assert _call(eng, _HOLD)[0] is None
    assert _call(eng, '{"proposals": []}')[0] is None


def test_bearish_fallback_fails_quiet_on_an_unusable_stop_reason():
    eng = _decide_engine(options_enabled=True)
    eng.client = MagicMock()
    resp = _mock_response(_PUT)
    resp.stop_reason = "max_tokens"
    eng.client.messages.create.return_value = resp
    with patch.object(engine_mod, "record_usage"):
        assert eng.decide_bearish_fallback(_bundle("HYG"), _acct([]), "x") is None
