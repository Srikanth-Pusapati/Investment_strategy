"""Tests for the dead-man watchdog (ops/deadman.py) and control panel helpers
(ops/control_panel.py). Actions that would touch the REAL bot (restart) are
not exercised here — only the pure/diagnostic logic and the kill-file toggle
against a temp path."""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import deadman
from ops import control_panel as panel

ET = ZoneInfo("America/New_York")


def _et(wd_offset_days: int, hour: int, minute: int = 0) -> dt.datetime:
    """A Monday (2026-07-13) + offset, at the given ET time."""
    base = dt.datetime(2026, 7, 13, hour, minute, tzinfo=ET)
    return base + dt.timedelta(days=wd_offset_days)


# -- deadman: market-hours window --------------------------------------------- #

def test_market_hours_open_window():
    assert deadman.market_hours(_et(0, 10, 0)) is True       # Mon 10:00
    assert deadman.market_hours(_et(0, 9, 25)) is True       # pre-open check
    assert deadman.market_hours(_et(0, 16, 5)) is True       # settle window


def test_market_hours_closed():
    assert deadman.market_hours(_et(0, 9, 20)) is False      # too early
    assert deadman.market_hours(_et(0, 16, 6)) is False      # after close
    assert deadman.market_hours(_et(5, 12, 0)) is False      # Saturday
    assert deadman.market_hours(_et(6, 12, 0)) is False      # Sunday


# -- deadman: liveness probes -------------------------------------------------- #

def test_bot_alive_rejects_none_and_foreign_pids():
    assert deadman.bot_alive(None) is False
    # Our own test process runs pytest, not `-m investment_strategy` — a
    # recycled pid must not read as a live bot.
    assert deadman.bot_alive(os.getpid()) is False


def test_log_age_minutes():
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        f.write(b"line\n")
        path = Path(f.name)
    age = deadman.log_age_minutes(log_file=path)
    assert age is not None and age < 1.0
    path.unlink()
    assert deadman.log_age_minutes(log_file=path) is None    # missing log


# -- deadman: diagnosis --------------------------------------------------------- #

def test_diagnose_dead_process(monkeypatch):
    monkeypatch.setattr(deadman, "bot_pid", lambda *a, **k: 1234)
    monkeypatch.setattr(deadman, "bot_alive", lambda pid: False)
    assert "NOT RUNNING" in deadman.diagnose()


def test_diagnose_wedged_process(monkeypatch):
    monkeypatch.setattr(deadman, "bot_pid", lambda *a, **k: 1234)
    monkeypatch.setattr(deadman, "bot_alive", lambda pid: True)
    monkeypatch.setattr(deadman, "log_age_minutes", lambda **k: 90.0)
    assert "WEDGED" in deadman.diagnose()


def test_diagnose_healthy(monkeypatch):
    monkeypatch.setattr(deadman, "bot_pid", lambda *a, **k: 1234)
    monkeypatch.setattr(deadman, "bot_alive", lambda pid: True)
    monkeypatch.setattr(deadman, "log_age_minutes", lambda **k: 3.0)
    assert deadman.diagnose() is None


# -- control panel: kill switch + status + logs ---------------------------------- #

def test_kill_switch_toggle(monkeypatch, tmp_path):
    monkeypatch.setattr(panel, "KILL_FILE", tmp_path / "KILL")
    assert "ENGAGED" in panel.kill_switch_on("test")
    assert (tmp_path / "KILL").exists()
    assert "cleared" in panel.kill_switch_off()
    assert not (tmp_path / "KILL").exists()
    assert "not engaged" in panel.kill_switch_off()


def test_status_shape():
    s = panel.status()   # read-only against the real repo — safe
    assert set(s) == {
        "time_et", "bot_pid", "bot_alive", "kill_switch",
        "log_age_min", "market_hours",
    }
    assert isinstance(s["bot_alive"], bool)
    assert isinstance(s["kill_switch"], bool)


def test_tail_log(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line{i}" for i in range(300)), encoding="utf-8")
    out = panel.tail_log(5, log_file=log)
    assert out.splitlines() == [f"line{i}" for i in range(295, 300)]
    assert "(no log" in panel.tail_log(5, log_file=tmp_path / "missing.log")
