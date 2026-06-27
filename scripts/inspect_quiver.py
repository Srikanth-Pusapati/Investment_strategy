"""Probe the Quiver endpoints we use and print the REAL response schema.

Loads the key via python-dotenv — the SAME path the app uses — so it strips any
inline `# comment` in .env (a naive `cut`/`grep` does not, which sends a malformed
token and gets a confusing HTTP 500). GETs each endpoint and reports the status +
sorted field names + a sample row; non-JSON/error bodies are printed raw so you
can see what came back instead of crashing on a parse error.

    .venv/bin/python scripts/inspect_quiver.py
"""
from __future__ import annotations

import json
import os

import requests
from dotenv import load_dotenv

_BASE = "https://api.quiverquant.com/beta"
_ENDPOINTS = [
    "live/offexchange",
    "live/wallstreetbets",
    "live/congresstrading",
    # per-ticker fallbacks if a live feed is empty/unavailable off-hours
    "historical/offexchange/AAPL",
    "historical/wallstreetbets/GME",
]


def _load_key() -> str:
    load_dotenv()  # same loader as the app — strips inline comments / quotes
    key = os.getenv("QUIVER_API_KEY", "").strip()
    if not key:
        raise SystemExit("QUIVER_API_KEY not set (check .env)")
    return key


def main() -> None:
    key = _load_key()
    print(f"key: {key[:6]}… ({len(key)} chars)\n")
    headers = {"Authorization": f"Bearer {key}"}
    for ep in _ENDPOINTS:
        url = f"{_BASE}/{ep}"
        print(f"=== GET {ep} ===")
        try:
            r = requests.get(url, headers=headers, timeout=25)
        except Exception as e:  # noqa: BLE001
            print(f"  request error: {e}\n")
            continue
        ct = r.headers.get("content-type", "?")
        print(f"  HTTP {r.status_code}  content-type={ct}")
        if r.status_code != 200:
            print(f"  body: {r.text[:300]!r}\n")
            continue
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            print(f"  non-JSON body: {r.text[:300]!r}\n")
            continue
        if isinstance(body, list) and body:
            print(f"  rows: {len(body)}")
            print(f"  keys: {sorted(body[0].keys())}")
            print(f"  sample: {json.dumps(body[0], indent=2)}\n")
        else:
            print(f"  unexpected shape: {str(body)[:300]}\n")


if __name__ == "__main__":
    main()
