"""Tests for the local dead-man's tick-stamp freshness check (ops/deadman.py).

The stamp is written by the bot's MAIN loop, so it detects a WEDGED decision
thread that the 24/7 watchdog would otherwise mask by keeping logs/bot.log warm.
Pure file/clock logic — no process or network.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops"))

import deadman  # noqa: E402


def _stamp(value: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "last_tick.stamp"
    p.write_text(value, encoding="utf-8")
    return p


def test_tick_stamp_age_fresh_and_stale():
    now = time.time()
    fresh = _stamp(f"{now:.0f}")
    age = deadman.tick_stamp_age_minutes(fresh)
    assert age is not None and age < 1.0

    stale = _stamp(f"{now - 600:.0f}")   # 10 min old
    age = deadman.tick_stamp_age_minutes(stale)
    assert age is not None and 9.0 < age < 11.0


def test_tick_stamp_missing_or_garbage_is_none():
    missing = Path(tempfile.mkdtemp()) / "nope.stamp"
    assert deadman.tick_stamp_age_minutes(missing) is None
    assert deadman.tick_stamp_age_minutes(_stamp("not-a-number")) is None


def test_diagnose_flags_wedged_main_loop(monkeypatch):
    # Live pid + warm log, but a stale tick stamp => WEDGED decision thread.
    monkeypatch.setattr(deadman, "bot_pid", lambda *a, **k: 4242)
    monkeypatch.setattr(deadman, "bot_alive", lambda pid: True)
    monkeypatch.setattr(deadman, "log_age_minutes", lambda **k: 1.0)  # log warm
    monkeypatch.setattr(deadman, "tick_stamp_age_minutes", lambda **k: 10.0)  # stale
    problem = deadman.diagnose()
    assert problem is not None and "WEDGED" in problem


def test_diagnose_healthy_when_stamp_fresh(monkeypatch):
    monkeypatch.setattr(deadman, "bot_pid", lambda *a, **k: 4242)
    monkeypatch.setattr(deadman, "bot_alive", lambda pid: True)
    monkeypatch.setattr(deadman, "log_age_minutes", lambda **k: 1.0)
    monkeypatch.setattr(deadman, "tick_stamp_age_minutes", lambda **k: 0.5)
    assert deadman.diagnose() is None


def _run_all():
    import types
    monkey = types.SimpleNamespace(_saved=[])
    def setattr_(obj, name, val):
        monkey._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)
    monkey.setattr = setattr_
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in fns:
        try:
            fn(monkey) if fn.__code__.co_argcount else fn()
            print(f"  PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {e!r}")
        finally:
            for obj, n, old in reversed(monkey._saved):
                setattr(obj, n, old)
            monkey._saved.clear()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
