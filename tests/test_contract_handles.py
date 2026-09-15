"""Run-7 item 4a-20: every EVAL_CONTRACT v3 watch-item handle is a code literal.

WHY: the contract's validity condition reads "a handle that matches nothing is
a measurement breach, not a zero". Run-6 scored the bearish funnel "0 IGNORED"
for the whole window because the `-> IGNORED` handle the away-mode runbook
grepped for had fallen off a truncated line (see orchestrator._log_bear_funnel
history). The v3 contract therefore pins each watch item to a greppable
handle and this test proves each handle exists as a STRING CONSTANT somewhere
in investment_strategy/. Comments and docstrings do not count: a handle that
is only documented is exactly the false positive the contract wants excluded.

Handle source: runs/pre-final-test-run-7/EVAL_CONTRACT.md, the line starting
`**Watch items`, every backticked token. Until that file is on this branch the
pinned copy below (verbatim from the 2026-09-12 draft) drives the
parametrization; test_pinned_handles_match_contract_file keeps the two from
drifting once the contract lands.

Handles landed by OTHER run-7 items are checked the same way (no xfail — a
missing handle must show red, not be waved through): 4a-17 `BOOK BETA
(post-exec):` / `HEDGE COUNTERFACTUAL:`; S-1 `SLOT COUNT:` / `ROTATION:`;
S-3 `PROXY PUT PICK:`; S-4 `STRIKE SNAP:`; S-5 `REGIME HOLD:`. This file is
green only once the whole change-set is on the branch.

Second check (critic item 5): the v3 checker's system exit-reason set — the
rows rule 1 excludes from satellite N — must name literals the bot actually
writes. The first draft named `core_fill` (an ENTRY mark), `core_trim` and
`flatten` (never written); the set was re-pinned to {hedge_unwind,
core_defense, regime_trim, defensive_rotate, correction}. The test greps the
literals and asserts the checker's set is a subset of them, and equal to the
contract's rule-1 set.
"""
from __future__ import annotations

import ast
import importlib.util
import re
from functools import lru_cache
from pathlib import Path

import pytest

import investment_strategy

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(investment_strategy.__file__).resolve().parent
CONTRACT = ROOT / "runs" / "pre-final-test-run-7" / "EVAL_CONTRACT.md"

_spec = importlib.util.spec_from_file_location(
    "eval_contract_check", ROOT / "scripts" / "eval_contract_check.py")
ecc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ecc)

# Verbatim from the v3 draft's "Watch items (handles verified by test)" line.
PINNED_HANDLES: tuple[str, ...] = (
    "SELL AUTHORITY:",
    "BOOK BETA CAP:",
    "BOOK BETA (post-exec):",
    "AUTO-HEDGE: beta:",
    "AUTO-HEDGE UNWIND:",
    "HEDGE COUNTERFACTUAL:",
    "PROXY PUT PICK:",
    "STRIKE SNAP:",
    "OPTIONS SINGLE-NAME BULLISH:",
    "ELIGIBLE but IGNORED",
    "-> IGNORED",
    "ignored=",
    "Top-up conviction",
    "SLOT COUNT:",
    "At max open positions",
    "REGIME HOLD:",
    "Market regime:",
    "CORE DEFENSE:",
    "Heartbeat withheld",
    "Watchdog BLIND",
    "not delivered",
    "ROTATION:",
)
# Verbatim from v3 rule 1 ("exit_reason not in the system set **{...}**").
PINNED_SYSTEM_EXIT_REASONS = frozenset(
    {"hedge_unwind", "core_defense", "regime_trim", "defensive_rotate",
     "correction"})


# ---------------------------------------------------------------- parsers ---

def parse_watch_item_handles(text: str) -> tuple[str, ...]:
    """Backticked tokens on the contract's `**Watch items` line, in order.
    Separators (` · ` and ` / `) are irrelevant: only the backticks count."""
    for line in text.splitlines():
        if line.lstrip().startswith("**Watch items"):
            return tuple(re.findall(r"`([^`]+)`", line))
    raise AssertionError("contract has no '**Watch items' line")


def parse_system_exit_reasons(text: str) -> frozenset[str]:
    """The rule-1 system set: `system set **{a, b, c}**`."""
    m = re.search(r"system set \*\*\{([^}]+)\}\*\*", text)
    assert m, "contract rule 1 has no 'system set **{...}**' block"
    return frozenset(s.strip() for s in m.group(1).split(",") if s.strip())


def _contract_text() -> str | None:
    return CONTRACT.read_text(encoding="utf-8") if CONTRACT.exists() else None


_TEXT = _contract_text()
HANDLES = parse_watch_item_handles(_TEXT) if _TEXT else PINNED_HANDLES


# ---------------------------------------------------- source-literal sweep ---

