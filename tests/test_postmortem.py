"""Tests for investment_strategy.postmortem (B2 nightly post-mortem)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import investment_strategy.postmortem as pm_mod
from investment_strategy.postmortem import _POSTMORTEM_SCHEMA, _append_curated, read_curated


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="_postmortem_"))


# ---- _append_curated: FIFO cap + dedupe ------------------------------------ #

def test_append_curated_basic():
    d = _tmpdir()
    with patch.object(pm_mod, "_LESSONS_DIR", d), \
         patch.object(pm_mod, "_CURATED_FILE", d / "curated.md"):
        _append_curated(["Lesson A", "Lesson B"], max_lines=10)
        lines = (d / "curated.md").read_text().splitlines()
    assert "Lesson A" in lines
    assert "Lesson B" in lines


def test_append_curated_deduplicates():
    d = _tmpdir()
    with patch.object(pm_mod, "_LESSONS_DIR", d), \
         patch.object(pm_mod, "_CURATED_FILE", d / "curated.md"):
        _append_curated(["Lesson A"], max_lines=10)
        _append_curated(["Lesson A", "Lesson B"], max_lines=10)
        lines = (d / "curated.md").read_text().splitlines()
    assert lines.count("Lesson A") == 1
    assert "Lesson B" in lines


def test_append_curated_fifo_cap():
    d = _tmpdir()
    with patch.object(pm_mod, "_LESSONS_DIR", d), \
         patch.object(pm_mod, "_CURATED_FILE", d / "curated.md"):
        # Seed with 5 "old" lessons
        for i in range(5):
            _append_curated([f"Old lesson {i}"], max_lines=5)
        # Now add 2 new lessons; old ones should be evicted FIFO
        _append_curated(["New lesson 1", "New lesson 2"], max_lines=5)
        lines = (d / "curated.md").read_text().splitlines()
    assert len(lines) <= 5, f"Expected ≤5 lines; got {len(lines)}: {lines}"
    assert "New lesson 1" in lines
    assert "New lesson 2" in lines


def test_append_curated_fifo_evicts_oldest():
    d = _tmpdir()
    with patch.object(pm_mod, "_LESSONS_DIR", d), \
         patch.object(pm_mod, "_CURATED_FILE", d / "curated.md"):
        _append_curated(["First", "Second", "Third"], max_lines=3)
        _append_curated(["Fourth"], max_lines=3)
        lines = (d / "curated.md").read_text().splitlines()
    # With cap=3: [First, Second, Third] + Fourth → [Second, Third, Fourth]
    assert "First" not in lines
    assert "Fourth" in lines


# ---- read_curated: empty-file and missing-file ----------------------------- #

def test_read_curated_missing_file_returns_empty():
    d = _tmpdir()
    with patch.object(pm_mod, "_CURATED_FILE", d / "curated.md"):
        result = read_curated()
    assert result == ""


def test_read_curated_empty_file_returns_empty():
    d = _tmpdir()
    f = d / "curated.md"
    f.write_text("", encoding="utf-8")
    with patch.object(pm_mod, "_CURATED_FILE", f):
        result = read_curated()
    assert result == ""


def test_read_curated_returns_header_and_lessons():
    d = _tmpdir()
    f = d / "curated.md"
    f.write_text("Lesson A\nLesson B\n", encoding="utf-8")
    with patch.object(pm_mod, "_CURATED_FILE", f):
        result = read_curated()
    assert "Operating lessons" in result
    assert "Lesson A" in result
    assert "Lesson B" in result


def test_read_curated_max_lines_respected():
    """max_lines is a cap on the CURATED FILE (managed by _append_curated),
    not a read-time slice — read_curated reads what's there. So after writing
    with cap=5, the file has at most 5 lines."""
    d = _tmpdir()
    with patch.object(pm_mod, "_LESSONS_DIR", d), \
         patch.object(pm_mod, "_CURATED_FILE", d / "curated.md"):
        # Write 20 lessons with a cap of 5 — file should hold only 5
        for i in range(20):
            _append_curated([f"Lesson {i}"], max_lines=5)
        result = read_curated()
    count = sum(1 for line in result.splitlines() if line.startswith("Lesson "))
    assert count <= 5, f"Expected ≤5 lesson lines after cap; got {count}"


# ---- injection concat (read_curated + render_lessons joins cleanly) --------- #

def test_injection_concat_nonempty():
    """Verify that read_curated() returns something that can be safely concatenated."""
    d = _tmpdir()
    f = d / "curated.md"
    f.write_text("Cap LLY at 1 buy/day when holding.\n", encoding="utf-8")
    with patch.object(pm_mod, "_CURATED_FILE", f):
        curated = read_curated()
    # Simulates what _lessons() does: signal attribution + curated lessons
    attribution_block = "## Track record\n- QUIVER: 5 trades, 60% win, +1.2% avg"
    combined = attribution_block + "\n\n" + curated
    assert "Track record" in combined
    assert "Operating lessons" in combined
    assert "Cap LLY" in combined


# ---- structured-output schema: API-compatible subset only ------------------ #

def test_schema_has_no_unsupported_keywords():
    """The Anthropic json_schema subset rejects array/string constraints like
    maxItems (the Jul 9-10 nightly 400s). Guard the whole schema tree."""
    banned = {"maxItems", "minItems", "maxLength", "minLength", "maxProperties"}

    def walk(node):
        if isinstance(node, dict):
            hits = banned & set(node.keys())
            assert not hits, f"Unsupported schema keyword(s) {hits} in {node}"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(_POSTMORTEM_SCHEMA)
    assert _POSTMORTEM_SCHEMA.get("additionalProperties") is False


# ---- run_postmortem: lesson truncation + curated write ---------------------- #

class _FakeJournalRec:
    def __init__(self):
        self.action, self.verdict, self.symbol = "buy", "approved", "AAPL"
        self.ts, self.conviction, self.approved_notional = "2026-07-10T10:00:00", 0.8, 500.0
        self.reason = "test"


class _FakeJournal:
    def today(self, _dt):
        return [_FakeJournalRec()]


class _FakeLedger:
    def effective(self):
        return []


def _fake_anthropic_returning(payload_json: str):
    from unittest.mock import MagicMock
    block = MagicMock()
    block.type, block.text = "text", payload_json
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create.return_value = resp
    fake_mod = MagicMock()
    fake_mod.Anthropic.return_value = client
    return fake_mod, client


def test_run_postmortem_truncates_lessons_to_three_and_writes_curated():
    import json
    d = _tmpdir()
    fake_mod, client = _fake_anthropic_returning(json.dumps({
        "summary_md": "day summary",
        "lessons": [f"Lesson {i}" for i in range(5)],
    }))
    cfg = type("Cfg", (), {"anthropic_api_key": "k", "decision_model": "m"})()
    with patch.object(pm_mod, "_LESSONS_DIR", d), \
         patch.object(pm_mod, "_CURATED_FILE", d / "curated.md"), \
         patch.dict(sys.modules, {"anthropic": fake_mod}):
        result = pm_mod.run_postmortem(cfg, _FakeLedger(), _FakeJournal(),
                                       day="2026-07-10", max_lessons=15)
    assert result is not None
    # Only the first 3 of 5 lessons reach the curated file (prompt says 0-3;
    # code enforces it since the schema no longer can).
    lines = (d / "curated.md").read_text().splitlines()
    assert lines == ["Lesson 0", "Lesson 1", "Lesson 2"]
    # And the request used the module schema (no maxItems regression).
    schema = client.messages.create.call_args.kwargs["output_config"]["format"]["schema"]
    assert schema is _POSTMORTEM_SCHEMA


def test_run_postmortem_reads_the_labeled_et_day_not_previous():
    """Regression (2026-07-13 skip): `day` parsed as UTC midnight lands on the
    PREVIOUS ET date inside journal._trading_day(), so the labeled day's journal
    looked empty and the nightly post-mortem silently skipped a 15-trade day.
    Uses a REAL DecisionJournal — _FakeJournal.today() ignores its argument and
    would mask exactly this bug."""
    import json
    from unittest.mock import MagicMock
    from investment_strategy.journal import DecisionJournal
    base = _tmpdir()
    day = "2026-07-13"
    (base / f"{day}.jsonl").write_text(json.dumps({
        "ts": "2026-07-13T14:00:00+00:00", "symbol": "AAPL", "action": "buy",
        "instrument": "equity", "conviction": 0.8, "target_weight_pct": 5.0,
        "verdict": "approved", "approved_notional": 500.0, "reason": "r",
        "rationale_head": "rh",
    }) + "\n", encoding="utf-8")
    journal = DecisionJournal(base_dir=base)
    with patch.dict(sys.modules, {"anthropic": MagicMock()}), \
         patch("builtins.print"):  # dry-run prints the prompt; keep output clean
        result = pm_mod.run_postmortem(None, _FakeLedger(), journal,
                                       day=day, dry_run=True)
    assert result is not None, "post-mortem skipped the labeled day's records"


# ---- day_marks: close-to-close per-name day attribution (Aug-23) ----------- #

def _rec(**kw):
    from investment_strategy.ledger import TradeRecord
    return TradeRecord(**kw)


def _closes(table):
    """close_series stub: {symbol: [(date, close), ...]} -> callable."""
    def _fn(symbol, days):
        return table.get(symbol, [])
    return _fn


def test_day_marks_held_winner_giveback_is_the_day_loser():
    """The IESC shape: a partial exit realized +$ against entry basis, but the
    name fell hard close-to-close — the day attribution must show the hit and
    pick the loser by the close-to-close number."""
    day = "2026-08-18"
    records = [
        # 100 sh bought at 50 four days earlier (entry basis far below).
        _rec(ts="2026-08-14T14:00:00Z", symbol="IESC", action="buy",
             qty=100.0, entry_price=50.0, cost_usd=5000.0),
        _rec(ts="2026-08-14T14:00:00Z", symbol="WINR", action="buy",
             qty=10.0, entry_price=100.0, cost_usd=1000.0),
        # Scale-out of 20 sh at 62 on the day: realized +$240 entry-basis.
        _rec(ts="2026-08-18T15:00:00Z", symbol="IESC", action="sell",
             qty=20.0, exit_price=62.0, realized_pl_pct=24.0,
             realized_pl=240.0, exit_reason="scale"),
    ]
    closes = _closes({
        # Prior close 70 -> today 60: held 80 sh lose $800 + the 20 sold at 62
        # lose (62-70)*20 = -160 vs the prior mark. day = 80*60 - 100*70 + 20*62
        "IESC": [("2026-08-17", 70.0), ("2026-08-18", 60.0)],
        "WINR": [("2026-08-17", 100.0), ("2026-08-18", 103.0)],
    })
    lines, winner, loser = pm_mod.day_marks(records, day, closes)
    text = "\n".join(lines)
    assert "IESC" in text and "-960" in text          # 4800-7000+1240
    assert "realized today (entry-basis): +240" in text
    assert "LOSER by close-to-close: IESC" in loser
    assert "WINNER by close-to-close: WINR" in winner  # +30 held, no trades


def test_day_marks_flat_at_close_uses_exit_proceeds():
    day = "2026-08-18"
    records = [
        _rec(ts="2026-08-17T14:00:00Z", symbol="AAPL", action="buy",
             qty=10.0, entry_price=100.0, cost_usd=1000.0),
        _rec(ts="2026-08-18T15:00:00Z", symbol="AAPL", action="sell",
             qty=10.0, exit_price=104.0, realized_pl_pct=4.0,
             realized_pl=40.0, exit_reason="decision"),
    ]
    closes = _closes({"AAPL": [("2026-08-17", 102.0), ("2026-08-18", 99.0)]})
    lines, winner, loser = pm_mod.day_marks(records, day, closes)
    text = "\n".join(lines)
    # Close-to-close: sold at 104 vs prior close 102 -> +20, NOT the +40
    # entry-basis realized number.
    assert "+20" in text and "flat at close" in text
    assert "realized today (entry-basis): +40" in text


def test_day_marks_options_are_realized_only():
    day = "2026-08-18"
    records = [
        _rec(ts="2026-08-18T15:00:00Z", symbol="AMZN", action="sell",
             instrument="option", qty=26.0, realized_pl_pct=-50.0,
             realized_pl=-14956.0, exit_reason="stop"),
    ]
    lines, winner, loser = pm_mod.day_marks(records, day, _closes({}))
    text = "\n".join(lines)
    assert "(option)" in text and "-14,956" in text
    assert "no close-to-close mark for options" in text
    assert winner == "" and loser == ""               # no equity marks


def test_day_marks_missing_closes_degrades_to_na_line():
    day = "2026-08-18"
    records = [
        _rec(ts="2026-08-14T14:00:00Z", symbol="NOPX", action="buy",
             qty=5.0, entry_price=10.0, cost_usd=50.0),
    ]
    lines, winner, loser = pm_mod.day_marks(records, day, _closes({}))
    assert any("NOPX" in l and "n/a" in l for l in lines)


def test_run_postmortem_dry_run_includes_close_to_close_block():
    """End-to-end: a broker stub with daily_close_series gets the block into
    the prompt, and the winner/loser line rides along."""
    import json
    from unittest.mock import MagicMock
    from investment_strategy.journal import DecisionJournal

    base = _tmpdir()
    day = "2026-08-18"
    (base / f"{day}.jsonl").write_text(json.dumps({
        "ts": f"{day}T14:00:00+00:00", "symbol": "IESC", "action": "buy",
        "instrument": "equity", "conviction": 0.6, "target_weight_pct": 5.0,
        "verdict": "approved", "approved_notional": 500.0, "reason": "r",
        "rationale_head": "rh",
    }) + "\n", encoding="utf-8")
    journal = DecisionJournal(base_dir=base)

    class _Ledger:
        def effective(self):
            return [
                _rec(ts="2026-08-14T14:00:00Z", symbol="IESC", action="buy",
                     qty=100.0, entry_price=50.0, cost_usd=5000.0),
                _rec(ts="2026-08-18T15:00:00Z", symbol="IESC", action="sell",
                     qty=20.0, exit_price=62.0, realized_pl_pct=24.0,
                     realized_pl=240.0, exit_reason="scale"),
            ]

    broker = MagicMock()
    broker.daily_close_series = _closes(
        {"IESC": [("2026-08-17", 70.0), ("2026-08-18", 60.0)]})

    printed: list[str] = []
    with patch.dict(sys.modules, {"anthropic": MagicMock()}), \
         patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
        result = pm_mod.run_postmortem(None, _Ledger(), journal, day=day,
                                       dry_run=True, broker=broker)
    assert result is not None
    prompt = "\n".join(printed)
    assert "CLOSE-TO-CLOSE" in prompt
    assert "LOSER by close-to-close: IESC" in prompt
    assert "realized today (entry-basis): +240" in prompt


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
