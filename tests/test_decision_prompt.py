"""Tests for the decision-prompt renderer's full-book ROTATION guidance.

Postmortem 2026-07-14: with the book at 15/15, MU (conviction 0.63) was
rejected at the slot cap while CVX (0.46) sat held — the model never tried
pairing a SELL with the BUY because nothing told it rotation was possible.
The prompt now says so exactly when the cap would actually reject.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_decision_prompt.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import investment_strategy.decision.engine as engine_mod
from investment_strategy.decision.engine import DecisionEngine
from investment_strategy.models import (
    AccountSnapshot,
    Position,
    Signal,
    SignalBundle,
    SignalKind,
)


def _engine(max_open_positions=2, options_enabled=False) -> DecisionEngine:
    eng = DecisionEngine.__new__(DecisionEngine)  # skip API client construction
    eng.cfg = SimpleNamespace(risk=SimpleNamespace(
        options_enabled=options_enabled,
        max_open_positions=max_open_positions,
        # Option-gate fields the DTE-window render reads when options are on.
        min_option_dte=7.0, max_option_dte=60.0, max_option_premium_pct=1.0,
    ))
    return eng


def _pos(symbol, asset_class="us_equity") -> Position:
    return Position(
        symbol=symbol, qty=1.0, avg_entry_price=100.0, current_price=100.0,
        market_value=100.0, unrealized_pl=0.0, unrealized_pl_pct=0.0,
        asset_class=asset_class,
    )


def _acct(positions) -> AccountSnapshot:
    return AccountSnapshot(
        equity=100_000, last_equity=100_000, cash=1_000, buying_power=1_000,
        positions=positions,
    )


def _bundle(symbol) -> SignalBundle:
    sig = Signal(kind=SignalKind.CONGRESS, symbol=symbol, summary="x", score=0.5)
    return SignalBundle(symbol=symbol, signals=[sig])


def test_rotation_block_renders_when_book_full():
    eng = _engine(max_open_positions=2)
    acct = _acct([_pos("CVX"), _pos("AAPL")])
    text = eng._render_dynamic([_bundle("MU")], acct, "", [])
    assert "Book FULL (2/2 equity slots)" in text
    assert "ROTATION" in text
    # Trusted guidance must sit OUTSIDE the untrusted region.
    assert text.index("Book FULL") < text.index("<market_data>")


def test_rotation_block_absent_when_book_has_room():
    eng = _engine(max_open_positions=3)
    acct = _acct([_pos("CVX"), _pos("AAPL")])
    text = eng._render_dynamic([_bundle("MU")], acct, "", [])
    assert "Book FULL" not in text
    assert "ROTATION" not in text


def test_rotation_block_ignores_option_rows():
    # Options have their own concurrency cap and don't consume equity slots —
    # the prompt must count the way the risk gate counts (equity rows only).
    eng = _engine(max_open_positions=2)
    acct = _acct([
        _pos("CVX"),
        _pos("AAPL260821C00200000", asset_class="us_option"),
    ])
    text = eng._render_dynamic([_bundle("MU")], acct, "", [])
    assert "Book FULL" not in text


def test_rotation_block_survives_missing_risk_config():
    # Engines built without a risk config (some tests, tooling) must not crash.
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = SimpleNamespace()
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic([_bundle("MU")], acct, "", [])
    assert "Book FULL" not in text


def test_held_note_renders_entry_conviction_and_age():
    # The incumbent baseline a rotation must beat (postmortem 2026-07-14: the
    # CVX 0.46 vs MU 0.63 gap was visible only in our journal, never to the
    # model).
    eng = _engine(max_open_positions=5)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic(
        [_bundle("CVX")], acct, "", [],
        held_notes={"CVX": "entry conviction 0.46, held 1.2d"},
    )
    assert "(HELD: 1 sh, +0.0%, entry conviction 0.46, held 1.2d)" in text


def test_held_tag_unchanged_without_note():
    eng = _engine(max_open_positions=5)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic([_bundle("CVX")], acct, "", [])
    assert "(HELD: 1 sh, +0.0%)" in text


def test_data_health_note_renders_in_account_block():
    # RH dead-auth used to silently DROP the external-holdings line; the model
    # couldn't tell an outage from "RH holds nothing". The note must render.
    eng = _engine(max_open_positions=5)
    acct = _acct([])
    note = "Robinhood data unavailable (OAuth expired): external holdings missing."
    text = eng._render_dynamic([_bundle("CVX")], acct, "", [], data_health=[note])
    assert f"DATA HEALTH: {note}" in text
    # And absent when not passed — no phantom outage banner.
    clean = eng._render_dynamic([_bundle("CVX")], acct, "", [])
    assert "DATA HEALTH" not in clean


def test_composite_anchor_renders_under_candidate_header():
    eng = _engine(max_open_positions=5)
    acct = _acct([])
    text = eng._render_dynamic(
        [_bundle("CVX")], acct, "", [], composites={"CVX": 0.42},
    )
    lines = text.splitlines()
    idx = lines.index("### CVX")
    assert lines[idx + 1].startswith("Composite signal index: +0.42")
    # No composite for the symbol -> no anchor line.
    clean = eng._render_dynamic([_bundle("CVX")], acct, "", [], composites={})
    assert "Composite signal index" not in clean


def test_system_prompt_carries_new_rules():
    from investment_strategy.decision.prompts import SYSTEM_PROMPT
    assert "CHASING" in SYSTEM_PROMPT
    assert "Composite signal index" in SYSTEM_PROMPT


# -- risk-off downside mandate (defined-risk puts when the market turns down) -- #
def test_downside_block_renders_in_riskoff_with_options():
    eng = _engine(options_enabled=True)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic(
        [_bundle("MU")], acct, "", [],
        regime_label="risk-off",
        regime_reason="SPY below 200dma (690 vs 700), VIX 32 -> risk-off, size x0.40.",
    )
    assert "MARKET IS RISK-OFF" in text
    assert "long_put or bear_put_spread" in text
    # Trusted guidance must sit OUTSIDE the untrusted region.
    assert text.index("MARKET IS RISK-OFF") < text.index("<market_data>")


def test_downside_block_absent_when_risk_on():
    eng = _engine(options_enabled=True)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic([_bundle("MU")], acct, "", [], regime_label="risk-on")
    assert "MARKET IS RISK-OFF" not in text


def test_downside_block_absent_when_options_off():
    # No point steering to puts the risk gate would reject (options disabled).
    eng = _engine(options_enabled=False)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic([_bundle("MU")], acct, "", [], regime_label="risk-off")
    assert "MARKET IS RISK-OFF" not in text


# -- prompt caching: stable/dynamic split + cache_control (Jul 22 upgrade #2) -- #

def _decide_engine(options_enabled=False) -> DecisionEngine:
    eng = DecisionEngine.__new__(DecisionEngine)  # skip API client construction
    eng.cfg = SimpleNamespace(
        decision_effort="medium",
        risk=SimpleNamespace(
            options_enabled=options_enabled, max_open_positions=5,
            min_option_dte=7.0, max_option_dte=60.0, max_option_premium_pct=1.0,
            min_new_name_conviction=0.5, min_conviction=0.2,
        ),
    )
    eng.model = "claude-opus-4-8"
    return eng


def _mock_response(payload='{"proposals": []}'):
    block = MagicMock()
    block.type, block.text = "text", payload
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "end_turn"
    resp.usage = SimpleNamespace(
        input_tokens=100, output_tokens=10,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    return resp


def _call_decide(eng, **kwargs):
    eng.client = MagicMock()
    eng.client.messages.create.return_value = _mock_response()
    acct = _acct(kwargs.pop("positions", []))
    bundles = kwargs.pop("bundles", [_bundle("CVX")])
    with patch.object(engine_mod, "record_usage"):
        eng.decide(bundles, acct, **kwargs)
    return eng.client.messages.create.call_args.kwargs


def test_decide_sends_two_block_user_content_with_one_cache_breakpoint():
    eng = _decide_engine()
    kwargs = _call_decide(eng, lessons="ATTR-LESSON", curated="CURATED-LESSON")
    assert isinstance(kwargs["system"], str)   # system stays a plain string — no breakpoint there
    content = kwargs["messages"][0]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in content[1]


def test_stable_block_has_curated_and_risk_contract_not_dynamic_content():
    eng = _decide_engine()
    kwargs = _call_decide(
        eng, lessons="## Track record\nATTR-LESSON-MARKER",
        curated="## Operating lessons (from your own nightly post-mortems — trusted)\nCURATED-MARKER",
        today="## Today so far (your own actions this trading day — trusted, not market data)",
    )
    stable, dynamic = kwargs["messages"][0]["content"]
    stable_text, dynamic_text = stable["text"], dynamic["text"]
    assert "Today's date:" in stable_text
    assert "CURATED-MARKER" in stable_text
    assert "## Risk contract" in stable_text
    assert "<market_data>" not in stable_text
    assert "ATTR-LESSON-MARKER" not in stable_text
    assert "Today so far" not in stable_text


def test_dynamic_block_carries_everything_else():
    eng = _decide_engine(options_enabled=True)
    kwargs = _call_decide(
        eng, lessons="## Track record\nATTR-LESSON-MARKER",
        curated="CURATED-MARKER",
        today="## Today so far (your own actions this trading day — trusted, not market data)",
        buy_excluded={"AMC": "price $2.20 < $5 liquidity floor"},
        regime_label="risk-off", regime_reason="SPY below 200dma.",
        positions=[_pos("CVX") for _ in range(5)],
    )
    stable, dynamic = kwargs["messages"][0]["content"]
    dynamic_text = dynamic["text"]
    assert "ATTR-LESSON-MARKER" in dynamic_text
    assert "Today so far" in dynamic_text
    assert "Buys excluded this cycle" in dynamic_text
    assert "MARKET IS RISK-OFF" in dynamic_text
    assert "<market_data>" in dynamic_text
    assert "Return proposals for the candidates" in dynamic_text
    # Nothing from the stable block leaks in twice.
    assert "## Risk contract" not in dynamic_text
    assert "CURATED-MARKER" not in dynamic_text


def test_stable_block_deterministic_across_differing_dynamic_inputs():
    """The cache breakpoint only pays off if the stable block is byte-identical
    across a day's cycles — if per-cycle data leaked in, every cycle would be a
    silent cache miss (a write, never a read)."""
    eng = _decide_engine()
    kwargs1 = _call_decide(
        eng, lessons="LESSON-CALL-1", curated="SAME-CURATED",
        today="Today block call 1", bundles=[_bundle("CVX")],
    )
    kwargs2 = _call_decide(
        eng, lessons="LESSON-CALL-2 (totally different)", curated="SAME-CURATED",
        today="Today block call 2, much longer with different content",
        bundles=[_bundle("AAPL"), _bundle("MSFT")],
    )
    stable1 = kwargs1["messages"][0]["content"][0]["text"]
    stable2 = kwargs2["messages"][0]["content"][0]["text"]
    assert stable1 == stable2


def test_decide_still_returns_proposals_from_dynamic_content():
    # End-to-end sanity: the split didn't break response parsing.
    from investment_strategy.models import TradeProposal
    eng = _decide_engine()
    eng.client = MagicMock()
    eng.client.messages.create.return_value = _mock_response(
        '{"proposals": [{"symbol": "CVX", "action": "buy", "instrument": "equity", '
        '"conviction": 0.7, "target_weight_pct": 3.0, "rationale": "x"}]}'
    )
    acct = _acct([])
    with patch.object(engine_mod, "record_usage"):
        proposals = eng.decide([_bundle("CVX")], acct)
    assert len(proposals) == 1
    assert isinstance(proposals[0], TradeProposal)
    assert proposals[0].symbol == "CVX"


# -- market regime line + option direction discipline (calls up / puts down) -- #
def test_regime_line_renders_every_cycle_with_direction_rule_up():
    eng = _engine(options_enabled=True)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic(
        [_bundle("MU")], acct, "", [],
        regime_label="risk-on",
        regime_reason="SPY above 200dma (700 vs 650), VIX 15 -> risk-on, size x1.00.",
        regime_trend="up",
    )
    assert "## Market regime: risk-on" in text
    assert "SPY above 200dma" in text
    assert "CALL structures" in text and "auto-rejected" in text
    # Trusted guidance sits OUTSIDE the untrusted region.
    assert text.index("## Market regime") < text.index("<market_data>")


def test_regime_line_direction_rule_down():
    eng = _engine(options_enabled=True)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic(
        [_bundle("MU")], acct, "", [],
        regime_label="neutral",
        regime_reason="SPY below 200dma (620 vs 650), VIX 15 -> neutral, size x0.50.",
        regime_trend="down",
    )
    assert "## Market regime: neutral" in text
    assert "PUT structures" in text
    assert "CALL structures are auto-rejected" in text.replace(
        "Bullish CALL structures are auto-rejected", "CALL structures are auto-rejected")


def test_regime_line_present_without_direction_rule_when_options_off():
    eng = _engine(options_enabled=False)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic(
        [_bundle("MU")], acct, "", [],
        regime_label="risk-on", regime_reason="reason here", regime_trend="up",
    )
    assert "## Market regime: risk-on" in text
    assert "CALL structures" not in text


def test_regime_line_absent_when_filter_disabled():
    # Filter off => orchestrator passes empty label; nothing renders.
    eng = _engine(options_enabled=True)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic([_bundle("MU")], acct, "", [])
    assert "## Market regime" not in text


def test_riskoff_mandate_still_renders_alongside_regime_line():
    eng = _engine(options_enabled=True)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic(
        [_bundle("MU")], acct, "", [],
        regime_label="risk-off",
        regime_reason="SPY below 200dma (620 vs 650), VIX 32 -> risk-off, size x0.40.",
        regime_trend="down",
    )
    assert "## Market regime: risk-off" in text
    assert "MARKET IS RISK-OFF" in text
    assert "long_put or bear_put_spread" in text


def test_system_prompt_carries_option_direction_rule():
    from investment_strategy.decision.prompts import SYSTEM_PROMPT
    assert "OPTION DIRECTION" in SYSTEM_PROMPT
    assert "LONG-RUN market trend" in SYSTEM_PROMPT
    assert "AUTO-REJECTS" in SYSTEM_PROMPT


def test_trend_up_call_line_suppressed_in_riskoff_vol_spike():
    # Vol-spiked uptrend (trend=up + label=risk-off): the risk-off mandate
    # carries the cycle's put instruction — rendering the trend-up "don't
    # propose puts" line beside "PROPOSE a long_put" would contradict it.
    eng = _engine(options_enabled=True)
    acct = _acct([_pos("CVX")])
    text = eng._render_dynamic(
        [_bundle("MU")], acct, "", [],
        regime_label="risk-off",
        regime_reason="SPY above 200dma (700 vs 650), VIX 34 -> risk-off, size x0.40.",
        regime_trend="up",
    )
    assert "MARKET IS RISK-OFF" in text
    assert "long_put or bear_put_spread" in text
    assert "don't spend conviction on puts" not in text
    assert "CALL structures (long_call / bull_call_spread)" not in text


# -- same-cycle option fallback (Jul 28) ------------------------------------- #

_FALLBACK_CALL = (
    '{"proposals": [{"symbol": "FIRY", "action": "buy", "instrument": "option", '
    '"conviction": 0.65, "target_weight_pct": 0, "stop_loss_pct": null, '
    '"take_profit_pct": null, "rationale": "call fallback", "key_signals": [], '
    '"option_strategy": "long_call", "option_legs": [{"expiry": "2026-08-21", '
    '"strike": 30, "right": "call", "side": "buy", "ratio": 1}], '
    '"max_premium_usd": 5000}]}'
)


def _call_fallback(eng, payload, symbol="FIRY", **kwargs):
    eng.client = MagicMock()
    eng.client.messages.create.return_value = _mock_response(payload)
    with patch.object(engine_mod, "record_usage"):
        return eng.decide_option_fallback(
            _bundle(symbol), _acct([]), 0.60,
            "Overextended: RSI 73 and 3.4xATR above the 20d SMA", **kwargs,
        )


def test_option_fallback_returns_call_structure():
    eng = _decide_engine(options_enabled=True)
    p = _call_fallback(eng, _FALLBACK_CALL)
    assert p is not None
    assert p.symbol == "FIRY"
    assert p.option_strategy.value == "long_call"


def test_option_fallback_none_on_hold():
    eng = _decide_engine(options_enabled=True)
    hold = (
        '{"proposals": [{"symbol": "FIRY", "action": "hold", "instrument": "equity", '
        '"conviction": 0.5, "target_weight_pct": 0, "stop_loss_pct": null, '
        '"take_profit_pct": null, "rationale": "not enough edge", "key_signals": [], '
        '"option_strategy": null, "option_legs": [], "max_premium_usd": null}]}'
    )
    assert _call_fallback(eng, hold) is None


def test_option_fallback_rejects_equity_and_put_pivots():
    """The fallback is bullish-and-option-only: an equity re-propose would die
    at the same gate, and a put contradicts the (bullish) rejected thesis."""
    eng = _decide_engine(options_enabled=True)
    equity = _FALLBACK_CALL.replace('"instrument": "option"', '"instrument": "equity"')
    assert _call_fallback(eng, equity) is None
    put = _FALLBACK_CALL.replace('"long_call"', '"long_put"').replace(
        '"right": "call"', '"right": "put"')
    assert _call_fallback(eng, put) is None


def test_option_fallback_prompt_scoped_and_cache_reuses_stable_block():
    eng = _decide_engine(options_enabled=True)
    _call_fallback(eng, _FALLBACK_CALL, curated="SAME-CURATED")
    fb_kwargs = eng.client.messages.create.call_args.kwargs
    stable, dynamic = fb_kwargs["messages"][0]["content"]
    assert stable["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "OPTION FALLBACK" in dynamic["text"]
    assert "Overextended: RSI 73" in dynamic["text"]
    assert "long_call or bull_call_spread" in dynamic["text"]
    # Byte-identical stable block vs the main decide() call -> cache READ.
    main_kwargs = _call_decide(eng, curated="SAME-CURATED")
    assert stable["text"] == main_kwargs["messages"][0]["content"][0]["text"]
