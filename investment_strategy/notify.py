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

    def __init__(self, cfg: AlertConfig) -> None:
        self.cfg = cfg
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()
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

    def critical(self, key: str, subject: str, body: str) -> None:
        """Page a human about a CRITICAL condition. `key` de-dupes recurring
        events (e.g. the same symbol failing to close every tick) so we send at
        most once per cooldown. Best-effort; swallows all errors."""
        if not self.cfg.enabled:
            return
        if not self._should_send(key):
            return
        sent = False
        if self._email_configured:
            sent = self._send_email(subject, body) or sent
        if self.cfg.webhook_url:
            sent = self._send_webhook(subject, body) or sent
        if not sent:
            # Either nothing configured or every sink failed — make sure the
            # missed page is at least loud in the log, and DON'T record it as
            # sent so the next tick retries delivery.
            log.error("ALERT not delivered (no working sink): %s", subject)
            with self._lock:
                self._last_sent.pop(key, None)

    # -- throttle ----------------------------------------------------------- #
    def _should_send(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and (now - last) < self.cfg.cooldown_s:
                return False
            self._last_sent[key] = now  # optimistic; cleared below if all sinks fail
            return True

    # -- sinks -------------------------------------------------------------- #
    def _send_email(self, subject: str, body: str) -> bool:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[trading-bot] {subject}"
            msg["From"] = self.cfg.smtp_user
            recipients = [r.strip() for r in self.cfg.email_to.split(",") if r.strip()]
            msg["To"] = ", ".join(recipients)
            msg.set_content(body)
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=15) as s:
                s.starttls()
                s.login(self.cfg.smtp_user, self.cfg.smtp_password)
                s.send_message(msg, to_addrs=recipients)
            log.info("CRITICAL alert emailed to %s: %s", msg["To"], subject)
            return True
        except Exception as e:  # never let paging break the safety loop
            log.warning("Alert email send failed: %s", e)
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
