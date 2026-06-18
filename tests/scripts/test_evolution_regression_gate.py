"""Tests for scripts/evolution_regression_gate.py (#296, child of #248).

The regression gate decides whether a SHIPPED harness change made its targeted
weakness cluster WORSE over the next N sessions, and — only when it grew — emits
a structured, human-gated ``regression`` issue object. The optional LLM that
authors issue prose is behind an injectable seam, so every test here runs
offline with a stub (or no seam at all). The verdict itself is fully
deterministic and needs no LLM.
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_regression_gate import (  # noqa: E402
    build_regression_issue,
    cluster_signature,
    evaluate_regression,
    find_post_ship_occurrences,
    gate,
    load_gate_input,
    main,
    target_signature,
)


# --- fixtures -------------------------------------------------------------
# Weakness records match the trace-miner schema (kind/tool/signature/...).

def _tool_failure(tool="terminal", n=9):
    return {
        "kind": "tool_failure",
        "tool": tool,
        "occurrences": n,
        "severity": n,
        "label": f"`{tool}` results look like failures {n}x — harden the wrapper.",
    }


def _provider_error(sig="429:rate_limit", n=60):
    return {
        "kind": "provider_error",
        "signature": sig,
        "occurrences": n,
        "severity": n,
        "label": f"provider error `{sig}` recurs {n}x — check fallback chain.",
    }


def _retry_spiral(tool="browser_navigate", runs=15, sessions=3):
    return {
        "kind": "retry_spiral",
        "tool": tool,
        "max_consecutive": runs,
        "sessions": sessions,
        "severity": runs,
        "label": f"`{tool}` retry spiral up to {runs} consecutive — add a fallback.",
    }


def _proposal_for(weakness, *, ptype="tool_guard", accepted_issue=301):
    """A shipped harness proposal in the shape evolution_harness_proposer emits:
    evidence carries the targeted cluster's anonymized fields verbatim."""
    evidence = {k: weakness[k] for k in (
        "kind", "tool", "signature", "occurrences", "severity", "label",
        "max_consecutive", "sessions",
    ) if k in weakness}
    return {
        "type": ptype,
        "source": "self-harness",
        "evidence": evidence,
        "accepted_issue": accepted_issue,
        "status": "proposed",
        "requires_human_review": True,
        "auto_apply": False,
    }


def _stub_llm(calls):
    """An injectable LLM seam that records its calls and returns canned prose.
    Proves the gate works WITHOUT a network and that the seam is the only place
    model output enters."""

    def _fn(verdict):
        calls.append(verdict)
        return {
            "title": f"[REGRESSION] stub for {verdict.get('signature')}",
            "summary": "stub regression summary authored by the (mocked) LLM.",
        }

    return _fn


# --- cluster identity -----------------------------------------------------

class TestClusterSignature:
    def test_tool_failure_keyed_by_kind_and_tool(self):
        assert cluster_signature(_tool_failure("terminal")) == "tool_failure:terminal"

    def test_provider_error_keyed_by_kind_and_signature(self):
        assert cluster_signature(_provider_error("429:rate_limit")) == "provider_error:429:rate_limit"

    def test_retry_spiral_keyed_by_kind_and_tool(self):
        assert cluster_signature(_retry_spiral("browser_navigate")) == "retry_spiral:browser_navigate"

    def test_malformed_records_return_none(self):
        assert cluster_signature("nope") is None
        assert cluster_signature({}) is None
        assert cluster_signature({"kind": "tool_failure"}) is None  # no subject
        assert cluster_signature({"tool": "x"}) is None  # no kind

    def test_same_failure_same_signature(self):
        # The proposal's targeted cluster and a post-ship record of the SAME
        # failure must collide so growth can be measured.
        assert cluster_signature(_tool_failure("terminal", n=9)) == \
            cluster_signature(_tool_failure("terminal", n=40))


class TestTargetSignature:
    def test_reads_signature_from_proposal_evidence(self):
        p = _proposal_for(_provider_error("429:rate_limit"))
        assert target_signature(p) == "provider_error:429:rate_limit"

    def test_falls_back_to_inline_kind_subject(self):
        # An older/inline proposal shape with kind+tool at the top level.
        assert target_signature({"kind": "tool_failure", "tool": "terminal"}) == "tool_failure:terminal"

    def test_untieable_proposal_returns_none(self):
        assert target_signature({"type": "tool_guard"}) is None
        assert target_signature("nope") is None


