"""Shared, per-cycle-cached Quiver Quantitative client.

Quiver exposes the same data two ways: a per-symbol `historical/{dataset}/{symbol}`
endpoint and a bulk cross-ticker `live/{dataset}` feed. Our signal layer wants the
per-symbol view ("is congress trading THIS name?") and our screener wants the
cross-ticker view ("which names are members buying?") — but the live feed already
contains BOTH. Calling them separately (and fanning the historical endpoint out
per symbol) pulled congress twice and burned rate limit; paid tiers are ~300
req/min, so that double-pull is what would 429 us as more datasets are added.

This client fixes that: pull each bulk `live/{dataset}` feed ONCE per decision
cycle, cache it, and serve every consumer from the one response. `new_cycle()`
drops the cache at the top of each cycle so data stays fresh between cycles but is
shared within one. Retries with exponential backoff on 429 / transient network
errors (tenacity); any hard failure degrades to [] so a flaky endpoint can never
raise into the trade loop.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger("quiver")

_BASE = "https://api.quiverquant.com/beta"


class _Retryable(Exception):
    """Transient failure (429 / network) worth retrying with backoff."""


class QuiverClient:
    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key
        self.timeout = timeout
        # Per-cycle response cache keyed by request path. Cleared by new_cycle().
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def new_cycle(self) -> None:
        """Drop the per-cycle cache so the next pull fetches fresh data. Call once
        at the start of each decision cycle, before signals/screeners run."""
        with self._lock:
            self._cache.clear()

    # -- public dataset views ---------------------------------------------- #
    def live(self, dataset: str) -> list[dict[str, Any]]:
        """Bulk cross-ticker feed for a dataset (e.g. 'congresstrading',
        'offexchange', 'wallstreetbets'). Cached for the cycle and shared by every
        consumer, so the signal layer and the screener pull it at most once."""
        return self._cached(f"live/{dataset}")

    def historical(self, dataset: str, symbol: str) -> list[dict[str, Any]]:
        """Per-symbol history. Prefer live() where the bulk feed already covers the
        window — this still fans out one request per symbol."""
        return self._cached(f"historical/{dataset}/{symbol.upper()}")

    # -- fetch + cache ----------------------------------------------------- #
    def _cached(self, path: str) -> list[dict[str, Any]]:
        with self._lock:
            if path in self._cache:
                return self._cache[path]
        data = self._fetch(path)
        with self._lock:
            # Cache even an empty/failed result so a down endpoint isn't hammered
            # again within the same cycle.
            self._cache[path] = data
        # Log the size of each real (cache-miss) pull so an EMPTY upstream feed is
        # visible — otherwise a 200-with-[] looks identical downstream to "we
        # filtered everything out". Bulk live feeds at INFO (few per cycle);
        # per-symbol historical at DEBUG (one per symbol, noisy).
        if path.startswith("live/"):
            log.info("Quiver %s -> %d row(s).", path, len(data))
        else:
            log.debug("Quiver %s -> %d row(s).", path, len(data))
        return data

    def _fetch(self, path: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            return self._request(path)
        except Exception as e:  # retries exhausted or hard error -> degrade to []
            log.warning("Quiver %s failed: %s", path, e)
            return []

    @retry(
        retry=retry_if_exception_type(_Retryable),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _request(self, path: str) -> list[dict[str, Any]]:
        """Retry wrapper: backs off on transient (_Retryable) failures."""
        return self._do_request(path)

    def _do_request(self, path: str) -> list[dict[str, Any]]:
        """One HTTP attempt. Maps 429/network to _Retryable (retried upstream),
        other non-200s to [] (treated as no data), 200 to the JSON list."""
        try:
            r = requests.get(
                f"{_BASE}/{path}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise _Retryable(f"network error: {e}") from e
        if r.status_code == 429:
            log.info("Quiver 429 on %s — backing off.", path)
            raise _Retryable("rate limited (429)")
        if r.status_code == 403:
            # Dataset not on the current plan — log loudly ONCE-ish so an opt-in
            # source isn't mistaken for "configured but silently empty".
            log.warning("Quiver %s HTTP 403 — not on your plan: %s",
                        path, r.text[:120].strip())
            return []
        if r.status_code != 200:
            log.debug("Quiver %s HTTP %s", path, r.status_code)
            return []
        body = r.json()
        return body if isinstance(body, list) else []
