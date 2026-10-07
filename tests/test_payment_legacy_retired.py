"""F42: the legacy token-approval flow is RETIRED and left byte-identical (S1).

Tests only; no source file is edited by this stage. Content hashes (not
``git diff``) so the check works in a shallow CI checkout and survives rebases.

Regenerating the pins (only ever against the BASE commit, never HEAD)::

    git show 2eee10c:src/invespend/payments/<name>.py | sha256sum
    git show 2eee10c:src/invespend/cli.py > tests/fixtures/legacy/cli_2eee10c.py.txt

Later stages add to this file: S11 ``test_cli_diff_is_added_lines_only``
(difflib against the S1 cli fixture), S12 workflow pin test, S13 docs test.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAYMENTS = ROOT / "src" / "invespend" / "payments"
FIXTURES = Path(__file__).parent / "fixtures"
PINS = json.loads((FIXTURES / "legacy_sha256.json").read_text())
CLI_BASE = FIXTURES / "legacy" / "cli_2eee10c.py.txt"

LEGACY_SIX = tuple(PINS["legacy_six"])
FORBIDDEN_MODULES = ("pipeline", "token", "inbox", "notify", "dedup")
FORBIDDEN_IDENTIFIERS = (
    "approval_recipients", "build_approval_email", "extract_token",
    "run_approval_cycle", "extract_last3",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pin_fixture_covers_legacy_six_and_preexisting_shared_modules():
    pinned = {Path(p).stem for p in PINS["files"]}
    assert pinned == set(LEGACY_SIX) | {"extract", "store", "pg_store"}
    assert set(PINS["preexisting_payments_modules"]) >= pinned | {
        "__init__", "accounts", "audit", "beneficiaries", "caps"}


@pytest.mark.parametrize("rel", sorted(PINS["files"]))
def test_legacy_files_byte_identical_to_base(rel):
    actual = _sha(ROOT / rel)
    assert actual == PINS["files"][rel], (
        f"{rel} changed: legacy flow is retired (F42): do not edit")


def test_cli_base_fixture_matches_pinned_sha256():
    assert _sha(CLI_BASE) == PINS["cli_base_fixture"]


def test_cli_base_fixture_is_valid_python_source():
    ast.parse(CLI_BASE.read_text())


def _v2_modules() -> list[Path]:
    skip = set(PINS["preexisting_payments_modules"])
    return sorted(p for p in PAYMENTS.glob("*.py") if p.stem not in skip)


def _legacy_imports(tree: ast.AST) -> list[str]:
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if "payments" in parts:
                    tail = parts[parts.index("payments") + 1:]
                    if tail and tail[0] in FORBIDDEN_MODULES:
                        bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")
            if node.level and not node.module:
                # "from . import notify" (inside the payments package)
                bad += [a.name for a in node.names if a.name in FORBIDDEN_MODULES]
            elif node.level == 1 and mod[0] in FORBIDDEN_MODULES:
                bad.append(node.module)
            elif "payments" in mod:
                tail = mod[mod.index("payments") + 1:]
                if tail and tail[0] in FORBIDDEN_MODULES:
                    bad.append(node.module)
                elif not tail:
                    bad += [a.name for a in node.names if a.name in FORBIDDEN_MODULES]
    return bad


def test_v2_modules_never_import_legacy_flow():
    for path in _v2_modules():
        src = path.read_text()
        assert _legacy_imports(ast.parse(src)) == [], f"{path.name} imports the legacy flow"
        for ident in FORBIDDEN_IDENTIFIERS:
            assert not re.search(rf"\b{ident}\b", src), f"{path.name} uses legacy {ident}"


def test_scan_detects_a_legacy_import():
    # Mutation spot-check of the scanner itself.
    for snippet in (
        "from invespend.payments.pipeline import x",
        "from .token import x",
        "from . import inbox",
        "import invespend.payments.notify",
        "from invespend.payments import dedup",
    ):
        assert _legacy_imports(ast.parse(snippet)), snippet
    assert _legacy_imports(ast.parse("from .notify_v2 import x")) == []
    assert _legacy_imports(ast.parse("from .v2_inbox import x")) == []


def test_expected_v2_modules_exist_once_s11_landed():
    if not (PAYMENTS / "v2_cli.py").exists():
        pytest.skip("v2_cli.py not built yet (S11)")
    expected = ("v2_inbox loopguard refs sender_auth trigger amounts content commands "
                "bankdetails images instructions notify_v2 routing execute approval "
                "batch cycle mode v2_cli outcome").split()
    missing = [m for m in expected if not (PAYMENTS / f"{m}.py").exists()]
    assert missing == []