# --- post-ship matching ---------------------------------------------------

class TestFindPostShipOccurrences:
    def test_matches_the_right_cluster(self):
        weaknesses = [_tool_failure("terminal", n=40), _provider_error(n=5)]
        assert find_post_ship_occurrences("tool_failure:terminal", weaknesses) == 40

    def test_absent_cluster_is_zero(self):
        # Cluster fell below the miner's threshold and isn't reported -> 0.
        weaknesses = [_provider_error(n=5)]
        assert find_post_ship_occurrences("tool_failure:terminal", weaknesses) == 0

    def test_retry_spiral_uses_max_consecutive(self):
        weaknesses = [_retry_spiral("browser_navigate", runs=20)]
        assert find_post_ship_occurrences("retry_spiral:browser_navigate", weaknesses) == 20

    def test_non_dict_entries_ignored(self):
        weaknesses = [_tool_failure("terminal", n=12), "x", 5, None]
        assert find_post_ship_occurrences("tool_failure:terminal", weaknesses) == 12


# --- the verdict (deterministic core) -------------------------------------

class TestEvaluateRegression:
    def test_cluster_grew_is_a_regression(self):
        p = _proposal_for(_tool_failure("terminal"))
        v = evaluate_regression(p, baseline_occurrences=9,
                                post_ship_weaknesses=[_tool_failure("terminal", n=14)], sessions=20)
        assert v["regressed"] is True
        assert v["reason"] == "cluster_grew"
        assert v["signature"] == "tool_failure:terminal"
        assert v["baseline_occurrences"] == 9
        assert v["post_ship_occurrences"] == 14
        assert v["delta"] == 5

    def test_cluster_shrank_is_not_a_regression(self):
        p = _proposal_for(_tool_failure("terminal"))
        v = evaluate_regression(p, baseline_occurrences=9,
                                post_ship_weaknesses=[_tool_failure("terminal", n=3)], sessions=20)
        assert v["regressed"] is False
        assert v["delta"] == -6

    def test_cluster_vanished_is_not_a_regression(self):
        # The harness change worked: the cluster dropped below threshold and is
        # absent from post-ship records (count 0 < baseline).
        p = _proposal_for(_provider_error("429:rate_limit"))
        v = evaluate_regression(p, baseline_occurrences=60, post_ship_weaknesses=[], sessions=20)
        assert v["regressed"] is False
        assert v["post_ship_occurrences"] == 0
        assert v["delta"] == -60

    def test_equal_count_is_not_a_regression(self):
        # Held the line — strictly-greater is the bar, so equal is NOT flagged.
        p = _proposal_for(_tool_failure("terminal"))
        v = evaluate_regression(p, baseline_occurrences=9,
                                post_ship_weaknesses=[_tool_failure("terminal", n=9)], sessions=20)
        assert v["regressed"] is False
        assert v["delta"] == 0

    def test_untieable_proposal_never_guesses(self):
        v = evaluate_regression({"type": "tool_guard"}, baseline_occurrences=9,
                                post_ship_weaknesses=[_tool_failure("terminal", n=99)], sessions=20)
        assert v["regressed"] is False
        assert v["reason"] == "no_target_signature"
        assert v["signature"] is None

    def test_only_the_targeted_cluster_counts(self):
        # A DIFFERENT cluster growing must not flag THIS proposal as a regression.
        p = _proposal_for(_tool_failure("terminal"))
        v = evaluate_regression(p, baseline_occurrences=9,
                                post_ship_weaknesses=[_provider_error(n=500)], sessions=20)
        assert v["regressed"] is False
        assert v["post_ship_occurrences"] == 0


# --- the regression issue object ------------------------------------------

