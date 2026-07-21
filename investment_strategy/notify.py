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
  - No new dependency. Email goes over stdlib smtplib (Gmail app-password SMTP);
    a generic webhook (Slack/Discord/PagerDuty-style) is an optional second sink.
    If nothing is configured, alerting is inert and we just log — no-op by default.

The bot is a long-lived process and cannot call Claude's Gmail MCP itself, so it
owns its own channel here.
"""
from __future__ import annotations

import json
import logging
import queue
import smtplib
import threading
import time
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage

log = logging.getLogger("notify")

# Don't re-send the same alert key more than once per this window. The event
# keeps logging every tick regardless; this only bounds the OUTBOUND paging.
DEFAULT_COOLDOWN_S = 900.0  # 15 minutes

# A same-key alert whose severity is at least this multiple of the last one sent
# bypasses the cooldown: a 4-min dark gap must not silence the 67-min gap that
# follows it inside the window (Jul 17: the smallest gap claimed the window and
# suppressed every larger one).
SEVERITY_ESCALATION = 2.0


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


class Alerter:
    """Sends CRITICAL alerts to email and/or a webhook, throttled per key.

    Never raises: every send path is wrapped so a mail/network failure degrades to
    a logged warning rather than taking down the watchdog thread that called it.
    """

    def __init__(self, cfg: AlertConfig, async_send: bool = False) -> None:
        self.cfg = cfg
        # key -> (wall-clock ts, severity) of the last SENT alert for that key.
        self._last_sent: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
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

    @property
    def _email_configured(self) -> bool:
        c = self.cfg
        return bool(c.smtp_host and c.smtp_user and c.smtp_password and c.email_to)

    def critical(
        self, key: str, subject: str, body: str, severity: float | None = None,
    ) -> None:
        """Page a human about a CRITICAL condition. `key` de-dupes recurring
        events (e.g. the same symbol failing to close every tick) so we send at
        most once per cooldown. `severity` (optional, higher = worse — e.g. a
        dark-gap's minutes) lets a materially worse same-key event bypass a
        cooldown a milder one claimed. Best-effort; swallows all errors."""
        if not self.cfg.enabled:
            return
        if not self._should_send(key, severity):
            return
        if self._async and self._queue is not None:
            self._queue.put((key, subject, body))
        else:
            self._dispatch(key, subject, body)

    def _dispatch(self, key: str, subject: str, body: str) -> None:
        """Actually deliver to the configured sinks. On total failure, un-record
        the throttle stamp so the next tick retries delivery."""
        sent = False
        if self._email_configured:
            sent = self._send_email(subject, body) or sent
        if self.cfg.webhook_url:
            sent = self._send_webhook(subject, body) or sent
        if not sent:
            log.error("ALERT not delivered (no working sink): %s", subject)
            with self._lock:
                self._last_sent.pop(key, None)

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
        mode."""
        if not self._async or self._queue is None:
            return
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.1)

    # -- throttle ----------------------------------------------------------- #
    def _should_send(self, key: str, severity: float | None = None) -> bool:
        now = time.time()   # WALL clock — the cooldown must not stretch across
        # host sleep (time.monotonic froze during suspend, turning a 15-min
        # window into ~6.2 wall-clock hours on Jul 17 and swallowing the pages).
        with self._lock:
            prev = self._last_sent.get(key)
            if prev is not None:
                last_ts, last_sev = prev
                within_cooldown = (now - last_ts) < self.cfg.cooldown_s
                escalated = (
                    severity is not None and last_sev > 0
                    and severity >= last_sev * SEVERITY_ESCALATION
                )
                if within_cooldown and not escalated:
                    return False
            # optimistic; _dispatch clears it if every sink fails
            self._last_sent[key] = (now, severity if severity is not None else 0.0)
            return True

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
    os.getenv-compatible."""
    def flag(name: str, default: str = "off") -> bool:
        return getenv(name, default).strip().lower() in {"on", "true", "1", "yes"}

    return AlertConfig(
        enabled=flag("ALERTS_ENABLED"),
        smtp_host=getenv("ALERT_SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(getenv("ALERT_SMTP_PORT", "587")),
        smtp_user=getenv("ALERT_SMTP_USER", ""),
        smtp_password=getenv("ALERT_SMTP_PASSWORD", ""),
        email_to=getenv("ALERT_EMAIL_TO", ""),
        webhook_url=getenv("ALERT_WEBHOOK_URL", "").strip(),
        cooldown_s=float(getenv("ALERT_COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_S))),
    )
