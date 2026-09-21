"""Tests for the CRITICAL alerter (notify.py) and its watchdog wiring.

Pure logic, no network: we stub the email/webhook sinks and assert on throttling,
fail-open behavior, sink dispatch, dead-sink backoff + the undelivered-alert
spool (Sep 7 2026 storm), the orchestrator's doubling-rung watchdog-blind gate,
and that watchdog CRITICAL paths actually page.

Runnable two ways:
    .venv/bin/python tests/test_notify.py     # standalone, no pytest
    .venv/bin/pytest tests/                    # if pytest is installed
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import AccountSnapshot, Position
from investment_strategy.monitor.watchdog import Watchdog
from investment_strategy.notify import AlertConfig, Alerter, load_alert_config
from investment_strategy.state import PortfolioState


def _spool_tmp() -> str:
    return os.path.join(tempfile.gettempdir(), f"_nf_spool_{uuid.uuid4().hex}.jsonl")


def _alert_cfg(enabled=True, email_to="me@example.com", webhook_url="",
               cooldown_s=900.0, retry_base_s=60.0, retry_cap_s=900.0,
               spool_path=None) -> AlertConfig:
    return AlertConfig(
        enabled=enabled,
        smtp_host="smtp.gmail.com", smtp_port=587,
        smtp_user="bot@example.com", smtp_password="pw",
        email_to=email_to, webhook_url=webhook_url, cooldown_s=cooldown_s,
        retry_base_s=retry_base_s, retry_cap_s=retry_cap_s,
        # every test gets its own spool file so nothing touches the repo's state/
        spool_path=_spool_tmp() if spool_path is None else spool_path,
    )


def _spool_text(a: Alerter) -> str:
    path = a.cfg.spool_path
    if not path or not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class _RecordingAlerter(Alerter):
    """Alerter with the real throttle/dispatch/backoff/spool logic but stubbed
    sinks and a settable wall clock (`t`), so backoff timing is deterministic."""
    def __init__(self, cfg, email_ok=True, webhook_ok=True):
        self.t = 1_700_000_000.0            # fake wall clock, seconds
        super().__init__(cfg)
        self.emails: list[tuple[str, str]] = []
        self.webhooks: list[tuple[str, str]] = []
        self.attempts: list[float] = []     # clock reading at every sink attempt
        self._email_ok = email_ok
        self._webhook_ok = webhook_ok
        self.email_plan: list[bool] | None = None   # per-attempt outcome override

    def _now(self):
        return self.t

    def _send_email(self, subject, body):
        self.attempts.append(self.t)
        ok = self.email_plan.pop(0) if self.email_plan else self._email_ok
        if ok:
            self.emails.append((subject, body))
        return ok

    def _send_webhook(self, subject, body):
        self.attempts.append(self.t)
        if self._webhook_ok:
            self.webhooks.append((subject, body))
        return self._webhook_ok


# -- Alerter unit behavior --------------------------------------------------- #

def test_disabled_never_sends():
    a = _RecordingAlerter(_alert_cfg(enabled=False))
    a.critical("k", "subj", "body")
    assert a.emails == [] and a.webhooks == []


def test_email_sink_dispatch():
    a = _RecordingAlerter(_alert_cfg())
    a.critical("k", "subj", "body")
    assert a.emails == [("subj", "body")]
    assert a.webhooks == []          # no webhook configured


def test_webhook_sink_dispatch():
    a = _RecordingAlerter(_alert_cfg(email_to="", webhook_url="https://hook"))
    a.critical("k", "subj", "body")
    assert a.webhooks == [("subj", "body")]
    assert a.emails == []            # email not configured => not attempted


def test_throttle_dedupes_same_key():
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0))
    a.critical("same", "s1", "b1")
    a.critical("same", "s2", "b2")   # within cooldown -> suppressed
    assert len(a.emails) == 1
    a.critical("other", "s3", "b3")  # different key -> sends
    assert len(a.emails) == 2


def test_throttle_resends_after_cooldown():
    a = _RecordingAlerter(_alert_cfg(cooldown_s=0.0))  # no cooldown
    a.critical("k", "s", "b")
    a.critical("k", "s", "b")
    assert len(a.emails) == 2


def test_failed_sink_retries_after_backoff_not_cooldown():
    # If every sink fails, the key must NOT wait out the full 900s cooldown
    # before retrying — but it must not retry on the very next tick either
    # (Sep 7: the old "pop the throttle stamp" retry paged 75x in 56 min against
    # dead DNS). It retries after the backoff (60s for a first failure).
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0), email_ok=False)
    a.critical("k", "s", "b")        # email fails -> parked for 60s
    a._email_ok = True               # sink recovers right away
    a.t += 30
    a.critical("k", "s", "b")        # inside the backoff -> held, no attempt
    assert a.emails == [] and len(a.attempts) == 1
    a.t += 30                        # 60s after the failure
    a.critical("k", "s", "b")        # retry goes through despite the cooldown
    assert a.emails[0] == ("s", "b")


def test_partial_sink_success_still_throttles():
    # Webhook works, email fails -> overall delivered, so throttle DOES engage.
    a = _RecordingAlerter(
        _alert_cfg(webhook_url="https://hook", cooldown_s=900.0),
        email_ok=False, webhook_ok=True,
    )
    a.critical("k", "s", "b")
    a.critical("k", "s", "b")        # suppressed: previous delivery succeeded
    assert len(a.webhooks) == 1


def test_worse_severity_bypasses_cooldown():
    # A 4-min dark gap must not silence the 67-min gap inside the same window
    # (Jul 17): a same-key alert >= 2x the last severity escalates past the
    # cooldown, but a milder or comparable follow-up stays suppressed.
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0))
    a.critical("dark_gap", "4 min", "b", severity=4.0)
    a.critical("dark_gap", "5 min", "b", severity=5.0)    # not 2x -> suppressed
    assert len(a.emails) == 1
    a.critical("dark_gap", "67 min", "b", severity=67.0)  # >= 2x -> escalates
    assert len(a.emails) == 2
    a.critical("dark_gap", "60 min", "b", severity=60.0)  # milder -> suppressed
    assert len(a.emails) == 2


def test_severityless_alerts_keep_pure_cooldown():
    # Without a severity, behavior is the old pure-cooldown throttle.
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0))
    a.critical("k", "s", "b")
    a.critical("k", "s", "b", severity=100.0)   # last had no severity -> no escalate
    assert len(a.emails) == 1


def test_async_mode_flush_delivers():
    import queue as _q

    class _AsyncRecording(Alerter):
        def __init__(self, cfg):
            super().__init__(cfg, async_send=True)
            self.emails = []
        def _send_email(self, subject, body):
            self.emails.append((subject, body))
            return True

    a = _AsyncRecording(_alert_cfg(cooldown_s=0.0))
    a.critical("k", "s", "b")
    a.flush(timeout=5.0)
    assert a.emails == [("s", "b")]


def test_load_alert_config_from_env():
    env = {
        "ALERTS_ENABLED": "on",
        "ALERT_EMAIL_TO": "you@example.com",
        "ALERT_WEBHOOK_URL": "https://hook ",
        "ALERT_COOLDOWN_SECONDS": "60",
    }
    getenv = lambda k, d=None: env.get(k, d if d is not None else "")  # noqa: E731
    cfg = load_alert_config(getenv)
    assert cfg.enabled is True
    assert cfg.email_to == "you@example.com"
    assert cfg.webhook_url == "https://hook"      # trimmed
    assert cfg.cooldown_s == 60.0
    assert cfg.smtp_host == "smtp.gmail.com"      # default
    # dead-sink knobs default (60s doubling, capped at the 15-min cooldown) and
    # the spool sits next to STATE_FILE — anchored at the REPO ROOT when the
    # path is relative, so ops/deadman.py / preflight run from any cwd share
    # the orchestrator's spool (the catch-up page needs ONE file).
    import investment_strategy.notify as notify_mod
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(notify_mod.__file__)))
    assert cfg.retry_base_s == 60.0 and cfg.retry_cap_s == 900.0
    assert cfg.spool_path == os.path.join(repo_root, "state", "alerts_spool.jsonl")
    env.update({"ALERT_RETRY_BASE_S": "30", "ALERT_RETRY_CAP_S": "120",
                "STATE_FILE": "/tmp/x/risk_state.json"})
    cfg = load_alert_config(getenv)
    assert cfg.retry_base_s == 30.0 and cfg.retry_cap_s == 120.0
    assert cfg.spool_path == "/tmp/x/alerts_spool.jsonl"     # absolute: as given
    env["ALERT_SPOOL_FILE"] = " /tmp/y/spool.jsonl "
    assert load_alert_config(getenv).spool_path == "/tmp/y/spool.jsonl"   # trimmed
    env["ALERT_SPOOL_FILE"] = "var/spool.jsonl"
    assert load_alert_config(getenv).spool_path == os.path.join(repo_root, "var", "spool.jsonl")
    env["ALERT_SPOOL_FILE"] = " "
    assert load_alert_config(getenv).spool_path == ""          # blank = spooling off


# -- dead-sink backoff + undelivered-alert spool (Sep 7 2026 storm) ----------- #

def test_failed_delivery_backs_off_exponentially():
    # Dead sink, same key every 30s for 32 minutes: attempts land at 0, 60, 180,
    # 420, 900 (60s doubling) then every 900s (cap) — not every tick.
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0), email_ok=False)
    t0 = a.t
    for i in range(0, 1920 // 30 + 1):
        a.t = t0 + 30 * i
        a.critical("k", "s", "b")
    assert [t - t0 for t in a.attempts] == [0, 60, 180, 420, 900, 1800]
    assert a.emails == []


def test_backoff_cap_is_configurable():
    a = _RecordingAlerter(_alert_cfg(retry_base_s=60.0, retry_cap_s=100.0),
                          email_ok=False)
    t0 = a.t
    for i in range(0, 40):
        a.t = t0 + 10 * i
        a.critical("k", "s", "b")
    assert [t - t0 for t in a.attempts] == [0, 60, 160, 260, 360]


def test_backoff_clears_on_delivery():
    # Once a retry lands, the key is back on the plain cooldown: no lingering
    # backoff, and the next same-key page waits the cooldown (not 2x the last wait).
    a = _RecordingAlerter(_alert_cfg(cooldown_s=200.0), email_ok=False)
    a.critical("k", "s", "b")                      # fails -> 60s backoff
    a.t += 60
    a._email_ok = True
    a.critical("k", "s", "b")                      # delivered
    assert a.emails[0] == ("s", "b")
    n = len(a.attempts)
    a.t += 199
    a.critical("k", "s", "b")                      # inside the cooldown -> held
    assert len(a.attempts) == n
    a.t += 1
    a.critical("k", "s", "b")                      # cooldown over -> sent
    assert len(a.attempts) == n + 1


def test_escalation_bypasses_backoff_but_milder_stays_held():
    # Keep the Jul 17 rule even against a dead sink: a >= 2x worse same-key
    # event earns one more try; a comparable one stays parked.
    a = _RecordingAlerter(_alert_cfg(), email_ok=False)
    a.critical("dark_gap", "4 min", "b", severity=4.0)       # fails -> backoff 60s
    a.t += 10
    a.critical("dark_gap", "5 min", "b", severity=5.0)       # held (not 2x)
    assert len(a.attempts) == 1
    a.critical("dark_gap", "8 min", "b", severity=8.0)       # 2x -> one more try
    assert len(a.attempts) == 2
    a.t += 10
    a.critical("dark_gap", "9 min", "b", severity=9.0)       # held (backoff now 120s)
    assert len(a.attempts) == 2


def test_critical_returns_whether_admitted():
    a = _RecordingAlerter(_alert_cfg())
    assert a.critical("k", "s", "b") is True
    assert a.critical("k", "s", "b") is False                       # cooldown
    assert _RecordingAlerter(_alert_cfg(enabled=False)).critical("k", "s", "b") is False
    d = _RecordingAlerter(_alert_cfg(), email_ok=False)
    assert d.critical("k", "s", "b") is True                        # attempted (failed)
    d.t += 1
    assert d.critical("k", "s", "b") is False                       # held by backoff


def test_spool_records_failed_and_held_pages():
    a = _RecordingAlerter(_alert_cfg(), email_ok=False)
    a.critical("k", "subj-1", "body-1")           # attempt fails -> spooled
    a.t += 30
    a.critical("k", "subj-2", "body-2")           # held by backoff -> spooled
    rows = [json.loads(ln) for ln in _spool_text(a).splitlines() if ln.strip()]
    assert [(r["key"], r["subject"], r["body"], r["attempts"], r["reason"])
            for r in rows] == [
        ("k", "subj-1", "body-1", 1, "delivery_failed"),
        ("k", "subj-2", "body-2", 1, "backoff"),
    ]
    assert rows[0]["ts"] == a.t - 30 and rows[1]["ts"] == a.t


def test_spool_flushes_single_summary_on_recovery():
    a = _RecordingAlerter(_alert_cfg(), email_ok=False)
    t0 = a.t
    for i in range(0, 8):                  # 0..210s: attempts at 0/60/180, held otherwise
        a.t = t0 + 30 * i
        a.critical("naked", "NAKED AAPL", "close failed")
    assert len(a.attempts) == 3 and a.emails == []
    a._email_ok = True                     # sink comes back
    a.t = t0 + 240
    a.critical("halt", "EQUITY FLOOR HALTED", "b")   # a DIFFERENT key delivers
    assert len(a.emails) == 2              # the live page + ONE summary
    assert a.emails[0] == ("EQUITY FLOOR HALTED", "b")
    subj, body = a.emails[1]
    assert subj.startswith("8 alerts were undeliverable between ")
    assert "[naked] NAKED AAPL  x8" in body
    assert "3 delivery attempts failed, 5 held" in body
    assert "close failed" in body          # the latest body excerpt travels along
    assert _spool_text(a) == ""            # truncated
    a.t += 1000
    a.critical("halt2", "another", "b")
    assert len(a.emails) == 3              # no second summary


def test_spool_survives_restart_and_flushes_from_a_fresh_instance():
    # The spool is a file under state/, so a restart (or ops/deadman.py, which
    # builds its own Alerter on the same path) still delivers the catch-up.
    cfg = _alert_cfg()
    a = _RecordingAlerter(cfg, email_ok=False)
    a.critical("k", "s", "b")
    b = _RecordingAlerter(cfg, email_ok=True)      # same spool path, new process
    b.critical("other", "live", "b")
    assert len(b.emails) == 2
    assert b.emails[1][0].startswith("1 alert was undeliverable between ")
    assert _spool_text(b) == ""


def test_spool_kept_when_summary_send_fails():
    a = _RecordingAlerter(_alert_cfg(), email_ok=False)
    a.critical("k", "s", "b")
    a._email_ok = True
    a.email_plan = [True, False]           # live page delivers, summary send fails
    a.t += 60
    a.critical("other", "live", "b")
    assert a.emails == [("live", "b")]
    assert _spool_text(a).strip() != ""    # still spooled
    a.t += 1000
    a.critical("third", "again", "b")      # next success flushes it
    assert len(a.emails) == 3 and "undeliverable" in a.emails[2][0]
    assert _spool_text(a) == ""


def test_spool_drop_is_atomic_and_keeps_later_records():
    # The flush reads the spool, delivers the summary (slow, no lock held), then
    # drops only what it summarised — a record spooled meanwhile (another
    # thread/process) survives, and the rewrite goes through tmp + os.replace
    # so no .tmp is left behind and a torn line is never written in place.
    a = _RecordingAlerter(_alert_cfg(), email_ok=False)
    for i in range(3):
        a.critical(f"k{i}", f"s{i}", "b")          # three distinct keys, all fail
    assert len(a._spool_read()) == 3
    a._spool_append("late", "arrived after the read", "b", attempts=0, reason="backoff")
    a._spool_drop(3)
    rows = a._spool_read()
    assert [(r["key"], r["subject"]) for r in rows] == [("late", "arrived after the read")]
    assert not os.path.exists(a.cfg.spool_path + ".tmp")
    assert not os.path.exists(os.path.splitext(a.cfg.spool_path)[0] + ".tmp")


def test_spool_drop_counts_parsed_records_not_raw_lines():
    # A torn line (crash mid-append) sits between the records the summary
    # read. _spool_read skips it; the drop must count the same way, or the
    # cut lands one line short and the last summarised record survives to be
    # double-counted by the next catch-up page.
    a = _RecordingAlerter(_alert_cfg(), email_ok=False)
    a.critical("k0", "s0", "b")
    a.critical("k1", "s1", "b")
    with open(a.cfg.spool_path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "key": "torn", "sub')       # torn, no newline
        fh.write("\n")
    a.critical("k2", "s2", "b")
    assert [r["key"] for r in a._spool_read()] == ["k0", "k1", "k2"]
    a._spool_append("late", "after the read", "b", attempts=0, reason="backoff")
    a._spool_drop(3)                                    # what the summary covered
    assert [r["key"] for r in a._spool_read()] == ["late"]
    assert "torn" not in _spool_text(a)
    # And through the real flush: the summary covers 3, nothing is re-summarised.
    b = _RecordingAlerter(_alert_cfg(), email_ok=False)
    b.critical("x0", "s0", "b")
    with open(b.cfg.spool_path, "a", encoding="utf-8") as fh:
        fh.write("{torn\n")
    b.critical("x1", "s1", "b")
    b.critical("x2", "s2", "b")
    b._email_ok = True
    b.t += 900
    b.critical("fresh", "live page", "b")
    summaries = [s for s, _ in b.emails if "undeliverable" in s]
    assert len(summaries) == 1 and summaries[0].startswith("3 alerts were undeliverable")
    assert _spool_text(b) == ""
    b._spool_drop(0)                                    # no-op, never eats a line


def test_backoff_retry_admits_one_attempt_until_the_worker_reports():
    # Async mode: the worker can sit 35-65 s inside SMTP before it records the
    # outcome. Once the backoff timer expires, a 30 s-tick caller with the same
    # key must get ONE attempt queued, not one per tick until the worker
    # returns — each duplicate was another probe of the dead sink and another
    # spool record inflating the catch-up count.
    import queue as _q
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0), email_ok=False)
    a.critical("k", "s", "b")                           # fails -> parked 60 s
    assert len(a.attempts) == 1
    a._async, a._queue = True, _q.Queue()               # worker "busy": nothing drains
    a.t += 60
    admitted = [a.critical("k", "s", "b") for _ in range(3)]
    assert admitted == [True, False, False]
    assert a._queue.qsize() == 1
    assert [r["reason"] for r in a._spool_read()] == ["delivery_failed", "backoff", "backoff"]
    # The re-park is the SAME wait (failure count unchanged); the worker's
    # failure then doubles it, its success clears it.
    assert a._backoff["k"] == (a.t + 60, 1)
    a._dispatch(*a._queue.get())                        # worker runs: still dead
    assert a._backoff["k"] == (a.t + 120, 2)
    a.t += 120
    a._email_ok = True
    assert a.critical("k", "s", "b") is True
    a._dispatch(*a._queue.get())
    assert "k" not in a._backoff and a.emails[0] == ("s", "b")


def test_blank_spool_path_disables_spooling():
    a = _RecordingAlerter(_alert_cfg(spool_path=""), email_ok=False)
    a.critical("k", "s", "b")
    a._email_ok = True
    a.t += 60
    a.critical("k", "s", "b")
    assert a.emails == [("s", "b")]        # delivered, no summary, no file


def test_working_sink_pages_at_doubling_severities_within_cooldown():
    # Sep 7 shape with a WORKING sink and the OLD per-tick caller: severities
    # 5..79 at a 47s tick. The cooldown holds each tick; >=2x escalations get
    # through (5, 10, 20, 40) plus one cooldown expiry (60) — 5 pages in 58 min,
    # not 75. Cooldown + escalation rules are untouched by the backoff.
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0))
    t0 = a.t
    for i, skips in enumerate(range(5, 80)):
        a.t = t0 + 47 * i
        a.critical("watchdog_blind", f"blind {skips}", "b", severity=float(skips))
    assert [s for s, _ in a.emails] == [
        "blind 5", "blind 10", "blind 20", "blind 40", "blind 60"]
    assert _spool_text(a) == ""            # nothing undelivered -> nothing spooled


def test_sep7_storm_replay_dead_sink_is_bounded_and_summarised():
    # logs/Sep_07_2026.log: 75 CRITICAL pages (skips 5..79) at a ~47s tick,
    # 14:09:49 -> 15:05:34 CT, DNS dead ([Errno 8]); old code = 75 attempts, 150
    # SMTP tries, 0 delivered. Now the backoff bounds the attempts, every held
    # page is spooled, and the first delivery after DNS returns is followed by
    # ONE catch-up summary covering all 75.
    a = _RecordingAlerter(_alert_cfg(cooldown_s=900.0), email_ok=False)
    t0 = a.t
    for i, skips in enumerate(range(5, 80)):
        a.t = t0 + 47 * i
        a.critical("watchdog_blind", f"Watchdog blind for {skips} ticks", "b",
                   severity=float(skips))
    assert len(a.attempts) <= 8, a.attempts
    assert a.emails == []
    a._email_ok = True                     # DNS back
    a.t = t0 + 4700
    a.critical("watchdog_blind", "Watchdog blind for 100 ticks", "b", severity=100.0)
    subjects = [s for s, _ in a.emails]
    assert subjects[0] == "Watchdog blind for 100 ticks"
    summaries = [s for s in subjects if "undeliverable" in s]
    assert len(summaries) == 1
    assert summaries[0].startswith("75 alerts were undeliverable between ")
    assert _spool_text(a) == ""


# -- orchestrator: watchdog-blind pages only at doubling rungs ---------------- #

class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def _paging_hours(fn):
    """Pin Orchestrator._overlaps_paging_hours for the duration (no pytest
    fixture so the file stays runnable standalone)."""
    from investment_strategy.orchestrator import Orchestrator
    orig = Orchestrator.__dict__["_overlaps_paging_hours"]
    Orchestrator._overlaps_paging_hours = staticmethod(fn)
    try:
        yield
    finally:
        Orchestrator._overlaps_paging_hours = orig


def _blind_orch():
    from investment_strategy.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(monitor_interval_s=30)
    sev: list[float] = []
    o.alerter = SimpleNamespace(
        critical=lambda k, s, b, severity=None: sev.append(severity) or True)
    o._watchdog_skips = 0
    return o, sev


def test_watchdog_blind_pages_only_at_doubling_rungs():
    # Sep 7: the pre-fix caller paged (and logged CRITICAL) on EVERY tick past
    # 5. Now only the rungs 5/10/20/40 of a 79-tick run page and log.
    handler = _ListHandler()
    logging.getLogger("orchestrator").addHandler(handler)
    try:
        with _paging_hours(lambda s, e: True):
            o, sev = _blind_orch()
            for k in range(1, 80):
                o._watchdog_skips = k
                o._maybe_page_on_skip_run()
            assert sev == [5.0, 10.0, 20.0, 40.0]
            crit = [r for r in handler.records
                    if r.levelno == logging.CRITICAL
                    and "Watchdog BLIND" in r.getMessage()]
            assert len(crit) == 4
            # the run clears on a clean tick; a new run pages at its first rung
            for k in range(1, 6):
                o._watchdog_skips = k
                o._maybe_page_on_skip_run()
            assert sev == [5.0, 10.0, 20.0, 40.0, 5.0]
    finally:
        logging.getLogger("orchestrator").removeHandler(handler)


def test_watchdog_blind_logs_held_when_alerter_declines():
    # A rung whose page the alerter holds (cooldown, or every sink down and the
    # key in its retry backoff) leaves a greppable WARNING next to the
    # CRITICAL, so the log says the human was NOT paged at that rung.
    handler = _ListHandler()
    logging.getLogger("orchestrator").addHandler(handler)
    try:
        with _paging_hours(lambda s, e: True):
            o, sev = _blind_orch()
            o.alerter = SimpleNamespace(critical=lambda k, s, b, severity=None: False)
            o._watchdog_skips = 5
            o._maybe_page_on_skip_run()
            msgs = [(r.levelno, r.getMessage()) for r in handler.records]
            assert any(lvl == logging.CRITICAL and "Watchdog BLIND: 5" in m
                       for lvl, m in msgs)
            assert any(lvl == logging.WARNING and "held by the alerter" in m
                       and "5 ticks" in m for lvl, m in msgs)
            # a delivered rung logs no such warning
            handler.records.clear()
            o.alerter = SimpleNamespace(critical=lambda k, s, b, severity=None: True)
            o._watchdog_skips = 10
            o._maybe_page_on_skip_run()
            assert not any(r.levelno == logging.WARNING for r in handler.records)
    finally:
        logging.getLogger("orchestrator").removeHandler(handler)


def test_watchdog_blind_run_from_overnight_pages_first_in_hours_tick():
    # An outage that began overnight must page at the FIRST in-hours tick, not
    # wait for the next absolute rung (a run at 170 skips at 09:25 ET would
    # otherwise stay silent until 320); doubling continues from there.
    hours = {"open": False}
    with _paging_hours(lambda s, e: hours["open"]):
        o, sev = _blind_orch()
        for k in range(1, 171):
            o._watchdog_skips = k
            o._maybe_page_on_skip_run()
        assert sev == []
        hours["open"] = True
        for k in range(171, 700):
            o._watchdog_skips = k
            o._maybe_page_on_skip_run()
        assert sev == [171.0, 342.0, 684.0]


# -- watchdog wiring: CRITICAL paths must page ------------------------------- #

class _FailingBroker:
    """Every exit path fails (market close AND the marketable-limit fallback) =>
    the genuinely-naked position path that must page (1B.5)."""
    def cancel_open_orders_for(self, symbol):
        pass

    def close_position(self, symbol):
        return None

    def latest_price(self, symbol):
        return 50.0

    def close_position_marketable_limit(self, symbol, qty, ref_price):
        return None                      # fallback also fails => truly naked

    confirm_equity = 1000.0   # what the floor-breach confirming re-read reports

    def get_account(self):
        return _acct(self.confirm_equity)


def _pos(symbol="AAPL", pl_pct=-50.0):
    return Position(symbol=symbol, qty=10.0, avg_entry_price=100.0,
                    current_price=50.0, market_value=500.0,
                    unrealized_pl=-500.0, unrealized_pl_pct=pl_pct)


def _acct(equity, positions=None):
    return AccountSnapshot(equity=equity, last_equity=equity, cash=0.0,
                           buying_power=0.0,
                           positions=positions if positions is not None
                           else [_pos("AAPL")])


def _cfg():
    return SimpleNamespace(
        risk=SimpleNamespace(equity_floor_pct=60.0, max_daily_loss_pct=3.0),
        state_file="state/risk_state.json", monitor_interval_s=30,
    )


def _state() -> PortfolioState:
    p = os.path.join(tempfile.gettempdir(), f"_nf_{uuid.uuid4().hex}.json")
    return PortfolioState(path=p)


def test_failed_flatten_pages():
    state = _state()
    state.peak_equity = 1000.0
    a = _RecordingAlerter(_alert_cfg())
    wd = Watchdog(_cfg(), _FailingBroker(), state=state, alerter=a)
    wd._flatten_all(_acct(500.0), "EQUITY FLOOR")
    assert len(a.emails) == 1
    assert "NAKED" in a.emails[0][0] and "AAPL" in a.emails[0][0]


def test_equity_floor_halt_pages():
    state = _state()
    state.peak_equity = 1000.0        # floor $600
    a = _RecordingAlerter(_alert_cfg())
    broker = _FailingBroker()
    broker.confirm_equity = 500.0     # re-read agrees: genuinely below the floor
    wd = Watchdog(_cfg(), broker, state=state, alerter=a)
    assert wd._equity_floor_breached(_acct(500.0)) is True
    subjects = [s for s, _ in a.emails]
    assert any("EQUITY FLOOR" in s and "HALTED" in s for s in subjects)


def test_no_alerter_is_safe():
    # A watchdog with no alerter must not raise on a CRITICAL path.
    state = _state()
    wd = Watchdog(_cfg(), _FailingBroker(), state=state, alerter=None)
    wd._flatten_all(_acct(500.0), "DAILY LOSS")   # should not raise


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
