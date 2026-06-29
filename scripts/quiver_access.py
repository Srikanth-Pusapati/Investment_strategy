"""Report which Quiver datasets your API token can actually access.

Probes each known dataset's bulk `live/` endpoint (falling back to a per-ticker
`historical/` call when there's no live feed) and classifies the response:

    HAVE      HTTP 200 — your plan includes it
    UPGRADE   HTTP 403 — exists but not on your plan
    AUTH      HTTP 401 — token missing/invalid
    NO-ROUTE  HTTP 404 — no such endpoint at this path
    ERROR     500/other — server error (often a malformed token; see inspect_quiver)

This is the source of truth for "what's my tier" — the plan name on the website
doesn't enumerate per-dataset access, but this does. Loads the key via dotenv (the
same path the app uses, so an inline `# comment` in .env is stripped correctly).

    .venv/bin/python scripts/quiver_access.py
"""
from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

_BASE = "https://api.quiverquant.com/beta"

# (dataset, sample_ticker_for_historical_fallback). Curated to the datasets worth
# wiring as signals/screeners; extend freely.
_DATASETS = [
    ("congresstrading", "AAPL"),
    ("senatetrading", "AAPL"),
    ("housetrading", "AAPL"),
    ("offexchange", "AAPL"),
    ("wallstreetbets", "GME"),
    ("govcontracts", "LMT"),
    ("govcontractsall", "LMT"),
    ("lobbying", "AAPL"),
    ("insiders", "AAPL"),
    ("sec13f", "AAPL"),
    ("sec13fchanges", "AAPL"),
    ("flights", "AAPL"),
    ("patentdrift", "AAPL"),
    ("twitter", "AAPL"),
    ("wikipedia", "AAPL"),
]

_LABELS = {200: "HAVE", 403: "UPGRADE", 401: "AUTH"}


def _classify(status: int) -> str:
    if status in _LABELS:
        return _LABELS[status]
    if status == 404:
        return "NO-ROUTE"
    return f"ERROR {status}"


def _probe(session: requests.Session, url: str) -> int:
    try:
        return session.get(url, timeout=25).status_code
    except Exception:  # noqa: BLE001
        return -1


def main() -> None:
    load_dotenv()
    key = os.getenv("QUIVER_API_KEY", "").strip()
    if not key:
        raise SystemExit("QUIVER_API_KEY not set (check .env)")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {key}"
    print(f"key: {key[:6]}… ({len(key)} chars)\n")
    print(f"{'dataset':18} {'live':10} {'historical':12}")
    print("-" * 42)
    have = []
    for ds, tic in _DATASETS:
        live = _classify(_probe(s, f"{_BASE}/live/{ds}"))
        hist = _classify(_probe(s, f"{_BASE}/historical/{ds}/{tic}"))
        print(f"{ds:18} {live:10} {hist:12}")
        if "HAVE" in (live, hist):
            have.append(ds)
    print(f"\nAccessible datasets ({len(have)}): {', '.join(have) or 'none'}")


if __name__ == "__main__":
    main()
