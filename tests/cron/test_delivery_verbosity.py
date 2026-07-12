"""Tests for per-cron-job delivery verbosity (issue #924).

Covers the ``delivery_verbosity`` job field end-to-end: schema normalization in
``cron/jobs.py``, the resolver/transform helpers in ``cron/scheduler.py``, and
integration through ``_deliver_result`` for full / result_only / summary /
silent — including the guarantee that error deliveries are never transformed or
suppressed.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cron.jobs import (
    create_job,
    get_job,
    _normalize_delivery_verbosity,
    DELIVERY_VERBOSITY_LEVELS,
)
from cron.scheduler import (
    _deliver_result,
    _resolve_delivery_verbosity,
    _apply_delivery_verbosity,
    _summarize_job_result,
)


@pytest.fixture
def tmp_cron_dir(tmp_path, monkeypatch):
    """Isolate the on-disk cron job store to a tempdir."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


# ---------------------------------------------------------------------------
# Schema normalization (cron/jobs.py)
# ---------------------------------------------------------------------------

class TestNormalizeDeliveryVerbosity:
    def test_levels_exact(self):
        assert DELIVERY_VERBOSITY_LEVELS == ("full", "result_only", "summary", "silent")

    @pytest.mark.parametrize("level", DELIVERY_VERBOSITY_LEVELS)
    def test_valid_levels_pass(self, level):
        assert _normalize_delivery_verbosity(level) == level
        assert _normalize_delivery_verbosity(level.upper()) == level
        assert _normalize_delivery_verbosity(f"  {level} ") == level

    def test_none_and_empty(self):
        assert _normalize_delivery_verbosity(None) is None
        assert _normalize_delivery_verbosity("") is None
        assert _normalize_delivery_verbosity("   ") is None

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _normalize_delivery_verbosity("loud")


class TestCreateJobPersistence:
    def test_persists_when_set(self, tmp_cron_dir):
        job = create_job(
            prompt="check mail",
            schedule="15m",
            deliver="local",
            delivery_verbosity="result_only",
        )
        assert job["delivery_verbosity"] == "result_only"
        assert get_job(job["id"])["delivery_verbosity"] == "result_only"

    def test_absent_by_default(self, tmp_cron_dir):
        # Back-compat: omitted → key not persisted → readers default to "full".
        job = create_job(prompt="x", schedule="15m", deliver="local")
        assert "delivery_verbosity" not in job

    def test_invalid_rejected_at_create(self, tmp_cron_dir):
        with pytest.raises(ValueError):
            create_job(prompt="x", schedule="15m", deliver="local", delivery_verbosity="bogus")


# ---------------------------------------------------------------------------
# _resolve_delivery_verbosity — resolution order
# ---------------------------------------------------------------------------

class TestResolveDeliveryVerbosity:
    def test_per_job_wins(self):
        assert _resolve_delivery_verbosity({"delivery_verbosity": "summary"}, ["chat"], {}) == "summary"

    def test_per_job_invalid_falls_through(self):
        # A bogus per-job value is ignored, falling to the default.
        assert _resolve_delivery_verbosity({"delivery_verbosity": "bogus"}, ["chat"], {}) == "full"

    def test_chat_quiet_implies_result_only(self):
        cfg = {"display": {"chat_overrides": {"g": {"mode": "quiet"}}}}
        assert _resolve_delivery_verbosity({}, ["g"], cfg) == "result_only"

    def test_chat_quiet_via_quiet_chats(self):
        cfg = {"display": {"quiet_chats": ["g"]}}
        assert _resolve_delivery_verbosity({}, ["g"], cfg) == "result_only"

    def test_chat_silent_implies_silent(self):
        cfg = {"display": {"chat_overrides": {"s": {"mode": "silent"}}}}
        assert _resolve_delivery_verbosity({}, ["s"], cfg) == "silent"

    def test_chat_verbose_and_normal_are_full(self):
        cfg = {"display": {"chat_overrides": {"v": {"mode": "verbose"}, "n": {"mode": "normal"}}}}
        assert _resolve_delivery_verbosity({}, ["v"], cfg) == "full"
        assert _resolve_delivery_verbosity({}, ["n"], cfg) == "full"

    def test_per_job_overrides_chat(self):
        cfg = {"display": {"chat_overrides": {"s": {"mode": "silent"}}}}
        # Explicit per-job full beats a silent target chat.
        assert _resolve_delivery_verbosity({"delivery_verbosity": "full"}, ["s"], cfg) == "full"

    def test_default_full(self):
        assert _resolve_delivery_verbosity({}, ["unknown"], {}) == "full"
        assert _resolve_delivery_verbosity({}, [], {}) == "full"

    def test_none_chat_id_skipped(self):
        assert _resolve_delivery_verbosity({}, [None], {}) == "full"

    def test_multi_target_most_restrictive_wins(self):
        # Fan-out sharing ONE content: a verbose first target must NOT leak the
        # full trace to a silent/quiet later target.
        cfg = {
            "display": {
                "chat_overrides": {
                    "v": {"mode": "verbose"},
                    "s": {"mode": "silent"},
                    "q": {"mode": "quiet"},
                }
            }
        }
        # verbose + silent → don't suppress the verbose target, don't leak to
        # the silent one → result_only (final answer everywhere).
        assert _resolve_delivery_verbosity({}, ["v", "s"], cfg) == "result_only"
        # verbose + quiet → result_only.
        assert _resolve_delivery_verbosity({}, ["v", "q"], cfg) == "result_only"

    def test_multi_target_all_silent_suppresses(self):
        cfg = {"display": {"chat_overrides": {"s1": {"mode": "silent"}, "s2": {"mode": "silent"}}}}
        assert _resolve_delivery_verbosity({}, ["s1", "s2"], cfg) == "silent"

    def test_multi_target_silent_plus_unconfigured_degrades_not_suppresses(self):
        # A silent target mixed with an unconfigured one must still deliver
        # (result_only) to the unconfigured target — never suppress it.
        cfg = {"display": {"chat_overrides": {"s": {"mode": "silent"}}}}
        assert _resolve_delivery_verbosity({}, ["s", "unconfigured"], cfg) == "result_only"


