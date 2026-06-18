"""Tests for scripts/evolution_harness_proposer.py (#295, child of #248).

The harness proposer turns trace-miner weakness records into structured,
human-gated harness proposals. The LLM call is behind an injectable seam, so
every test here runs offline with a stub (or no seam at all).
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_harness_proposer import (  # noqa: E402
    KIND_TO_TYPE,
    PROPOSAL_TYPES,
    build_proposal,
    generate_proposals,
    load_weaknesses,
    main,
    proposal_type_for,
)


# --- fixtures -------------------------------------------------------------

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


def _stub_llm(calls):
    """An injectable LLM seam that records its calls and returns canned fields.
    Proves the generator works WITHOUT a network and that the seam is the only
    place model output enters."""

    def _fn(weakness, ptype):
        calls.append((weakness, ptype))
        return {
            "title": f"[HARNESS] stub for {weakness.get('kind')}",
            "delta": "ADD: stub harness delta authored by the (mocked) LLM.",
            "rationale": "stub rationale",
        }

    return _fn


# --- type mapping ---------------------------------------------------------

class TestProposalTypeFor:
    def test_known_kinds_map_to_constrained_types(self):
        assert proposal_type_for(_tool_failure()) == "tool_guard"
        assert proposal_type_for(_provider_error()) == "retry_policy_change"
        assert proposal_type_for(_retry_spiral()) == "retry_policy_change"

    def test_every_mapped_type_is_in_the_constrained_vocabulary(self):
        for t in KIND_TO_TYPE.values():
            assert t in PROPOSAL_TYPES

    def test_unknown_kind_returns_none(self):
        assert proposal_type_for({"kind": "totally_new_kind"}) is None

    def test_non_dict_and_missing_kind_return_none(self):
        assert proposal_type_for("not-a-dict") is None
        assert proposal_type_for({"no": "kind"}) is None
        assert proposal_type_for({"kind": 123}) is None


# --- single proposal build ------------------------------------------------

class TestBuildProposal:
    def test_offline_envelope_without_llm(self):
        p = build_proposal(_tool_failure())
        assert p is not None
        assert p["type"] == "tool_guard"
        assert p["llm_authored"] is False
        assert p["delta"] == ""  # no model -> no authored delta
        assert p["title"].startswith("[HARNESS]")
        # rationale falls back to the miner's label (no model).
        assert "harden" in p["rationale"]

    def test_llm_seam_authors_fields_and_is_called(self):
        calls = []
        p = build_proposal(_provider_error(), llm=_stub_llm(calls))
        assert p is not None
        assert len(calls) == 1
        weakness, ptype = calls[0]
        assert ptype == "retry_policy_change"
        assert weakness["signature"] == "429:rate_limit"
        assert p["llm_authored"] is True
        assert p["delta"].startswith("ADD:")
        assert p["title"] == "[HARNESS] stub for provider_error"

    def test_unknown_kind_is_dropped(self):
        assert build_proposal({"kind": "mystery", "severity": 99}) is None

    def test_non_dict_weakness_is_dropped(self):
        assert build_proposal("nope") is None

    def test_evidence_carries_only_anonymized_fields(self):
        # Even if a (malformed) record sneaks an unexpected key, evidence keeps
        # only the whitelisted anonymized fields — never raw content.
        w = dict(_tool_failure())
        w["raw_trace"] = "secret user prompt that must NOT leak"
        p = build_proposal(w)
        assert p is not None
        assert "raw_trace" not in p["evidence"]
        assert "raw_trace" not in json.dumps(p)
        assert p["evidence"]["tool"] == "terminal"
        assert p["evidence"]["occurrences"] == 9

    def test_llm_returning_garbage_degrades_to_envelope(self):
        # A model reply that is not a dict must not crash; fall back to defaults.
        def _bad_llm(_w, _t):
            return ["not", "a", "dict"]

        p = build_proposal(_retry_spiral(), llm=_bad_llm)
        assert p is not None
        assert p["llm_authored"] is False
        assert p["delta"] == ""
        assert p["title"].startswith("[HARNESS]")

    def test_llm_raising_is_caught_and_recorded(self):
        def _boom(_w, _t):
            raise RuntimeError("network down")

        p = build_proposal(_tool_failure(), llm=_boom)
        assert p is not None
        assert p["llm_error"] == "network down"
        assert p["llm_authored"] is False  # degraded, not crashed


# --- HARD safety invariant: human-gated, never auto-applied ---------------

class TestHumanGatingInvariant:
    def test_every_proposal_is_inert_and_human_gated(self):
        weaknesses = [_tool_failure(), _provider_error(), _retry_spiral()]
        for p in generate_proposals(weaknesses, llm=_stub_llm([])):
            assert p["status"] == "proposed"
            assert p["requires_human_review"] is True
            assert p["auto_apply"] is False

    def test_module_exposes_no_apply_path(self):
        # The generator MUST NOT ship any function that applies/mutates harness
        # state. Guard against a future regression that adds one.
        import evolution_harness_proposer as mod

        forbidden = ("apply", "write_prompt", "mutate", "patch_config",
                     "install", "rewrite", "commit")
        names = [n for n in dir(mod) if not n.startswith("_")]
        for n in names:
            low = n.lower()
            for bad in forbidden:
                assert bad not in low, f"forbidden apply-like symbol exported: {n}"

    def test_cli_output_echoes_human_gated_flag(self, tmp_path, capsys):
        sidecar = {"weaknesses": [_tool_failure()]}
        p = tmp_path / "weaknesses-latest.json"
        p.write_text(json.dumps(sidecar), encoding="utf-8")
        rc = main(["evolution_harness_proposer.py", str(p)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["human_gated"] is True
        assert all(pr["auto_apply"] is False for pr in out["proposals"])


# --- batch generation -----------------------------------------------------

class TestGenerateProposals:
    def test_one_proposal_per_valid_weakness_order_preserved(self):
        weaknesses = [_provider_error(n=60), _retry_spiral(), _tool_failure()]
        props = generate_proposals(weaknesses)
        assert len(props) == 3
        # Order follows input (miner already sorts by severity desc).
        assert props[0]["type"] == "retry_policy_change"
        assert props[0]["evidence"]["signature"] == "429:rate_limit"
        assert props[2]["type"] == "tool_guard"

    def test_malformed_and_unknown_records_dropped(self):
        weaknesses = [_tool_failure(), {"kind": "unknown"}, "not-a-dict", {}]
        props = generate_proposals(weaknesses)
        assert len(props) == 1
        assert props[0]["type"] == "tool_guard"

    def test_empty_input_yields_no_proposals(self):
        assert generate_proposals([]) == []


# --- loader ---------------------------------------------------------------

class TestLoadWeaknesses:
    def test_accepts_full_sidecar_object(self):
        payload = {"window_days": 7, "weaknesses": [_tool_failure(), _provider_error()]}
        recs = load_weaknesses(payload)
        assert len(recs) == 2

    def test_accepts_bare_list(self):
        recs = load_weaknesses([_tool_failure()])
        assert len(recs) == 1

    def test_non_dict_entries_dropped(self):
        recs = load_weaknesses([_tool_failure(), "x", 5, None])
        assert len(recs) == 1

    def test_unrecognized_shape_yields_empty(self):
        assert load_weaknesses(42) == []
        assert load_weaknesses({"weaknesses": "not-a-list"}) == []


# --- CLI ------------------------------------------------------------------

class TestMainCLI:
    def test_file_input_emits_proposals(self, tmp_path, capsys):
        sidecar = {"weaknesses": [_tool_failure(), _provider_error()]}
        p = tmp_path / "w.json"
        p.write_text(json.dumps(sidecar), encoding="utf-8")
        rc = main(["evolution_harness_proposer.py", str(p)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["count"] == 2
        assert out["source"] == "self-harness"

    def test_stdin_input(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([_tool_failure()])))
        rc = main(["evolution_harness_proposer.py"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["count"] == 1

    def test_bad_json_exit_2(self, tmp_path, capsys):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        rc = main(["evolution_harness_proposer.py", str(p)])
        assert rc == 2
        assert "not valid JSON" in capsys.readouterr().err

    def test_unknown_flag_exit_2(self, capsys):
        rc = main(["evolution_harness_proposer.py", "--bogus"])
        assert rc == 2
        assert "unknown flag" in capsys.readouterr().err

    def test_missing_file_exit_2(self, tmp_path, capsys):
        rc = main(["evolution_harness_proposer.py", str(tmp_path / "nope.json")])
        assert rc == 2
        assert "cannot read input" in capsys.readouterr().err

    def test_empty_weaknesses_yields_zero_proposals(self, tmp_path, capsys):
        p = tmp_path / "empty.json"
        p.write_text(json.dumps({"weaknesses": []}), encoding="utf-8")
        rc = main(["evolution_harness_proposer.py", str(p)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["count"] == 0 and out["proposals"] == []
