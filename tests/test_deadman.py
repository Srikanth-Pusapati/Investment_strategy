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


def test_attempt_restart_respects_cooldown(monkeypatch):
    stamp = Path(tempfile.mkdtemp()) / "deadman.restart-stamp"
    stamp.touch()                                  # fresh -> cooldown active
    assert deadman.attempt_restart(None, stamp=stamp) is None


def test_attempt_restart_dead_bot_posts_panel(monkeypatch):
    stamp = Path(tempfile.mkdtemp()) / "deadman.restart-stamp"  # missing -> go
    calls = []

    class _Resp:
        def read(self):
            return b"Bot restarted (pid 111).\n"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import urllib.request
    monkeypatch.setattr(deadman, "bot_alive", lambda pid: False)
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: calls.append(req) or _Resp(),
    )
    out = deadman.attempt_restart(4242, stamp=stamp)
    assert out == "auto-restart: Bot restarted (pid 111)."
    assert calls and calls[0].get_method() == "POST"
    assert stamp.exists()                          # cooldown armed for next run


def test_attempt_restart_wedged_bot_is_killed_first(monkeypatch):
    stamp = Path(tempfile.mkdtemp()) / "deadman.restart-stamp"
    killed = []
    alive = {"v": True}

    def _fake_run(cmd, **kw):
        if cmd[0] == "kill":
            killed.append(cmd)
            alive["v"] = False                     # TERM works on 1st check
        import types as _t
        return _t.SimpleNamespace(stdout="")

    import urllib.request
    monkeypatch.setattr(deadman, "bot_alive", lambda pid: alive["v"])
    monkeypatch.setattr(deadman.subprocess, "run", _fake_run)

    class _Resp:
        def read(self):
            return b"ok"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _Resp(),
    )
    out = deadman.attempt_restart(4242, stamp=stamp)
    assert killed and killed[0] == ["kill", "-TERM", "4242"]
    assert out == "auto-restart: ok"


def test_attempt_restart_panel_down_reports_failure(monkeypatch):
    stamp = Path(tempfile.mkdtemp()) / "deadman.restart-stamp"
    import urllib.request

    def _boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(deadman, "bot_alive", lambda pid: False)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    out = deadman.attempt_restart(None, stamp=stamp)
    assert out is not None and "auto-restart FAILED" in out


def test_flatten_hold_missing_fresh_stale():
    marker = Path(tempfile.mkdtemp()) / "flatten.hold"
    assert deadman.flatten_hold_active(marker) is False        # missing
    marker.touch()
    assert deadman.flatten_hold_active(marker) is True         # fresh
    old = time.time() - (deadman.FLATTEN_HOLD_STALE_S + 60)
    os.utime(marker, (old, old))
    assert deadman.flatten_hold_active(marker) is False        # >2h = debris
    # boundary via injected clock: 1s under the cutoff is still active
    marker.touch()
    assert deadman.flatten_hold_active(
        marker, now_ts=marker.stat().st_mtime + deadman.FLATTEN_HOLD_STALE_S - 1
    ) is True


def test_main_stands_down_during_flatten_hold(monkeypatch):
    # Marker fresh -> deadman must NOT diagnose/kill/restart/page, just log
    # its 'skip (flatten hold)' heartbeat and exit 0.
    def _must_not_run(*a, **k):
        raise AssertionError("must not diagnose/restart during a flatten hold")

    monkeypatch.setattr(deadman, "market_hours", lambda now=None: True)
    monkeypatch.setattr(deadman, "flatten_hold_active", lambda *a, **k: True)
    monkeypatch.setattr(deadman, "diagnose", _must_not_run)
    monkeypatch.setattr(deadman, "attempt_restart", _must_not_run)
    lines = []
    import builtins
    real_print = builtins.print
    monkeypatch.setattr(
        builtins, "print",
        lambda *a, **k: lines.append(" ".join(str(x) for x in a)),
    )
    try:
        rc = deadman.main()
    finally:
        builtins.print = real_print
    assert rc == 0
    assert any("skip (flatten hold)" in ln for ln in lines)


