"""Tests for the RECOVERABLE Robinhood dead-OAuth latch.

The latch used to be one-way: a dead refresh token disabled all RH reads until
a process restart, even after the user re-ran `robinhood_auth login` (the
2026-07-16 incident — reauth done, bot still blind). These tests pin the new
contract: latch metadata is recorded, `enabled` self-heals when the token file
changes on disk, a failed relogin re-latches with the NEW signature (no
unlatch/relatch spin), the stat() probe is throttled, and the health file the
control panel reads is written on both edges.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_robinhood_latch.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.portfolio.robinhood import RobinhoodReader, _UNSET

try:  # same import the production latch uses
    from mcp.client.auth.exceptions import OAuthTokenError
except ImportError:  # pragma: no cover — fallback name-matching path
    class OAuthTokenError(Exception):
        pass


@pytest.fixture(autouse=True)
def _clean_latch():
    """The latch is CLASS state by design — isolate every test."""
    def reset():
        RobinhoodReader._auth_dead = False
        RobinhoodReader._auth_dead_since = None
        RobinhoodReader._auth_dead_token_sig = None
        RobinhoodReader._last_recovery_check = 0.0
    reset()
    yield
    reset()


def _reader(tmp_path, with_tokens=True):
    tok = tmp_path / "robinhood_oauth.json"
    if with_tokens:
        tok.write_text(json.dumps({"tokens": {"access_token": "a", "refresh_token": "b"}}))
    r = RobinhoodReader.__new__(RobinhoodReader)
    r.cfg = SimpleNamespace(
        robinhood_enabled=True,
        robinhood_mcp_url="https://agent.example/mcp",
        robinhood_mcp_token="",
        robinhood_oauth_file=str(tok),
        robinhood_positions_tool="get_equity_positions",
        robinhood_account_number="",
        # The health file anchors to STATE_FILE's directory (the panel reads
        # <repo>/state/), NOT the token file's.
        state_file=str(tmp_path / "risk_state.json"),
    )
    r._account_number = _UNSET
    return r


def _bump_token_file(tmp_path):
    """Rewrite the token file so its (mtime, size) signature changes — mtime
    forced forward because a same-second rewrite can otherwise tie."""
    tok = tmp_path / "robinhood_oauth.json"
    tok.write_text(json.dumps(
        {"tokens": {"access_token": "fresh-and-longer", "refresh_token": "new"}}
    ))
    st = os.stat(tok)
    os.utime(tok, (st.st_atime + 60, st.st_mtime + 60))


def test_latch_records_metadata_and_disables_all_readers(tmp_path):
    r = _reader(tmp_path)
    other = _reader(tmp_path)
    assert r.enabled and other.enabled
    assert r._latch_if_auth_dead(OAuthTokenError("refresh dead")) is True
    assert RobinhoodReader.auth_dead() is True
    assert RobinhoodReader.auth_dead_since() is not None
    assert RobinhoodReader._auth_dead_token_sig == r._token_file_sig(
        r.cfg.robinhood_oauth_file
    )
    # File unchanged -> the recovery probe must NOT clear the latch.
    assert r.enabled is False
    assert other.enabled is False   # class-level: all readers die together


def test_latch_walks_nested_exception_groups(tmp_path):
    r = _reader(tmp_path)
    wrapped = BaseExceptionGroup(
        "mcp", [BaseExceptionGroup("inner", [OAuthTokenError("dead")])]
    )
    assert r._latch_if_auth_dead(wrapped) is True
    assert RobinhoodReader.auth_dead() is True


def test_plain_errors_never_latch(tmp_path):
    r = _reader(tmp_path)
    assert r._latch_if_auth_dead(ValueError("transient http blip")) is False
    assert RobinhoodReader.auth_dead() is False
    assert r.enabled is True


def test_unlatch_when_token_file_changes(tmp_path):
    r = _reader(tmp_path)
    r._latch_if_auth_dead(OAuthTokenError("dead"))
    assert r.enabled is False
    _bump_token_file(tmp_path)
    RobinhoodReader._last_recovery_check = 0.0   # bypass the 60s throttle
    assert r.enabled is True                     # self-healed, no restart
    assert RobinhoodReader.auth_dead() is False
    assert RobinhoodReader.auth_dead_since() is None


def test_recovery_probe_is_throttled(tmp_path):
    r = _reader(tmp_path)
    r._latch_if_auth_dead(OAuthTokenError("dead"))
    _bump_token_file(tmp_path)
    # A probe just ran -> the fresh token isn't noticed yet...
    RobinhoodReader._last_recovery_check = time.monotonic()
    assert r.enabled is False
    # ...but is once the throttle window has passed.
    RobinhoodReader._last_recovery_check = 0.0
    assert r.enabled is True


def test_failed_relogin_relatches_with_new_sig_no_spin(tmp_path):
    r = _reader(tmp_path)
    r._latch_if_auth_dead(OAuthTokenError("dead"))
    first_sig = RobinhoodReader._auth_dead_token_sig
    _bump_token_file(tmp_path)
    RobinhoodReader._last_recovery_check = 0.0
    assert r.enabled is True                     # unlatched on file change
    # The relogin was ALSO bad: the next call dies again -> re-latch records
    # the NEW file signature, so the same file can't unlatch it again.
    r._latch_if_auth_dead(OAuthTokenError("still dead"))
    assert RobinhoodReader._auth_dead_token_sig != first_sig
    RobinhoodReader._last_recovery_check = 0.0
    assert r.enabled is False                    # stays dead until ANOTHER relogin


def test_health_file_written_on_both_edges(tmp_path):
    r = _reader(tmp_path)
    health = tmp_path / "robinhood_health.json"
    r._latch_if_auth_dead(OAuthTokenError("dead"))
    h = json.loads(health.read_text())
    assert h["auth_dead"] is True and h["since"]
    _bump_token_file(tmp_path)
    RobinhoodReader._last_recovery_check = 0.0
    assert r.enabled is True
    h = json.loads(health.read_text())
    assert h["auth_dead"] is False and h["since"] is None


def test_missing_token_file_never_unlatches(tmp_path):
    r = _reader(tmp_path)
    r._latch_if_auth_dead(OAuthTokenError("dead"))
    os.remove(r.cfg.robinhood_oauth_file)
    RobinhoodReader._last_recovery_check = 0.0
    assert r.enabled is False   # sig unreadable -> stay latched, don't crash


def test_reconcile_health_clears_stale_dead_file_at_startup(tmp_path):
    # The latch is in-memory: a relogin + restart clears it, but the health
    # file written by the PREVIOUS process still says AUTH DEAD — without
    # startup reconciliation the panel shows a phantom outage forever.
    r = _reader(tmp_path)
    health = tmp_path / "robinhood_health.json"
    health.write_text(json.dumps(
        {"auth_dead": True, "since": "2026-07-16T10:00:00+00:00", "detail": "x"}
    ))
    r.reconcile_health()   # fresh process: latch is clear
    h = json.loads(health.read_text())
    assert h["auth_dead"] is False


def test_reconcile_health_noop_when_actually_latched_or_missing(tmp_path):
    r = _reader(tmp_path)
    health = tmp_path / "robinhood_health.json"
    r.reconcile_health()                     # no file -> no crash, no file
    assert not health.exists()
    r._latch_if_auth_dead(OAuthTokenError("dead"))   # writes auth_dead: true
    r.reconcile_health()                     # latch IS set -> file stays dead
    assert json.loads(health.read_text())["auth_dead"] is True
