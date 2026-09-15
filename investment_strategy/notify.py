"""Out-of-band alerting for watchdog CRITICALs — page a human when the safety
loop can't self-heal.

A CRITICAL in the watchdog means the automated defense already failed: a close
order didn't go through (a NAKED, unmonitored position), or the equity floor
latched a halt on a dying account. Until now that only went to the log — i.e. to
nobody, unless someone happened to be watching a terminal. This module gives the
bot a way to reach a person.

Design constraints (match the rest of the safety core):
  - FAILS OPEN, never raises. An alert-send that errors must not break the
    watchdog tick; the underlying event is already logged at CRITICAL regardless.
  - THROTTLED. The watchdog runs every ~30s and retries a failed close every
    tick, so the same CRITICAL recurs until it clears. We de-dupe by key and only
    re-send after a cooldown, so one stuck position can't send 120 emails/hour.
  - BACKS OFF AND SPOOLS when every sink is dead. Sep 7 2026 (Labor Day): DNS
    died on the host and the watchdog-blind key paged 75 times in 56 minutes,
    150 SMTP failures, 0 delivered — a failed send used to un-stamp the
    throttle so the very next tick retried at full cadence, and the storm only
    stopped because the paging window closed. Now a failed delivery parks the
    key behind an exponential retry timer (60s doubling, capped at the
    cooldown) and the undelivered page is appended to a durable JSONL spool
    under state/. The first delivery that succeeds afterwards — any key, any
    process sharing the state dir — sends ONE summary page of what was missed
    ("N alerts were undeliverable between T1 and T2: …") and truncates the
    spool, so an outage ends with a catch-up instead of silence or a flood.
  - No new dependency. Email goes over stdlib smtplib (Gmail app-password SMTP);
    a generic webhook (Slack/Discord/PagerDuty-style) is an optional second sink.
    If nothing is configured, alerting is inert and we just log — no-op by default.

The bot is a long-lived process and cannot call Claude's Gmail MCP itself, so it
owns its own channel here.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import threading
import time
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger("notify")

# Don't re-send the same alert key more than once per this window. The event
# keeps logging every tick regardless; this only bounds the OUTBOUND paging.
DEFAULT_COOLDOWN_S = 900.0  # 15 minutes

# A same-key alert whose severity is at least this multiple of the last one sent
# bypasses the cooldown: a 4-min dark gap must not silence the 67-min gap that
# follows it inside the window (Jul 17: the smallest gap claimed the window and
# suppressed every larger one).
SEVERITY_ESCALATION = 2.0

# Retry schedule for a key whose delivery FAILED on every sink: the n-th
# consecutive failure parks the key for min(cap, base * 2**(n-1)) seconds
# (60, 120, 240, 480, 900, 900, …). ALERT_RETRY_BASE_S / ALERT_RETRY_CAP_S
# override. The cap matches the default 15-min cooldown so, at defaults, a dead
# sink never retries less often than a live one would re-page; severity
# escalation still bypasses it (a >= 2x worse same-key event earns one more try).
DEFAULT_RETRY_BASE_S = 60.0
DEFAULT_RETRY_CAP_S = 900.0
_RETRY_EXP_MAX = 30   # 2**30 * base is already astronomically past any cap

# Undelivered pages are spooled here (JSONL, one record per undelivered page)
# and summarised in one page when a sink works again. Lives under the state dir
# so every process that shares it (orchestrator, ops/deadman.py, preflight) can
# flush it. ALERT_SPOOL_FILE overrides; the default follows STATE_FILE's dir.
DEFAULT_SPOOL_NAME = "alerts_spool.jsonl"
SPOOL_MAX_BYTES = 5_000_000       # stop appending past this; the summary still goes
SPOOL_BODY_CHARS = 400            # body excerpt kept per record
SPOOL_SUMMARY_SUBJECTS = 20       # distinct subjects listed in the summary page
# A RELATIVE spool path is anchored here (the repo root), not at the cwd: the
# orchestrator runs from the repo, but ops/deadman.py and preflight are also
# run by hand from wherever the operator is, and the catch-up page only works
# if every process appends to and flushes the SAME file.
_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AlertConfig:
    """Where CRITICAL alerts go. Both sinks optional; unset => log-only (inert)."""
    enabled: bool
    # -- email (Gmail SMTP with an app password) --
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_to: str            # comma-separated recipients
    # -- generic webhook (Slack/Discord/PagerDuty incoming URL) --
    webhook_url: str
    cooldown_s: float
    # -- dead-sink handling (Sep 7 storm); defaulted so keyword call sites that
    #    predate them keep constructing --
    retry_base_s: float = DEFAULT_RETRY_BASE_S
    retry_cap_s: float = DEFAULT_RETRY_CAP_S
    spool_path: str = "state/" + DEFAULT_SPOOL_NAME   # "" disables spooling


class Alerter:
    """Sends CRITICAL alerts to email and/or a webhook, throttled per key.

    Never raises: every send path is wrapped so a mail/network failure degrades to
    a logged warning rather than taking down the watchdog thread that called it.
    """

    def __init__(self, cfg: AlertConfig, async_send: bool = False) -> None:
        self.cfg = cfg
        # key -> (wall-clock ts, severity) of the last ATTEMPTED alert for that
        # key. Stamped optimistically at admission; a delivery failure no longer
        # pops it (that was the Sep 7 retry-every-tick bug) — it parks the key in
        # _backoff instead, which then governs the retry timing.
        self._last_sent: dict[str, tuple[float, float]] = {}
        # key -> (retry_after_ts, consecutive_failures); present only while the
        # key's latest attempt failed on every sink. Cleared by a delivery.
        self._backoff: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._spool_lock = threading.Lock()
        self._spool_path: Path | None = (
            Path(cfg.spool_path) if getattr(cfg, "spool_path", "") else None
        )
        self._spool_full_warned = False
        # Optional background sender. The throttle decision still runs on the
        # CALLER's thread (fast, deterministic), but the blocking SMTP/webhook
        # I/O moves to a worker so a post-wake getaddrinfo stall can't wedge the
        # watchdog thread that paged. Short-lived callers (deadman, preflight)
        # keep async_send=False so their page can't die with the process; the
        # long-lived orchestrator opts in and flush()es on shutdown.
        self._async = async_send
        self._queue: queue.Queue | None = None
        if async_send:
            self._queue = queue.Queue()
            threading.Thread(
                target=self._drain, name="alerter", daemon=True,
            ).start()
        if cfg.enabled and not (self._email_configured or cfg.webhook_url):
            log.warning(
                "ALERTS_ENABLED is on but no email or webhook is configured — "
                "CRITICAL alerts will only be logged. Set ALERT_EMAIL_TO (+SMTP) "
                "or ALERT_WEBHOOK_URL."
            )
        pending = self._spool_count()
        if pending:
            log.info(
                "Alert spool holds %d undelivered alert(s) from earlier (%s); a "
                "summary page goes out on the next successful delivery.",
                pending, self._spool_path,
            )

    @property
    def _email_configured(self) -> bool:
        c = self.cfg
        return bool(c.smtp_host and c.smtp_user and c.smtp_password and c.email_to)

    @staticmethod
    def _now() -> float:
        # WALL clock — the cooldown/backoff must not stretch across host sleep
        # (time.monotonic froze during suspend, turning a 15-min window into
        # ~6.2 wall-clock hours on Jul 17 and swallowing the pages).
        return time.time()

    def critical(
        self, key: str, subject: str, body: str, severity: float | None = None,
    ) -> bool:
        """Page a human about a CRITICAL condition. `key` de-dupes recurring
        events (e.g. the same symbol failing to close every tick) so we send at
        most once per cooldown. `severity` (optional, higher = worse — e.g. a
        dark-gap's minutes) lets a materially worse same-key event bypass a
        cooldown a milder one claimed. Returns True when the page was admitted
        (dispatched, or queued in async mode); False when disabled or held by
        the cooldown/backoff. Best-effort; swallows all errors."""
        if not self.cfg.enabled:
            return False
        admitted, why = self._should_send(key, severity)
        if not admitted:
            if why == "backoff":
                # Every sink was failing the last time this key tried: hold it
                # (no attempt) but keep a durable record for the catch-up page.
                with self._lock:
                    retry_after, fails = self._backoff.get(key, (0.0, 0))
                log.info(
                    "ALERT held (sinks failing, %d failure(s); retry in %.0fs) — "
                    "spooled: %s", fails, max(0.0, retry_after - self._now()),
                    subject,
                )
                self._spool_append(key, subject, body, attempts=fails,
                                   reason="backoff")
            return False
        if self._async and self._queue is not None:
            self._queue.put((key, subject, body))
        else:
            self._dispatch(key, subject, body)
        return True

    def _dispatch(self, key: str, subject: str, body: str) -> None:
        """Actually deliver to the configured sinks. On delivery, clear the
        key's backoff and flush any spooled catch-up. On total failure, park the
        key behind an exponential retry timer and spool the page — never retry
        at caller cadence (Sep 7: 75 attempts in 56 min against dead DNS)."""
        if self._deliver(subject, body):
            with self._lock:
                self._backoff.pop(key, None)
            self._flush_spool()
            return
        with self._lock:
            prev = self._backoff.get(key)
            fails = (prev[1] if prev else 0) + 1
            wait = self._backoff_wait(fails)
            self._backoff[key] = (self._now() + wait, fails)
        log.error(
            "ALERT not delivered (no working sink; failure %d, next retry in "
            "%.0fs): %s", fails, wait, subject,
        )
        self._spool_append(key, subject, body, attempts=fails,
                           reason="delivery_failed")

    def _backoff_wait(self, fails: int) -> float:
        """Seconds a key waits after its `fails`-th consecutive total failure:
        min(cap, base * 2**(fails-1)), never negative."""
        wait = min(
            float(self.cfg.retry_cap_s),
            float(self.cfg.retry_base_s) * (2 ** min(max(fails, 1) - 1, _RETRY_EXP_MAX)),
        )
        return max(0.0, wait)

    def _deliver(self, subject: str, body: str) -> bool:
        """Try every configured sink; True if at least one took the page."""
        sent = False
        if self._email_configured:
            sent = self._send_email(subject, body) or sent
        if self.cfg.webhook_url:
            sent = self._send_webhook(subject, body) or sent
        return sent

    def _drain(self) -> None:
        """Background worker: deliver queued alerts so blocking I/O never wedges
        the caller (watchdog) thread. Daemon; never raises out."""
        assert self._queue is not None
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._dispatch(*item)
            except Exception:  # noqa: BLE001 — a bad send must not kill the worker
                log.exception("alert worker failed to dispatch")
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 10.0) -> None:
        """Block until queued alerts drain (or `timeout`). Call before a
        long-lived process exits so in-flight pages aren't lost. No-op in sync
        mode. Waits on the worker's task_done, not on an empty queue: the queue
        empties the instant the worker picks a page up, while the delivery —
        and the spool catch-up summary that follows a success — is still in
        flight."""
        if not self._async or self._queue is None:
            return
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.1)

    # -- throttle ----------------------------------------------------------- #
    def _should_send(
        self, key: str, severity: float | None = None,
    ) -> tuple[bool, str]:
        """(admitted, reason). reason is 'send', 'cooldown' (last attempt was
        delivered and the window hasn't passed) or 'backoff' (last attempt
        failed on every sink and its retry timer hasn't expired). Severity
        escalation (>= SEVERITY_ESCALATION x the last attempt) bypasses both:
        a materially worse event is worth one more try even against a sink
        that was dead a minute ago, and doubling severities bound the count."""
        now = self._now()
        with self._lock:
            prev = self._last_sent.get(key)
            if prev is not None:
                last_ts, last_sev = prev
                escalated = (
                    severity is not None and last_sev > 0
                    and severity >= last_sev * SEVERITY_ESCALATION
                )
                backoff = self._backoff.get(key)
                if backoff is not None:
                    # Last attempt failed everywhere: the retry timer is the gate.
                    if now < backoff[0] and not escalated:
                        return False, "backoff"
                    # Admitting ONE retry: re-park the key for the same wait
                    # right now, on the caller's thread. In async mode the
                    # worker can sit 35-65 s inside _send_email (2 tries + a
                    # 5 s sleep) before it records the outcome, and a 30 s-tick
                    # caller with a persistent key would otherwise enqueue 2-3
                    # duplicate attempts per expiry — each another SMTP probe
                    # of the dead sink and another spool record. _dispatch
                    # still pops this on delivery / overwrites it on failure.
                    self._backoff[key] = (now + self._backoff_wait(backoff[1]),
                                          backoff[1])
                else:
                    within_cooldown = (now - last_ts) < self.cfg.cooldown_s
                    if within_cooldown and not escalated:
                        return False, "cooldown"
            # optimistic; _dispatch parks the key in _backoff if every sink fails
            self._last_sent[key] = (now, severity if severity is not None else 0.0)
            return True, "send"

    # -- spool (durable record of undelivered pages) ------------------------ #
    def _spool_count(self) -> int:
        p = self._spool_path
        if p is None:
            return 0
        try:
            if not p.exists():
                return 0
            with self._spool_lock, p.open("r", encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except Exception as e:  # noqa: BLE001 — best-effort bookkeeping
            log.debug("alert spool count failed: %s", e)
            return 0

    def _spool_append(
        self, key: str, subject: str, body: str, *, attempts: int, reason: str,
    ) -> None:
        """Append one undelivered page. Best-effort: never raises, stops past
        SPOOL_MAX_BYTES (the summary still goes out from what was kept)."""
        p = self._spool_path
        if p is None:
            return
        rec = {
            "ts": self._now(),
            "key": key,
            "subject": subject,
            "body": (body or "")[:SPOOL_BODY_CHARS],
            "attempts": int(attempts),
            "reason": reason,
        }
        try:
            with self._spool_lock:
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.exists() and p.stat().st_size > SPOOL_MAX_BYTES:
                    if not self._spool_full_warned:
                        self._spool_full_warned = True
                        log.warning(
                            "Alert spool %s is over %d bytes; not spooling further "
                            "undelivered pages until it is flushed.", p, SPOOL_MAX_BYTES,
                        )
                    return
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001 — spooling must never break paging
            log.warning("alert spool append failed (%s): %s", p, e)

    def _spool_read(self) -> list[dict]:
        p = self._spool_path
        if p is None or not p.exists():
            return []
        out: list[dict] = []
        with self._spool_lock, p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001 — skip a torn line, keep the rest
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        return out

    @staticmethod
    def _spool_line_is_record(line: str) -> bool:
        """The parse rule _spool_read applies per line — one place, so the
        drop below counts exactly the records the read returned."""
        line = line.strip()
        if not line:
            return False
        try:
            return isinstance(json.loads(line), dict)
        except Exception:  # noqa: BLE001 — torn line
            return False

    def _spool_drop(self, n: int) -> None:
        """Remove the first `n` PARSED records (the ones just summarised),
        keeping any lines appended since they were read — another
        thread/process may have spooled meanwhile. Counts records the way
        _spool_read does (blank/torn lines are not records), so a torn line
        among the first `n` — a crash mid-append — is dropped along with them
        instead of shifting the cut and leaving an already-summarised record
        behind to be double-counted by the next summary. Rewritten via tmp +
        os.replace (the state.py pattern) so a crash mid-rewrite never leaves
        a torn spool."""
        p = self._spool_path
        if p is None or not p.exists() or n <= 0:
            return
        with self._spool_lock:
            lines = p.read_text(encoding="utf-8").splitlines()
            seen = 0
            cut = 0
            for cut, ln in enumerate(lines, start=1):
                if self._spool_line_is_record(ln):
                    seen += 1
                    if seen >= n:
                        break
            else:
                cut = len(lines)
            rest = [ln for ln in lines[cut:] if ln.strip()]
            tmp = p.with_suffix(".tmp")
            tmp.write_text("".join(ln + "\n" for ln in rest), encoding="utf-8")
            os.replace(tmp, p)
            self._spool_full_warned = False

    def _flush_spool(self) -> None:
        """A sink just worked: summarise everything spooled while sinks were
        dead in ONE page and truncate the spool. A failed summary send keeps
        the spool for the next successful delivery. Never raises."""
        try:
            records = self._spool_read()
            if not records:
                return
            subject, body = self._spool_summary(records)
            if self._deliver(subject, body):
                self._spool_drop(len(records))
                log.info(
                    "Alert spool flushed: %d undelivered alert(s) summarised in "
                    "one page.", len(records),
                )
            else:
                log.warning(
                    "Alert spool summary (%d record(s)) could not be delivered; "
                    "keeping the spool for the next successful send.", len(records),
                )
        except Exception:  # noqa: BLE001 — catch-up must never break the live page
            log.exception("alert spool flush failed (non-fatal)")

    @staticmethod
    def _fmt_ts(ts: object) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(float(ts)))
        except Exception:  # noqa: BLE001
            return "?"

    @classmethod
    def _spool_summary(cls, records: list[dict]) -> tuple[str, str]:
        """One page for a batch of undelivered alerts: count, span, and the
        distinct (key, subject) pairs with their multiplicity."""
        stamps = [r.get("ts") for r in records
                  if isinstance(r.get("ts"), (int, float))]
        t1 = cls._fmt_ts(min(stamps)) if stamps else "?"
        t2 = cls._fmt_ts(max(stamps)) if stamps else "?"
        n = len(records)
        failed = sum(1 for r in records if r.get("reason") == "delivery_failed")
        held = n - failed
        counts: dict[str, int] = {}
        order: list[str] = []
        for r in reversed(records):          # most recent first
            label = f"[{r.get('key', '?')}] {r.get('subject', '?')}"
            if label not in counts:
                order.append(label)
            counts[label] = counts.get(label, 0) + 1
        subject = (
            f"{n} alert{'s were' if n != 1 else ' was'} undeliverable "
            f"between {t1} and {t2}"
        )
        lines = [
            f"{n} CRITICAL alert{'s' if n != 1 else ''} could not be paged out "
            f"between {t1} and {t2}: every configured sink failed "
            f"({failed} delivery attempt{'s' if failed != 1 else ''} failed, "
            f"{held} held by retry backoff).",
            "The bot kept logging them; this is the catch-up so the gap is not "
            "silent. Check the log for the CRITICAL lines and the host's network.",
            "",
            "Distinct alerts (most recent first):",
        ]
        for label in order[:SPOOL_SUMMARY_SUBJECTS]:
            lines.append(f"  - {label}  x{counts[label]}")
        if len(order) > SPOOL_SUMMARY_SUBJECTS:
            lines.append(f"  … and {len(order) - SPOOL_SUMMARY_SUBJECTS} more")
        last = records[-1]
        lines += [
            "",
            f"Most recent ({cls._fmt_ts(last.get('ts'))}) "
            f"[{last.get('key', '?')}] {last.get('subject', '?')}:",
            str(last.get("body", ""))[:SPOOL_BODY_CHARS],
        ]
        return subject, "\n".join(lines)

    # -- sinks -------------------------------------------------------------- #
    def _send_email(self, subject: str, body: str) -> bool:
        msg = EmailMessage()
        msg["Subject"] = f"[trading-bot] {subject}"
        msg["From"] = self.cfg.smtp_user
        recipients = [r.strip() for r in self.cfg.email_to.split(",") if r.strip()]
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)
        for attempt in range(2):
            try:
                with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=15) as s:
                    s.starttls()
                    s.login(self.cfg.smtp_user, self.cfg.smtp_password)
                    s.send_message(msg, to_addrs=recipients)
                log.info("CRITICAL alert emailed to %s: %s", msg["To"], subject)
                return True
            except Exception as e:  # never let paging break the safety loop
                log.warning("Alert email send failed (attempt %d/2): %s", attempt + 1, e)
                if attempt == 0:
                    time.sleep(5)  # brief wait for network to settle after wake-from-sleep
        return False

    def _send_webhook(self, subject: str, body: str) -> bool:
        try:
            payload = json.dumps({"text": f"*{subject}*\n{body}"}).encode("utf-8")
            req = urllib.request.Request(
                self.cfg.webhook_url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                ok = 200 <= resp.status < 300
            if ok:
                log.info("CRITICAL alert posted to webhook: %s", subject)
            else:
                log.warning("Alert webhook returned HTTP %s", resp.status)
            return ok
        except Exception as e:
            log.warning("Alert webhook post failed: %s", e)
            return False


def ping_heartbeat(url: str) -> bool:
    """GET an external dead-man-monitor URL (healthchecks.io-style). The monitor
    pages when pings STOP, so the failure mode that matters is silence — which
    is exactly why this must never raise: a ping failure is the monitor's
    problem to notice, not a reason to disturb the safety loop that called us."""
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001 — liveness ping must never raise
        log.debug("Heartbeat ping failed: %s", e)
        return False


def load_alert_config(getenv) -> AlertConfig:
    """Build AlertConfig from an env-getter (kept out of config.py's giant
    load_config so the alerting knobs live next to the Alerter). `getenv` is
    os.getenv-compatible. The spool defaults to the STATE_FILE directory so
    the orchestrator, ops/deadman.py and preflight share one spool; a relative
    path (the default) is anchored at the repo root, not the caller's cwd."""
    def flag(name: str, default: str = "off") -> bool:
        return getenv(name, default).strip().lower() in {"on", "true", "1", "yes"}

    state_dir = Path(getenv("STATE_FILE", "state/risk_state.json") or
                     "state/risk_state.json").parent
    spool = getenv("ALERT_SPOOL_FILE", str(state_dir / DEFAULT_SPOOL_NAME)).strip()
    if spool and not Path(spool).is_absolute():
        spool = str(_REPO_ROOT / spool)
    return AlertConfig(
        enabled=flag("ALERTS_ENABLED"),
        smtp_host=getenv("ALERT_SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(getenv("ALERT_SMTP_PORT", "587")),
        smtp_user=getenv("ALERT_SMTP_USER", ""),
        smtp_password=getenv("ALERT_SMTP_PASSWORD", ""),
        email_to=getenv("ALERT_EMAIL_TO", ""),
        webhook_url=getenv("ALERT_WEBHOOK_URL", "").strip(),
        cooldown_s=float(getenv("ALERT_COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_S))),
        retry_base_s=float(getenv("ALERT_RETRY_BASE_S", str(DEFAULT_RETRY_BASE_S))),
        retry_cap_s=float(getenv("ALERT_RETRY_CAP_S", str(DEFAULT_RETRY_CAP_S))),
        spool_path=spool,
    )
