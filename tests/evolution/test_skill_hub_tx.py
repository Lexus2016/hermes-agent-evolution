# -*- coding: utf-8 -*-
"""Agentic Transaction Slice 2 — transactional skill hub tests (#2763)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.lib.agentic_tx import begin  # noqa: E402
from evolution.lib.skill_hub_tx import staged_skill_publish  # noqa: E402


def _card(tmp_path: Path) -> Path:
    card = tmp_path / "my-skill" / "SKILL.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("old body", encoding="utf-8")
    return card


def test_gate_failure_discards_and_leaves_store_untouched(tmp_path):
    card = _card(tmp_path)
    result = staged_skill_publish(card, "broken body", lambda b: False)
    assert result.published is False
    assert "validation gate failed" in result.reason
    assert card.read_text(encoding="utf-8") == "old body"
    # No staged residue.
    assert not list(card.parent.glob("*.tx-staged"))


def test_gate_pass_publishes_atomically(tmp_path):
    card = _card(tmp_path)
    result = staged_skill_publish(card, "great body", lambda b: "great" in b)
    assert result.published is True
    assert card.read_text(encoding="utf-8") == "great body"
    assert result.prior_body == "old body"


def test_throwing_validator_fails_closed(tmp_path):
    card = _card(tmp_path)

    def boom(_b):
        raise RuntimeError("gate crashed")

    result = staged_skill_publish(card, "x", boom)
    assert result.published is False
    assert card.read_text(encoding="utf-8") == "old body"


def test_envelope_rollback_restores_prior_body(tmp_path):
    card = _card(tmp_path)
    envelope = begin(enabled=True)
    published = staged_skill_publish(
        card, "new body", lambda b: True, envelope=envelope
    )
    assert published.published and card.read_text(encoding="utf-8") == "new body"
    # A later mutation in the SAME transaction fails → rollback fires the
    # skill-card compensation and the store returns to its pre-tx state
    # (begin() re-raises after rolling back — by design in S1).
    import pytest

    with pytest.raises(RuntimeError):
        with envelope.begin():
            raise RuntimeError("later mutation failed")
    assert card.read_text(encoding="utf-8") == "old body"


def test_envelope_rollback_removes_newly_created_card(tmp_path):
    card = tmp_path / "fresh" / "SKILL.md"
    card.parent.mkdir(parents=True)
    envelope = begin(enabled=True)
    result = staged_skill_publish(card, "first body", lambda b: True, envelope=envelope)
    assert result.published and result.prior_body is None
    envelope.rollback()
    assert not card.exists()


def test_disabled_envelope_registers_nothing(tmp_path):
    card = _card(tmp_path)
    envelope = begin(enabled=False)
    staged_skill_publish(card, "v2", lambda b: True, envelope=envelope)
    envelope.rollback()  # no compensations registered — v2 stays visible
    assert card.read_text(encoding="utf-8") == "v2"