def test_main_proceeds_when_no_flatten_hold(monkeypatch):
    # No (or stale) marker -> normal healthy path still prints 'ok'.
    monkeypatch.setattr(deadman, "market_hours", lambda now=None: True)
    monkeypatch.setattr(deadman, "flatten_hold_active", lambda *a, **k: False)
    monkeypatch.setattr(deadman, "diagnose", lambda *a, **k: None)
    lines = []
    import builtins
    real_print = builtins.print
    monkeypatch.setattr(
        builtins, "print",
        lambda *a, **k: lines.append(" ".join(str(x) for x in a)),
    )
    try:
        rc = deadman.main()
    finally:
        builtins.print = real_print
    assert rc == 0
    assert any(ln.endswith(" ok") for ln in lines)


def test_hold_marker_path_matches_flatten_script():
    # deadman and flatten_and_restart must agree on the marker location or
    # the hold-off silently never engages.
    scripts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import flatten_and_restart  # noqa: E402
    assert flatten_and_restart.HOLD_MARKER == deadman.FLATTEN_HOLD_FILE
    assert deadman.FLATTEN_HOLD_FILE.name == "flatten.hold"
    assert deadman.FLATTEN_HOLD_FILE.parent.name == "state"


def _flatten_mod(monkeypatch):
    """Import flatten_and_restart with the marker redirected to a temp path
    (NEVER the real state/flatten.hold — touching that would stand the live
    dead-man down) and the .env / clock / wait dependencies stubbed inert."""
    scripts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import flatten_and_restart as fr  # noqa: E402
    marker = Path(tempfile.mkdtemp()) / "flatten.hold"
    monkeypatch.setattr(fr, "HOLD_MARKER", marker)
    monkeypatch.setattr(
        fr, "env_val", lambda n: "https://paper-api.alpaca.markets/v2")
    monkeypatch.setattr(fr, "next_run_time", lambda: dt.datetime.now(fr.ET))
    return fr, marker


def test_flatten_hold_marker_choreography(monkeypatch):
    # The marker must NOT exist during the (possibly hours-long) open-wait —
    # the dead-man keeps guarding until the flatten actually begins — and
    # MUST exist while the flatten work runs, and be gone after the return.
    fr, marker = _flatten_mod(monkeypatch)
    seen = {}

    def _wait(target):
        seen["during_wait"] = marker.exists()

    def _work():
        seen["during_work"] = marker.exists()
        return 0

    monkeypatch.setattr(fr, "wait_until", _wait)
    monkeypatch.setattr(fr, "_flatten_reset_restart", _work)
    rc = fr.main()
    assert rc == 0
    assert seen["during_wait"] is False    # deadman still armed for the wait
    assert seen["during_work"] is True     # hold raised when the flatten began
    assert not marker.exists()             # removed on the normal return path


def test_flatten_hold_marker_removed_when_flatten_raises(monkeypatch):
    # A flatten that dies mid-work must still drop the marker in its finally —
    # otherwise the dead-man is muted for 2h with the bot down.
    fr, marker = _flatten_mod(monkeypatch)

    def _boom():
        assert marker.exists()
        raise RuntimeError("mid-flatten crash")

    monkeypatch.setattr(fr, "wait_until", lambda target: None)
    monkeypatch.setattr(fr, "_flatten_reset_restart", _boom)
    try:
        fr.main()
        raise AssertionError("main() swallowed the flatten crash")
    except RuntimeError:
        pass
    assert not marker.exists()             # finally removed it anyway


def test_flatten_hold_marker_removed_on_failure_exit_code(monkeypatch):
    # Non-zero return (could-not-flatten path) is still a normal exit — the
    # marker must not survive it either.
    fr, marker = _flatten_mod(monkeypatch)
    monkeypatch.setattr(fr, "wait_until", lambda target: None)
    monkeypatch.setattr(fr, "_flatten_reset_restart", lambda: 1)
    assert fr.main() == 1
    assert not marker.exists()


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