# ---------------------------------------------------------------------------
# _apply_delivery_verbosity — content transform & suppression
# ---------------------------------------------------------------------------

class TestApplyDeliveryVerbosity:
    def test_full_unchanged(self):
        assert _apply_delivery_verbosity("full", "body", success=True) == "body"

    def test_result_only_strips_reasoning(self):
        out = _apply_delivery_verbosity("result_only", "<think>secret</think>\nFinal answer", success=True)
        assert out == "Final answer"

    def test_summary_truncates(self):
        out = _apply_delivery_verbosity("summary", "A" * 400, success=True)
        assert out.endswith("…")
        assert len(out) <= 281

    def test_silent_suppresses_on_success(self):
        assert _apply_delivery_verbosity("silent", "body", success=True) is None

    def test_error_delivery_never_transformed(self):
        # success=False → every level passes the content through untouched.
        for level in DELIVERY_VERBOSITY_LEVELS:
            assert _apply_delivery_verbosity(level, "ERR trace", success=False) == "ERR trace"

    def test_summarize_helper_empty_safe(self):
        assert _summarize_job_result("") == ""
        assert _summarize_job_result("   ") == ""

    def test_summarize_short_content_unchanged(self):
        assert _summarize_job_result("short") == "short"


# ---------------------------------------------------------------------------
# Integration through _deliver_result
# ---------------------------------------------------------------------------

def _telegram_gateway_cfg():
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    pconfig.extra = {}
    mock_cfg = MagicMock()
    mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
    return mock_cfg


def _sent_text(send_mock):
    return send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][-1]


class TestDeliverResultVerbosityIntegration:
    JOB = {
        "id": "job1",
        "name": "mail-check",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }

    def _run(self, job, content, user_cfg, *, success=True):
        with patch("gateway.config.load_gateway_config", return_value=_telegram_gateway_cfg()), \
             patch("cron.scheduler.load_config", return_value=user_cfg), \
             patch(
                 "tools.send_message_tool._send_to_platform",
                 new=AsyncMock(return_value={"success": True}),
             ) as send_mock:
            result = _deliver_result(job, content, success=success)
        return result, send_mock

    def test_full_delivers_whole_content(self):
        job = {**self.JOB, "delivery_verbosity": "full"}
        _, send = self._run(job, "line1\nline2", {"cron": {"wrap_response": False}})
        assert _sent_text(send) == "line1\nline2"

    def test_result_only_strips_trace(self):
        job = {**self.JOB, "delivery_verbosity": "result_only"}
        _, send = self._run(
            job, "<think>reasoning</think>\nThe result", {"cron": {"wrap_response": False}}
        )
        assert _sent_text(send) == "The result"

    def test_summary_truncates(self):
        job = {**self.JOB, "delivery_verbosity": "summary"}
        _, send = self._run(job, "B" * 400, {"cron": {"wrap_response": False}})
        sent = _sent_text(send)
        assert sent.endswith("…") and len(sent) <= 281

    def test_silent_suppresses_delivery_on_success(self):
        job = {**self.JOB, "delivery_verbosity": "silent"}
        result, send = self._run(job, "quiet result", {"cron": {"wrap_response": False}})
        assert result is None
        send.assert_not_called()

    def test_silent_still_delivers_on_failure(self):
        job = {**self.JOB, "delivery_verbosity": "silent"}
        _, send = self._run(
            job, "ERROR: job blew up", {"cron": {"wrap_response": False}}, success=False
        )
        send.assert_called_once()
        assert _sent_text(send) == "ERROR: job blew up"

    def test_per_chat_quiet_target_forces_result_only(self):
        # No per-job verbosity; the delivery target chat is mode: quiet.
        cfg = {
            "cron": {"wrap_response": False},
            "display": {"chat_overrides": {"123": {"mode": "quiet"}}},
        }
        _, send = self._run(self.JOB, "<think>x</think>\nJust the answer", cfg)
        assert _sent_text(send) == "Just the answer"

    def test_per_chat_silent_target_suppresses(self):
        cfg = {
            "cron": {"wrap_response": False},
            "display": {"chat_overrides": {"123": {"mode": "silent"}}},
        }
        result, send = self._run(self.JOB, "nothing to see", cfg)
        assert result is None
        send.assert_not_called()

    def test_no_verbosity_is_unchanged_default(self):
        # No per-job field, no chat override → full (byte-identical to before).
        _, send = self._run(self.JOB, "plain output", {"cron": {"wrap_response": False}})
        assert _sent_text(send) == "plain output"