class TestBuildRegressionIssue:
    def test_no_issue_when_not_regressed(self):
        v = {"regressed": False, "signature": "tool_failure:terminal"}
        assert build_regression_issue(v, _proposal_for(_tool_failure())) is None

    def test_offline_envelope_without_llm(self):
        v = evaluate_regression(_proposal_for(_tool_failure("terminal")), 9,
                                [_tool_failure("terminal", n=14)], sessions=20)
        issue = build_regression_issue(v, _proposal_for(_tool_failure("terminal")))
        assert issue is not None
        assert issue["kind"] == "regression"
        assert issue["source"] == "self-harness"
        assert issue["llm_authored"] is False
        assert issue["title"].startswith("[REGRESSION]")
        assert "tool_failure:terminal" in issue["title"]
        # Body carries the deterministic evidence.
        assert "Delta: +5" in issue["body"]
        assert "#301" in issue["body"]  # back-reference to the accepted proposal
        assert issue["evidence"]["delta"] == 5

    def test_llm_seam_authors_prose_and_is_called(self):
        calls = []
        v = evaluate_regression(_proposal_for(_provider_error("429:rate_limit")), 60,
                                [_provider_error("429:rate_limit", n=80)], sessions=30)
        issue = build_regression_issue(v, _proposal_for(_provider_error("429:rate_limit")),
                                       llm=_stub_llm(calls))
        assert issue is not None
        assert len(calls) == 1
        assert calls[0]["signature"] == "provider_error:429:rate_limit"
        assert issue["llm_authored"] is True
        assert issue["title"] == "[REGRESSION] stub for provider_error:429:rate_limit"
        assert issue["body"].startswith("stub regression summary")

    def test_llm_returning_garbage_degrades_to_envelope(self):
        def _bad_llm(_v):
            return ["not", "a", "dict"]

        v = evaluate_regression(_proposal_for(_retry_spiral()), 5,
                                [_retry_spiral(runs=12)], sessions=20)
        issue = build_regression_issue(v, _proposal_for(_retry_spiral()), llm=_bad_llm)
        assert issue is not None
        assert issue["llm_authored"] is False
        assert issue["title"].startswith("[REGRESSION]")

    def test_llm_raising_is_caught_and_recorded(self):
        def _boom(_v):
            raise RuntimeError("network down")

        v = evaluate_regression(_proposal_for(_tool_failure()), 9,
                                [_tool_failure(n=20)], sessions=20)
        issue = build_regression_issue(v, _proposal_for(_tool_failure()), llm=_boom)
        assert issue is not None
        assert issue["llm_error"] == "network down"
        assert issue["llm_authored"] is False  # degraded, not crashed

    def test_evidence_carries_only_anonymized_counts(self):
        # The issue's evidence is counts/signature/delta — never raw trace text.
        w = _tool_failure("terminal", n=14)
        w["raw_trace"] = "secret user prompt that must NOT leak"
        p = _proposal_for(w)
        v = evaluate_regression(p, 9, [w], sessions=20)
        issue = build_regression_issue(v, p)
        assert issue is not None
        assert "raw_trace" not in json.dumps(issue)


# --- HARD safety invariant: human-visible, never auto-reverted ------------

