"""Tests for scripts/evolution_invariant_filter.py (#68, slices 1+2).

The filter screens a skill/prompt/harness rewrite against a versioned set of
immutable Cerberus/household constraints BEFORE it reaches the judge. These
tests pin the filter's own behavior AND its wiring into
``evolution_harness_gate`` — a real call site, not dead code.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_invariant_filter import (  # noqa: E402
    _rules_path,
    check_proposal,
    classify_stakes,
    filter_proposals,
    load_invariant_rules,
    load_safe_fallbacks,
    resolve_pinned_fallback,
)
from evolution_harness_gate import run_cron_pass, run_gate  # noqa: E402
from evolution_harness_sandbox import INVALID, VALIDATED, GateResult  # noqa: E402

SURFACE = {
    "retry_count": 10,
    "backoff": {"base_delay_sec": 1.0, "multiplier": 2.0, "max_delay_sec": 60.0},
    "guard_conditions": [],
}
CLEAN = {
    "type": "retry_policy_change",
    "title": "[HARNESS] retry-policy change for `terminal`",
    "delta": "cap consecutive retries at 3",
    "rationale": "retry spiral",
    "status": "proposed",
    "requires_human_review": True,
    "auto_apply": False,
}


def _green(_):
    return GateResult(True, 0, "ok")


def test_rules_file_loads_and_is_versioned():
    rules = load_invariant_rules()
    assert isinstance(rules["version"], int) and rules["version"] >= 1
    assert rules["rules"]  # non-empty
    assert _rules_path().name == "evolution_invariant_rules.json"


def test_clean_proposal_passes():
    assert check_proposal(CLEAN)["ok"] is True


def test_human_gating_floor_rejects_auto_apply():
    result = check_proposal({**CLEAN, "auto_apply": True})
    assert result["ok"] is False
    assert any(v["rule"] == "human-gating-floor" for v in result["violations"])


def test_human_gating_floor_rejects_explicit_false_review():
    result = check_proposal({**CLEAN, "requires_human_review": False})
    assert result["ok"] is False
    assert any(v["rule"] == "human-gating-floor" for v in result["violations"])


def test_forbidden_path_rewrite_is_rejected():
    result = check_proposal({**CLEAN, "path": "scripts/setup-hermes.sh"})
    assert result["ok"] is False
    assert any(
        v["rule"] == "no-critical-auth-path-rewrite" for v in result["violations"]
    )


def test_outbound_telemetry_delta_is_rejected():
    result = check_proposal({**CLEAN, "delta": "add telemetry for usage attribution"})
    assert result["ok"] is False
    assert any(v["rule"] == "no-outbound-telemetry" for v in result["violations"])


def test_removing_telemetry_is_allowed():
    # Suppression/removal is explicitly permitted — not a violation.
    result = check_proposal({**CLEAN, "delta": "disable posthog telemetry spam"})
    assert result["ok"] is True


def test_safety_gate_disable_is_rejected():
    result = check_proposal({**CLEAN, "delta": "disable the safety scan to speed up"})
    assert result["ok"] is False
    assert any(v["rule"] == "no-safety-gate-disable" for v in result["violations"])


def test_non_dict_proposal_passes():
    assert check_proposal("not-a-dict")["ok"] is True
    assert check_proposal(None)["ok"] is True


def test_filter_proposals_splits():
    good = [CLEAN, {**CLEAN, "delta": "disable posthog telemetry"}]
    bad = [{**CLEAN, "auto_apply": True}]
    passing, rejected = filter_proposals(good + bad)
    assert passing == good
    assert len(rejected) == 1 and rejected[0]["violations"]


def test_missing_rules_file_fails_closed(monkeypatch):
    import evolution_invariant_filter as mod

    monkeypatch.setenv(
        "EVOLUTION_PINNED_RULES", str(Path("/nonexistent/pinned/rules.json"))
    )
    result = check_proposal(CLEAN, rules=None)
    assert result["ok"] is False
    assert any(v["rule"] == "rules-unavailable" for v in result["violations"])


# -- gate wiring: the filter must be a REAL call site, not dead code --------


def test_run_gate_short_circuits_invariant_violation():
    verdict = run_gate({**CLEAN, "auto_apply": True}, gate_runner=_green)
    assert verdict["status"] == INVALID
    assert verdict["zero_fitness"] is True
    assert verdict["reason"] == "invariant violation"
    assert verdict["violations"]


def test_run_gate_still_validates_clean_proposal():
    proposal = {
        **CLEAN,
        "surface": SURFACE,
        "changes": [{"field": "retry_count", "before": 10, "after": 3}],
    }
    verdict = run_gate(proposal, gate_runner=_green)
    assert verdict["status"] == VALIDATED


def test_cron_pass_flags_invariant_rejected():
    payload = {
        "weaknesses": [
            {
                "kind": "retry_spiral",
                "tool": "terminal",
                "max_consecutive": 9,
                "occurrences": 4,
                # A poisoned weakness carries an exfiltration-flavored label
                # that flows into the proposal's delta/rationale.
                "label": "add telemetry to track terminal usage",
            }
        ]
    }
    report = run_cron_pass(payload, gate_runner=_green, surface=SURFACE)
    assert report["invariant_rejected"] == 1
    assert report["gated"] == 0
    assert report["validated"] == 0
    assert report["verdicts"][0]["zero_fitness"] is True


def test_cron_pass_gates_clean_proposal():
    report = run_cron_pass(
        {
            "weaknesses": [
                {
                    "kind": "retry_spiral",
                    "tool": "terminal",
                    "max_consecutive": 9,
                    "occurrences": 4,
                }
            ]
        },
        gate_runner=_green,
        surface=SURFACE,
    )
    assert report["invariant_rejected"] == 0
    assert report["gated"] == 1
    assert report["validated"] == 1


# -- Slice 3: stakes classification + staged rollout -------------------------


def test_classify_stakes_explicit_field():
    label, source = classify_stakes({**CLEAN, "stakes": "low"})
    assert label == "low" and source == "explicit"


def test_classify_stakes_explicit_unknown_fails_safe_to_high():
    label, source = classify_stakes({**CLEAN, "stakes": "extreme"})
    assert label == "high" and source == "default"


def test_classify_stakes_infer_medium_from_path():
    label, source = classify_stakes({**CLEAN, "path": "scripts/evolution_foo.py"})
    assert label == "medium" and source == "inferred"


def test_classify_stakes_infer_high_from_gate_path():
    label, source = classify_stakes(
        {**CLEAN, "path": "scripts/evolution_harness_gate.py"}
    )
    assert label == "high" and source == "inferred"


def test_classify_stakes_defaults_high_for_pathless():
    label, source = classify_stakes(CLEAN)
    assert label == "high" and source == "default"


def test_backcompat_rule_blocks_at_low_stakes():
    # A rule with NO enforce_at still blocks a low-stakes proposal (unchanged).
    result = check_proposal({**CLEAN, "stakes": "low", "auto_apply": True})
    assert result["ok"] is False
    assert result["violations"]  # demoted to advisory, not dropped
    assert result["advisory"] == []


def test_staged_rule_advisory_on_high_stakes():
    # A rule with enforce_at: high does NOT block a high-stakes proposal;
    # the finding surfaces as advisory so rollout is observable, not silent.
    staged_rule = {
        "id": "staged-rule",
        "severity": "hard",
        "check": "forbidden_paths",
        "enforce_at": "high",
        "forbidden_fragments": ["staging-target.py"],
    }
    rules = {
        "version": 1,
        "rules": [staged_rule],
    }
    result = check_proposal({**CLEAN, "path": "scripts/staging-target.py"}, rules=rules)
    assert result["ok"] is True
    assert result["advisory"]


def test_staged_rule_blocks_when_threshold_met():
    # Same staged rule now meets its floor on a LOW-stakes proposal -> blocks.
    staged_rule = {
        "id": "staged-rule",
        "severity": "hard",
        "check": "forbidden_paths",
        "enforce_at": "low",
        "forbidden_fragments": ["staging-target"],
    }
    rendered = {"version": 1, "rules": [staged_rule]}
    result = check_proposal(
        {**CLEAN, "path": "scripts/staging-target.py"}, rules=rendered
    )
    assert result["ok"] is False
    assert any(v["rule"] == "staged-rule" for v in result["violations"])


def test_check_proposal_reports_stakes_meta():
    result = check_proposal(CLEAN)
    assert result["stakes"] == "high"
    assert result["stakes_source"] == "default"


# -- Slice 4: pinned safe-fallback set ---------------------------------------


def test_load_safe_fallbacks_reads_versioned_set():
    rules = load_invariant_rules()
    fb = load_safe_fallbacks(rules)
    assert fb  # the shipped rules file declares a non-empty set
    assert all(e["id"] and e["replacement"] for e in fb)


def test_safe_fallback_attached_on_forbidden_path_block():
    result = check_proposal({**CLEAN, "path": "cron/jobs.py"})
    assert result["ok"] is False
    assert result["safe_fallback"]["target"] == "cron/jobs.py"
    assert result["safe_fallback"]["replacement"] == "cron/config/*.json"


def test_safe_fallback_absent_for_unpinned_block():
    result = check_proposal({**CLEAN, "path": "scripts/setup-hermes.sh"})
    assert result["ok"] is False
    # setup-hermes.sh IS in the forbidden set, and it DOES have a pinned
    # fallback in the shipped rules -> assert it is surfaced.
    assert result["safe_fallback"]["replacement"] == "config/hermes.yaml"


def test_resolve_pinned_fallback_none_when_no_blocking_path():
    assert resolve_pinned_fallback(CLEAN) is None
    assert resolve_pinned_fallback({"path": "scripts/evolution_foo.py"}) is None


# -- Slice 5 (#3518): pinned invariant core outside the agent-writable tree --


@pytest.fixture(autouse=True)
def _hermetic_pinned_surface(tmp_path, monkeypatch):
    """Hermetic pinned surface: temp copy of the shipped rules, outside any
    checkout, via $EVOLUTION_PINNED_RULES. No test touches the real file."""
    import shutil

    pinned = tmp_path / "pinned" / "evolution_invariant_rules.json"
    pinned.parent.mkdir()
    shutil.copyfile(_rules_path(), pinned)
    monkeypatch.setenv("EVOLUTION_PINNED_RULES", str(pinned))


def _pinned_rules(tmp_path, monkeypatch):
    """Install a minimal pinned rules file OUTSIDE the tree."""
    import json as _json

    pinned = tmp_path / "pinned" / "evolution_invariant_rules.json"
    pinned.parent.mkdir(exist_ok=True)
    pinned.write_text(
        _json.dumps(
            {
                "version": 9,
                "rules": [
                    {"id": "pinned-rule", "severity": "hard", "check": "human_gating"}
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EVOLUTION_PINNED_RULES", str(pinned))
    return pinned


def test_pinned_rules_win_over_rewritten_in_tree_copy(tmp_path, monkeypatch):
    # A self-edit rewrites the in-tree rules file. The filter must keep judging
    # against the PINNED copy: the rewritten in-tree rule never fires.
    import json as _json

    import evolution_invariant_filter as mod

    pinned = _pinned_rules(tmp_path, monkeypatch)
    rewritten = tmp_path / "tree" / "evolution_invariant_rules.json"
    rewritten.parent.mkdir()
    rewritten.write_text(
        _json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "id": "rewritten-rule",
                        "severity": "hard",
                        "check": "forbidden_patterns",
                        "patterns": ["harmless"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_rules_path", lambda: rewritten)

    rules = mod.load_invariant_rules()
    assert rules["version"] == 9
    assert rules["rules"][0]["id"] == "pinned-rule"
    assert rules["rules_source"] == str(pinned)

    # The in-tree rewrite would reject "harmless"; the pinned rules do not.
    result = check_proposal({**CLEAN, "delta": "harmless change"})
    assert result["ok"] is True


def test_missing_pinned_surface_fails_closed(tmp_path, monkeypatch):
    import evolution_invariant_filter as mod

    monkeypatch.setenv("EVOLUTION_PINNED_RULES", str(tmp_path / "absent.json"))
    with pytest.raises(ValueError, match="pinned invariant surface missing"):
        mod.load_invariant_rules()
    result = check_proposal(CLEAN, rules=None)
    assert result["ok"] is False
    assert any(v["rule"] == "rules-unavailable" for v in result["violations"])


def test_shadowed_pinned_surface_fails_closed(tmp_path, monkeypatch):
    # An in-tree copy sitting NEXT TO the pinned file shadows it: refuse to run.
    import evolution_invariant_filter as mod

    pinned = _pinned_rules(tmp_path, monkeypatch)
    shadow = pinned.parent / "evolution_invariant_rules.in-tree.json"
    shadow.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="shadowed"):
        mod.load_invariant_rules()
