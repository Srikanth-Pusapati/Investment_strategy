"""Tests for per-account state reset (recreated account / paper<->live switch).

No network: a fake broker supplies the account id, and a temp state dir stands in
for state/. We assert files are archived/deleted, and that the account-change hook
resets only on a real change (not on the first run or the same account).

Runnable two ways:
    .venv/bin/python tests/test_reset.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.reset import (
    account_marker_path,
    maybe_reset_on_account_change,
    per_account_paths,
    reset_local_state,
)


class _Broker:
    def __init__(self, acct_id):
        self._id = acct_id

    def account_id(self):
        return self._id


def _cfg(tmp, mode="paper", dashboard=""):
    return SimpleNamespace(
        state_file=str(Path(tmp) / "risk_state.json"),
        dashboard_file=dashboard,
        mode=SimpleNamespace(value=mode),
    )


def _seed_state(cfg):
    """Create the three per-account files so a reset has something to clear."""
    for p in per_account_paths(cfg):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stale", encoding="utf-8")


def test_reset_archives_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        _seed_state(cfg)
        notes = reset_local_state(cfg, archive=True)
        for p in per_account_paths(cfg):
            assert not p.exists()                 # cleared from their live location
        assert any("archived" in n for n in notes)
        archive_dir = Path(tmp) / "archive"
        assert archive_dir.exists() and any(archive_dir.iterdir())  # backup kept


def test_reset_delete_removes_without_backup():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        _seed_state(cfg)
        reset_local_state(cfg, archive=False)
        for p in per_account_paths(cfg):
            assert not p.exists()
        assert not (Path(tmp) / "archive").exists()


def test_first_run_records_marker_without_wiping():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        _seed_state(cfg)
        did_reset = maybe_reset_on_account_change(cfg, _Broker("PA111"))
        assert did_reset is False                 # no prior marker -> just record
        assert account_marker_path(cfg).read_text().strip() == "paper:PA111"
        for p in per_account_paths(cfg):
            assert p.exists()                     # existing state left intact


def test_same_account_does_not_reset():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        _seed_state(cfg)
        maybe_reset_on_account_change(cfg, _Broker("PA111"))   # records marker
        did_reset = maybe_reset_on_account_change(cfg, _Broker("PA111"))
        assert did_reset is False
        for p in per_account_paths(cfg):
            assert p.exists()


def test_account_change_triggers_reset():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        _seed_state(cfg)
        maybe_reset_on_account_change(cfg, _Broker("PA111"))   # first run: marker=PA111
        did_reset = maybe_reset_on_account_change(cfg, _Broker("PA999"))  # new account
        assert did_reset is True
        for p in per_account_paths(cfg):
            assert not p.exists()                 # old state cleared
        assert account_marker_path(cfg).read_text().strip() == "paper:PA999"


def test_paper_to_live_switch_triggers_reset():
    with tempfile.TemporaryDirectory() as tmp:
        paper = _cfg(tmp, mode="paper")
        _seed_state(paper)
        maybe_reset_on_account_change(paper, _Broker("ACC1"))
        live = _cfg(tmp, mode="live")             # same dir, different mode
        did_reset = maybe_reset_on_account_change(live, _Broker("ACC1"))
        assert did_reset is True                  # "paper:ACC1" != "live:ACC1"


def test_unreadable_account_skips_check():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        _seed_state(cfg)
        # Blank id (bad keys / offline) -> never wipe, never crash.
        did_reset = maybe_reset_on_account_change(cfg, _Broker(""))
        assert did_reset is False
        for p in per_account_paths(cfg):
            assert p.exists()


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
