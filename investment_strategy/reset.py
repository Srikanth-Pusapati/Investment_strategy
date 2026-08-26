"""Reset local per-account state — for a recreated Alpaca account or a paper<->live switch.

The bot keeps state on disk that belongs to ONE account:
  - state/trades.jsonl        the trade ledger the DASHBOARD reads
  - state/risk_state.json     saved peak-equity, halt latch, stops, entry clocks
  - state/equity_history.jsonl the P&L curve
  - dashboard.html            the generated view
Point the bot at a new or different account and that state is stale: the dashboard
shows the OLD account's trades, and a stale peak-equity can trip a false drawdown /
equity-floor halt on the fresh account. This module wipes it back to a clean slate.

Two ways it runs:
  1. MANUALLY — you run it when you recreate the account or go live:
        python -m investment_strategy.reset            # archive a backup, then clean
        python -m investment_strategy.reset --delete   # delete instead of archiving
        python -m investment_strategy.reset --yes      # don't prompt
  2. AUTOMATICALLY — the orchestrator calls maybe_reset_on_account_change() at
     startup; if the connected account's id differs from the last run, it archives
     the old state and starts fresh, so a paper->live switch just works.

Safe by default: it ARCHIVES (moves to state/archive/<timestamp>/) rather than
deletes, and never touches the KILL switch file or your .env.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, load_config
from .ledger import DEFAULT_LEDGER_PATH
from .status import DEFAULT_EQUITY_HISTORY_PATH

log = logging.getLogger("reset")


def _churn_carryover(cfg: Config) -> dict:
    """Extract the churn-guard memory from the OLD risk state before it is
    archived, so a fresh cycle does NOT launder the book's recent history
    (Jul 27-28: the reset wiped NU's exit clock and price; the next day's
    re-buy sailed through the cooldown AND the price-aware guard straight
    into a -4.1% stop — the single largest realized loss of Jul 29).

    Carries: exit clocks/prices, buy clocks/convictions, and loss streaks.
    Positions still OPEN at reset get a SYNTHESIZED exit stamped at reset
    time (no price — the price guard fails open), so the re-entry cooldown
    still applies to names the reset flattened. Best-effort: an unreadable
    state file carries nothing.

    Run-6 item 8b: gated by Config.reset_carry_churn (RESET_CARRY_CHURN,
    default OFF). A pre-registered clean window means clean STATE — run-5
    day 1 was dirtied by run-4 cooldowns/exit prices carried into a
    brand-new account. Off -> {} (empty clocks, prices and streaks)."""
    if not getattr(cfg, "reset_carry_churn", False):
        return {}
    try:
        d = json.loads(Path(cfg.state_file).read_text(encoding="utf-8"))
    except Exception:
        return {}
    now_iso = datetime.now(timezone.utc).isoformat()
    exit_times = {k: str(v) for k, v in d.get("exit_times", {}).items()}
    core = getattr(cfg, "core_etf", "")
    for sym in d.get("entry_times", {}):
        # Open at reset (still on the hold clock) — the reset itself is the
        # exit, so stamp NOW (refreshing any stale stamp from a prior trip:
        # the cooldown must run from the reset, not from last week). The core
        # ETF is exempt: it's a passive allocation the fresh cycle
        # re-establishes immediately.
        if sym != core:
            exit_times[sym] = now_iso
    carry = {
        "exit_times": exit_times,
        "exit_prices": {k: float(v) for k, v in d.get("exit_prices", {}).items()},
        "last_buy_times": {k: str(v) for k, v in d.get("last_buy_times", {}).items()},
        "last_buy_convictions": {
            k: float(v) for k, v in d.get("last_buy_convictions", {}).items()
        },
        "loss_streaks": {k: int(v) for k, v in d.get("loss_streaks", {}).items()},
        "streak_times": {k: str(v) for k, v in d.get("streak_times", {}).items()},
    }
    return {k: v for k, v in carry.items() if v}


def _state_dir(cfg: Config) -> Path:
    return Path(cfg.state_file).parent


def account_marker_path(cfg: Config) -> Path:
    """Where we remember which account the current local state belongs to."""
    return _state_dir(cfg) / "account.json"


def per_account_paths(cfg: Config) -> list[Path]:
    """Every file that belongs to ONE account and must be cleared on a switch. The
    ledger + equity history live alongside the risk state (same state/ dir), so we
    derive them from it — which also keeps this testable against a temp dir."""
    d = _state_dir(cfg)
    paths = [
        Path(cfg.state_file),                    # risk_state.json
        d / DEFAULT_LEDGER_PATH.name,            # trades.jsonl (the dashboard's data)
        d / DEFAULT_EQUITY_HISTORY_PATH.name,    # equity_history.jsonl
    ]
    if cfg.dashboard_file:
        paths.append(Path(cfg.dashboard_file))
    return paths


def reset_local_state(cfg: Config, archive: bool = True) -> list[str]:
    """Clear the per-account state files. Archives them under state/archive/<ts>/
    first (unless archive=False, which deletes). Returns human-readable notes on
    what happened. Never raises."""
    notes: list[str] = []
    dest = None
    carry = _churn_carryover(cfg)
    notes.append(
        "churn carry: "
        + ("ON (RESET_CARRY_CHURN=on — cooldowns/exit prices/streaks re-seeded)"
           if getattr(cfg, "reset_carry_churn", False)
           else "OFF (clean state — RESET_CARRY_CHURN=off, run-6 default)")
    )
    if archive:
        dest = _state_dir(cfg) / "archive" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for p in per_account_paths(cfg):
        try:
            if not p.exists():
                continue
            if archive:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(dest / p.name))
                notes.append(f"archived {p}")
            else:
                p.unlink()
                notes.append(f"deleted {p}")
        except Exception as e:  # a reset must never crash the caller
            notes.append(f"could NOT reset {p}: {e}")
    if archive and dest is not None and dest.exists():
        notes.append(f"backup at {dest}")
    # Re-seed the fresh risk state with the churn memory: cooldowns, exit
    # prices, and loss streaks are about the NAMES, not the account, and a
    # reset must not hand every recently-stopped ticker a clean slate.
    if carry:
        try:
            Path(cfg.state_file).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg.state_file).write_text(
                json.dumps(carry, indent=2), encoding="utf-8"
            )
            notes.append(
                "carried churn memory into the fresh state: "
                + ", ".join(f"{k}({len(v)})" for k, v in carry.items())
            )
        except Exception as e:
            notes.append(f"could NOT carry churn memory: {e}")
    return notes


# -- account-change detection (the automatic path) ------------------------- #
def current_fingerprint(cfg: Config, broker) -> str:
    """A human-readable id for the connected account: "paper:PA123" / "live:...".
    Empty if the account can't be read (caller then skips the check)."""
    acct = broker.account_id()
    return f"{cfg.mode.value}:{acct}" if acct else ""


