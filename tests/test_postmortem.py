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