class TestHumanGatingNoAutoRevert:
    def test_emitted_issue_is_inert_and_human_gated(self):
        v = evaluate_regression(_proposal_for(_tool_failure()), 9,
                                [_tool_failure(n=20)], sessions=20)
        issue = build_regression_issue(v, _proposal_for(_tool_failure()))
        assert issue is not None
        assert issue["status"] == "proposed"
        assert issue["requires_human_review"] is True
        assert issue["auto_revert"] is False

    def test_module_exposes_no_revert_or_apply_path(self):
        # The gate MUST NOT ship any function that reverts/applies harness state.
        # Guard against a future regression that adds one.
        import evolution_regression_gate as mod

        forbidden = ("revert", "rollback", "apply", "write_prompt", "mutate",
                     "patch_config", "install", "rewrite", "commit", "undo")
        names = [n for n in dir(mod) if not n.startswith("_")]
        for n in names:
            low = n.lower()
            for bad in forbidden:
                assert bad not in low, f"forbidden revert/apply-like symbol exported: {n}"

    def test_cli_echoes_no_auto_revert_flag(self, tmp_path, capsys):
        gate_input = {
            "proposal": _proposal_for(_tool_failure("terminal")),
            "baseline_occurrences": 9,
            "weaknesses": [_tool_failure("terminal", n=14)],
            "sessions": 20,
        }
        p = tmp_path / "gate-input.json"
        p.write_text(json.dumps(gate_input), encoding="utf-8")
        rc = main(["evolution_regression_gate.py", str(p)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["human_gated"] is True
        assert out["auto_revert"] is False
        assert out["issue"]["auto_revert"] is False


# --- end-to-end gate ------------------------------------------------------

class TestGate:
    def test_regression_yields_verdict_and_issue(self):
        p = _proposal_for(_tool_failure("terminal"))
        result = gate(p, 9, [_tool_failure("terminal", n=14)], sessions=20)
        assert result["regressed"] is True
        assert result["verdict"]["delta"] == 5
        assert result["issue"] is not None
        assert result["issue"]["kind"] == "regression"

    def test_no_regression_yields_no_issue(self):
        p = _proposal_for(_tool_failure("terminal"))
        result = gate(p, 9, [_tool_failure("terminal", n=2)], sessions=20)
        assert result["regressed"] is False
        assert result["issue"] is None


# --- loader ---------------------------------------------------------------

class TestLoadGateInput:
    def test_canonical_object(self):
        payload = {
            "proposal": _proposal_for(_tool_failure("terminal")),
            "baseline_occurrences": 9,
            "weaknesses": [_tool_failure("terminal", n=14)],
            "sessions": 20,
        }
        proposal, baseline, weaknesses, sessions = load_gate_input(payload)
        assert baseline == 9 and sessions == 20 and len(weaknesses) == 1
        assert proposal["type"] == "tool_guard"

    def test_baseline_falls_back_to_proposal_evidence(self):
        # No top-level baseline -> read the count the proposal recorded at ship.
        payload = {
            "proposal": _proposal_for(_tool_failure("terminal", n=9)),
            "weaknesses": [_tool_failure("terminal", n=14)],
        }
        _proposal, baseline, _w, _s = load_gate_input(payload)
        assert baseline == 9

    def test_garbage_degrades_to_safe_defaults(self):
        proposal, baseline, weaknesses, sessions = load_gate_input(42)
        assert proposal == {} and baseline == 0 and weaknesses == [] and sessions == 0

    def test_non_dict_weaknesses_dropped(self):
        payload = {"proposal": {}, "baseline_occurrences": 1,
                   "weaknesses": [_tool_failure(), "x", 5, None]}
        _p, _b, weaknesses, _s = load_gate_input(payload)
        assert len(weaknesses) == 1


# --- CLI ------------------------------------------------------------------

class TestMainCLI:
    def test_file_input_flags_regression(self, tmp_path, capsys):
        gate_input = {
            "proposal": _proposal_for(_provider_error("429:rate_limit")),
            "baseline_occurrences": 60,
            "weaknesses": [_provider_error("429:rate_limit", n=90)],
            "sessions": 30,
        }
        p = tmp_path / "in.json"
        p.write_text(json.dumps(gate_input), encoding="utf-8")
        rc = main(["evolution_regression_gate.py", str(p)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["regressed"] is True
        assert out["source"] == "self-harness"
        assert out["issue"]["kind"] == "regression"
        assert out["verdict"]["delta"] == 30

    def test_file_input_no_regression_emits_null_issue(self, tmp_path, capsys):
        gate_input = {
            "proposal": _proposal_for(_tool_failure("terminal")),
            "baseline_occurrences": 9,
            "weaknesses": [],  # cluster vanished
            "sessions": 30,
        }
        p = tmp_path / "in.json"
        p.write_text(json.dumps(gate_input), encoding="utf-8")
        rc = main(["evolution_regression_gate.py", str(p)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["regressed"] is False
        assert out["issue"] is None

    def test_stdin_input(self, monkeypatch, capsys):
        gate_input = {
            "proposal": _proposal_for(_tool_failure("terminal")),
            "baseline_occurrences": 9,
            "weaknesses": [_tool_failure("terminal", n=14)],
            "sessions": 20,
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(gate_input)))
        rc = main(["evolution_regression_gate.py"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["regressed"] is True

    def test_bad_json_exit_2(self, tmp_path, capsys):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        rc = main(["evolution_regression_gate.py", str(p)])
        assert rc == 2
        assert "not valid JSON" in capsys.readouterr().err

    def test_unknown_flag_exit_2(self, capsys):
        rc = main(["evolution_regression_gate.py", "--bogus"])
        assert rc == 2
        assert "unknown flag" in capsys.readouterr().err

    def test_missing_file_exit_2(self, tmp_path, capsys):
        rc = main(["evolution_regression_gate.py", str(tmp_path / "nope.json")])
        assert rc == 2
        assert "cannot read input" in capsys.readouterr().err

    def test_empty_payload_no_regression(self, tmp_path, capsys):
        p = tmp_path / "empty.json"
        p.write_text(json.dumps({}), encoding="utf-8")
        rc = main(["evolution_regression_gate.py", str(p)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["regressed"] is False and out["issue"] is None