def code_string_constants(source: str, filename: str = "<src>") -> list[str]:
    """Every string constant in `source` EXCEPT docstrings (comments never
    reach the AST). f-string constant parts are included, so a handle that
    sits in the literal part of an f-string counts; one assembled from a
    variable at runtime (`-> {stage}`) does not — by design."""
    tree = ast.parse(source, filename=filename)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@lru_cache(maxsize=None)
def _source_constants() -> dict[str, list[str]]:
    return {
        str(p.relative_to(ROOT)): code_string_constants(
            p.read_text(encoding="utf-8"), str(p))
        for p in sorted(SRC.rglob("*.py"))
    }


def files_with_literal(handle: str) -> list[str]:
    return [f for f, consts in _source_constants().items()
            if any(handle in c for c in consts)]


# exit_reason literals the bot writes: keyword form (orchestrator/ledger) and
# the watchdog's positional `_record_exit(pos, oid, "trail")` /
# `_exit_option_group(group, "flatten")` form, which lands as
# `exit_reason=reason` inside _record_exit.
_EXIT_REASON_KWARG = re.compile(r'exit_reason\s*=\s*"([a-z_]+)"')
_WATCHDOG_POSITIONAL = re.compile(
    r'_(?:record_exit|exit_option_group)\([^)"]*"([a-z_]+)"')


@lru_cache(maxsize=None)
def exit_reason_literals() -> frozenset[str]:
    found: set[str] = set()
    for p in SRC.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        found.update(_EXIT_REASON_KWARG.findall(text))
        found.update(_WATCHDOG_POSITIONAL.findall(text))
    return frozenset(found)


# ------------------------------------------------------------------ tests ---

def test_code_string_constants_excludes_docstrings_and_comments():
    """Guard the helper itself: a handle that lives only in a docstring or a
    comment must NOT count, one in an f-string's literal part must."""
    src = (
        '"""module doc says DOC ONLY: here"""\n'
        'def f(x):\n'
        '    """FUNC DOC: also not code"""\n'
        '    # COMMENT: never in the AST\n'
        '    return f"REAL HANDLE: {x} tail"\n'
    )
    consts = code_string_constants(src)
    assert any("REAL HANDLE:" in c for c in consts)
    for fake in ("DOC ONLY:", "FUNC DOC:", "COMMENT:"):
        assert not any(fake in c for c in consts), fake


def test_watch_item_parser_reads_the_contract_line():
    text = ("**Watch items (handles verified by test)**: `A:` · `B:` / "
            "`C thing` · `d=`.\n")
    assert parse_watch_item_handles(text) == ("A:", "B:", "C thing", "d=")
    assert parse_system_exit_reasons(
        "not in the system set **{x, y, z}** (the literals") == {"x", "y", "z"}


@pytest.mark.parametrize("handle", HANDLES)
def test_watch_item_handle_is_a_code_literal(handle: str):
    """Every contract handle is a substring of at least one non-docstring
    string constant under investment_strategy/. See the module docstring for
    the handles other run-7 items land; they are NOT xfailed on purpose."""
    hits = files_with_literal(handle)
    assert hits, (
        f"contract handle {handle!r} matches no code literal in "
        f"investment_strategy/ — a measurement breach, not a zero "
        f"(EVAL_CONTRACT v3, validity: watch-item handles)")


def test_pinned_handles_match_contract_file():
    """The pinned copy is a stand-in until the contract lands on the branch;
    once it does, the file is the authority and the copy must equal it."""
    if _TEXT is None:
        pytest.skip(f"{CONTRACT.relative_to(ROOT)} not on this branch yet — "
                    f"pinned copy drove the parametrization")
    assert parse_watch_item_handles(_TEXT) == PINNED_HANDLES


def test_checker_system_exit_reasons_are_code_literals():
    """Critic item 5: the v3 checker excludes exit_reason values that the bot
    actually writes — never `core_fill` (an entry mark) or `core_trim` /
    `flatten`-style names that no sell row carries. The set must be a subset
    of the literals in the source and equal to the contract's rule-1 set."""
    literals = exit_reason_literals()
    checker = set(ecc.V3_SYSTEM_EXIT_REASONS)
    missing = checker - literals
    assert not missing, (
        f"checker system exit reasons {sorted(missing)} are written nowhere "
        f"in investment_strategy/ (literals found: {sorted(literals)})")
    # The five the contract names all exist, and the bot's own model/bracket
    # exits are never swallowed into the system set.
    assert {"hedge_unwind", "core_defense", "regime_trim", "defensive_rotate",
            "correction"} <= literals
    assert not checker & {"decision", "thesis_decay", "bracket_stop",
                          "bracket_take", "stop", "take", "trail", "flatten",
                          "core_fill", "core_trim"}
    contract_set = (parse_system_exit_reasons(_TEXT) if _TEXT
                    else PINNED_SYSTEM_EXIT_REASONS)
    assert checker == contract_set == PINNED_SYSTEM_EXIT_REASONS
