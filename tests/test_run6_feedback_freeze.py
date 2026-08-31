"""Run-6 item 6 — freeze the self-referential feedback loops.

Knobs (all run-6 defaults): COMPOSITE_PERF_WEIGHTS off, EXPECTANCY_GATE_ENABLED
off (compute + 'would have armed' log, never reject), TRACK_RECORD_MIN_TRIPS 20,
CURATED_LESSONS_INJECT off (+ [SUPERSEDED] filter when on). Autotune stays
report-only.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import investment_strategy.autotune as autotune_mod

_REQ = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ANTHROPIC_API_KEY": "a"}


def _env_without(*keys) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in keys}
    env.update(_REQ)
    return env
import investment_strategy.postmortem as pm_mod
from datetime import datetime, timedelta, timezone

from investment_strategy.attribution import SourceStats, render_lessons
from investment_strategy.ledger import TradeLedger, TradeRecord
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.postmortem import read_curated
from investment_strategy.signals.composite import perf_weights


# ---------------------------------------------------------------- helpers
_T0 = datetime(2026, 8, 10, 14, tzinfo=timezone.utc)


def _ledger_with_trips(n_trips: int, symbol_prefix: str = "AB") -> TradeLedger:
    d = Path(tempfile.mkdtemp())
    led = TradeLedger(d / "ledger.jsonl")
    for i in range(n_trips):
        sym = f"{symbol_prefix}{i}"
        led.record(TradeRecord(
            symbol=sym, action="buy", qty=10, entry_signals=["congress"],
            key_signals=["congress trade +0.8"], ts=_T0 + timedelta(hours=2 * i),
        ))
        led.record(TradeRecord(
            symbol=sym, action="sell", qty=10, realized_pl_pct=-2.0,
            exit_reason="decision", ts=_T0 + timedelta(hours=2 * i + 1),
        ))
    return led


# ---------------------------------------------------------------- (a) perf weights
def test_perf_weights_knob_off_returns_empty_and_on_computes():
    led = _ledger_with_trips(5)
    assert perf_weights(led, min_trips=3, enabled=False) == {}
    on = perf_weights(led, min_trips=3, enabled=True)
    # Something is computed when enabled (cited congress trips are losers).
    assert isinstance(on, dict)


def test_composite_perf_weights_default_off_in_config():
    env = _env_without("COMPOSITE_PERF_WEIGHTS")
    with patch.dict(os.environ, env, clear=True):
        from investment_strategy.config import load_config
        cfg = load_config()
    assert cfg.risk.composite_perf_weights is False
    with patch.dict(os.environ, {**env, "COMPOSITE_PERF_WEIGHTS": "on"}, clear=True):
        from investment_strategy.config import load_config
        assert load_config().risk.composite_perf_weights is True


# ---------------------------------------------------------------- (b) expectancy gate
def _fake_orch(gate_on: bool) -> SimpleNamespace:
    risk = SimpleNamespace(
        expectancy_gate_enabled=gate_on, expectancy_gate_window_days=14,
        expectancy_gate_min_trips=1,
    )
    return SimpleNamespace(cfg=SimpleNamespace(risk=risk), ledger=object())


_NEG = {"congress": SourceStats(source="congress", trips=4, wins=0,
                                pl_pcts=[-3.2] * 4)}


def test_expectancy_gate_off_logs_would_have_armed_and_returns_empty(caplog):
    orch = _fake_orch(gate_on=False)
    with patch("investment_strategy.orchestrator.negative_expectancy_families",
               return_value=_NEG), caplog.at_level(logging.INFO, logger="orchestrator"):
        out = Orchestrator._expectancy_gate_read(orch)
    assert out == {}
    msgs = [r.getMessage() for r in caplog.records]
    assert any("would have armed against: congress -3.2%/trip x4" in m for m in msgs)
    assert not any(m.startswith("Expectancy gate armed") for m in msgs)


def test_expectancy_gate_on_returns_set_and_logs_armed(caplog):
    orch = _fake_orch(gate_on=True)
    with patch("investment_strategy.orchestrator.negative_expectancy_families",
               return_value=_NEG), caplog.at_level(logging.INFO, logger="orchestrator"):
        out = Orchestrator._expectancy_gate_read(orch)
    assert out == _NEG
    assert any("Expectancy gate armed against: congress" in r.getMessage()
               for r in caplog.records)


def test_expectancy_gate_read_fails_closed_to_empty():
    orch = _fake_orch(gate_on=True)
    with patch("investment_strategy.orchestrator.negative_expectancy_families",
               side_effect=RuntimeError("boom")):
        assert Orchestrator._expectancy_gate_read(orch) == {}


def test_expectancy_gate_default_off_and_legacy_alias():
    from investment_strategy.config import load_config
    env = _env_without("EXPECTANCY_GATE", "EXPECTANCY_GATE_ENABLED")
    with patch.dict(os.environ, env, clear=True):
        assert load_config().risk.expectancy_gate_enabled is False
    with patch.dict(os.environ, {**env, "EXPECTANCY_GATE": "on"}, clear=True):
        assert load_config().risk.expectancy_gate_enabled is True
    with patch.dict(os.environ, {**env, "EXPECTANCY_GATE": "on",
                                 "EXPECTANCY_GATE_ENABLED": "off"}, clear=True):
        assert load_config().risk.expectancy_gate_enabled is False


# ---------------------------------------------------------------- (c) track record
def test_track_record_min_trips_suppresses_small_samples():
    led = _ledger_with_trips(5)
    assert "congress" in render_lessons(led, min_source_trips=2)
    assert render_lessons(led, min_source_trips=20) == ""


def test_track_record_min_trips_default_20_in_config():
    from investment_strategy.config import load_config
    env = _env_without("TRACK_RECORD_MIN_TRIPS")
    with patch.dict(os.environ, env, clear=True):
        assert load_config().track_record_min_trips == 20


def test_orchestrator_passes_track_record_min_trips():
    led = _ledger_with_trips(5)
    orch = SimpleNamespace(cfg=SimpleNamespace(track_record_min_trips=20), ledger=led)
    assert Orchestrator._attribution_lessons(orch) == ""
    orch.cfg.track_record_min_trips = 2
    assert "congress" in Orchestrator._attribution_lessons(orch)


# ---------------------------------------------------------------- (d) curated lessons
def test_curated_inject_knob_off_returns_empty_even_with_file():
    d = Path(tempfile.mkdtemp())
    f = d / "curated.md"
    f.write_text("Never chase.\n", encoding="utf-8")
    orch = SimpleNamespace(cfg=SimpleNamespace(
        curated_lessons_inject=False, postmortem_max_lessons=15))
    with patch.object(pm_mod, "_CURATED_FILE", f):
        assert Orchestrator._curated_lessons(orch) == ""
        orch.cfg.curated_lessons_inject = True
        assert "Never chase." in Orchestrator._curated_lessons(orch)


def test_read_curated_skips_superseded_lines():
    d = Path(tempfile.mkdtemp())
    f = d / "curated.md"
    f.write_text(
        "Keep stops.\n"
        "[SUPERSEDED 2026-08-26: QQQ is the system core] cap QQQ exposure.\n"
        "  [SUPERSEDED] indented one.\n"
        "Size starters small.\n", encoding="utf-8")
    with patch.object(pm_mod, "_CURATED_FILE", f):
        out = read_curated(15)
        assert "Keep stops." in out and "Size starters small." in out
        assert "SUPERSEDED" not in out and "cap QQQ" not in out
        # max_lines applies AFTER the filter (newest live lines).
        assert read_curated(1).splitlines()[-1] == "Size starters small."
        f.write_text("[SUPERSEDED] only.\n", encoding="utf-8")
        assert read_curated(15) == ""


def test_postmortem_still_writes_curated_when_inject_off():
    d = Path(tempfile.mkdtemp())
    f = d / "curated.md"
    with patch.object(pm_mod, "_LESSONS_DIR", d), patch.object(pm_mod, "_CURATED_FILE", f):
        pm_mod._append_curated(["a new lesson"])
    assert "a new lesson" in f.read_text()


def test_curated_inject_default_off_in_config():
    from investment_strategy.config import load_config
    env = _env_without("CURATED_LESSONS_INJECT")
    with patch.dict(os.environ, env, clear=True):
        assert load_config().curated_lessons_inject is False


# ---------------------------------------------------------------- (f) autotune report-only
def test_autotune_never_writes_knobs():
    src = Path(autotune_mod.__file__).read_text(encoding="utf-8")
    assert "set_key" not in src
    assert not re.search(r"os\.environ\[[^\]]+\]\s*=", src)
    assert "environ.update" not in src and "putenv" not in src
    # The only file write is the weekly markdown report.
    writes = re.findall(r"\.write_text\(|open\([^)]*['\"]w", src)
    assert writes == [".write_text("]
    assert '".env"' not in src.replace("`.env`", "")