def _read_marker(cfg: Config) -> str:
    p = account_marker_path(cfg)
    try:
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except Exception:
        return ""


def _write_marker(cfg: Config, fingerprint: str) -> None:
    p = account_marker_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(fingerprint, encoding="utf-8")
    except Exception as e:
        log.warning("Could not write account marker: %s", e)


def maybe_reset_on_account_change(cfg: Config, broker) -> bool:
    """Startup hook: if the connected account differs from the one the local state
    belongs to, archive the old state and start fresh. Returns True if it reset.

    First run (no marker) records the account WITHOUT wiping — existing state is
    assumed to belong to the current account. Best-effort: an unreadable account
    (e.g. bad keys) skips the check entirely rather than risk a wrong wipe."""
    current = current_fingerprint(cfg, broker)
    if not current:
        log.debug("Account fingerprint unavailable — skipping account-change check.")
        return False
    previous = _read_marker(cfg)
    if not previous:
        _write_marker(cfg, current)          # first run: just remember it
        return False
    if previous == current:
        return False                          # same account — nothing to do
    log.warning(
        "Alpaca account CHANGED (%s -> %s) — archiving the old account's local "
        "state and starting fresh (dashboard + risk memory reset).",
        previous, current,
    )
    for note in reset_local_state(cfg, archive=True):
        log.warning("  reset: %s", note)
    _write_marker(cfg, current)
    return True


# -- manual CLI ------------------------------------------------------------ #
def main() -> int:
    logging.basicConfig(level="INFO", format="%(message)s")
    ap = argparse.ArgumentParser(description="Reset local per-account state (dashboard + risk memory).")
    ap.add_argument("--delete", action="store_true", help="delete instead of archiving a backup")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except Exception as e:
        print(f"Config error: {e}")
        return 1

    targets = [p for p in per_account_paths(cfg) if p.exists()]
    if not targets:
        print("Nothing to reset — no local state files exist. You're already clean.")
        _write_marker(cfg, current_fingerprint(cfg, _safe_broker(cfg)) or "")
        return 0

    print(f"This will {'DELETE' if args.delete else 'archive then clear'} the "
          f"{cfg.mode.value}-account local state:")
    for p in targets:
        print(f"  - {p}")
    if not args.yes:
        if input("Proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("Aborted.")
            return 1

    for note in reset_local_state(cfg, archive=not args.delete):
        print(f"  {note}")
    # Re-stamp the marker so the next run knows this state belongs to the current account.
    _write_marker(cfg, current_fingerprint(cfg, _safe_broker(cfg)) or "")
    print("\n✅ Reset complete. Verify with:  python -m investment_strategy.preflight")
    return 0


def _safe_broker(cfg: Config):
    """A broker for the marker re-stamp; a stub with a blank id if it can't connect."""
    try:
        from .execution import AlpacaClient
        return AlpacaClient(cfg)
    except Exception:
        class _Stub:
            def account_id(self):
                return ""
        return _Stub()


if __name__ == "__main__":
    import sys
    sys.exit(main())
