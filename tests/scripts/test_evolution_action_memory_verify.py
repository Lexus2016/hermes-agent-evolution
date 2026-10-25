"""Tests for action-conditioned memory verification (issue #3514)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from evolution_action_memory_verify import (
    IRREVERSIBLE,
    ROUTINE_READ,
    WRITE,
    consequence_class,
    main,
    verify_action,
)


def test_consequence_lookup():
    assert consequence_class("read_file") == ROUTINE_READ
    assert consequence_class("web_search") == ROUTINE_READ
    assert consequence_class("write_file") == WRITE
    assert consequence_class("patch") == WRITE
    assert consequence_class("merge") == IRREVERSIBLE
    assert consequence_class("send_message") == IRREVERSIBLE
    # unknown tools are treated as writes — re-check by default
    assert consequence_class("some_new_tool") == WRITE


def test_routine_read_proceeds_without_premises():
    result = verify_action("read_file", [])
    assert result["proceed"] is True
    assert result["reason"] == "routine_read_no_recheck"
    assert result["premises_checked"] == 0


def test_write_with_fresh_premise_proceeds():
    result = verify_action("write_file", ["deploy plan confirmed by user 2026-10-25"])
    assert result["proceed"] is True
    assert result["consequence_class"] == WRITE
    assert result["reason"] == "premises_fresh"


def test_write_with_stale_premise_holds():
    result = verify_action("patch", ["remembered proposal: switch providers"])
    assert result["proceed"] is False
    assert result["reason"] == "stale_premises"
    assert len(result["stale_premises"]) == 1


def test_irreversible_without_premises_holds():
    result = verify_action("merge", [])
    assert result["proceed"] is False
    assert result["reason"] == "no_premises_to_recheck"
    assert result["consequence_class"] == IRREVERSIBLE


def test_irreversible_requires_every_premise_fresh():
    fresh = "release notes verified against tag"
    stale = "old plan from last week"
    held = verify_action("send_message", [fresh, stale])
    assert held["proceed"] is False
    cleared = verify_action("send_message", [fresh, "recipient list user-acknowledged"])
    assert cleared["proceed"] is True


def test_blank_premises_are_ignored():
    result = verify_action("write_file", ["", "   ", "config change confirmed"])
    assert result["proceed"] is True
    assert result["premises_checked"] == 1


def test_cli_exit_codes(capsys):
    assert main(["--tool", "read_file"]) == 0
    assert main(["--tool", "merge"]) == 1
    assert main(["--tool", "write_file", "--premise", "edit confirmed"]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    assert json.loads(lines[-1])["proceed"] is True
    assert json.loads(lines[1])["reason"] == "no_premises_to_recheck"
