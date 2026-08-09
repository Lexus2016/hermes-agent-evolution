"""Tests for the COVE volatility-tagged memory gate (#1938)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from evolution_volatility_gate import check, classify, list_notes, tag  # noqa: E402


def test_classify():
    assert classify("see https://x.io/y") == "volatile"
    assert classify("upgrade to v1.2.3 done") == "volatile"
    assert classify("api at www.host.com/path") == "volatile"
    assert classify("use binary search o(n) over the array") == "stable"
    assert classify("plan: reproduce the bug then investigate") == "strategic"
    assert (
        classify("plan: debug the dfs invariant hypothesis, o(n) recursion")
        == "strategic"
    )
    assert classify("") == "stable"


def test_tag_and_list(tmp_path):
    ip = str(tmp_path / "idx.json")
    assert tag("n1", "see https://a.io", ip) == {"id": "n1", "level": "volatile"}
    assert json.loads(Path(ip).read_text(encoding="utf-8"))["n1"] == "volatile"
    tag("n2", "binary search", ip)
    assert list_notes(ip) == {"n1": "volatile", "n2": "stable"}


def test_check_detects_volatile(tmp_path):
    ip = str(tmp_path / "idx.json")
    tag("n1", "https://secret.io", ip)
    durable = tmp_path / "skill.md"
    durable.write_text("endpoint: https://secret.io/x requires 1.2.3", encoding="utf-8")
    r = check(str(durable), ip)
    assert not r["ok"]
    assert any(v["kind"] == "url" for v in r["violations"])
    assert any(v["kind"] == "version" for v in r["violations"])


def test_check_clean(tmp_path):
    ip = str(tmp_path / "idx.json")
    tag("n1", "binary search", ip)  # stable note
    durable = tmp_path / "skill.md"
    durable.write_text("use binary search", encoding="utf-8")
    assert check(str(durable), ip)["ok"]
    tag("n2", "https://x.io", ip)  # volatile, but file missing
    assert check(str(tmp_path / "nope.md"), ip)["ok"]
