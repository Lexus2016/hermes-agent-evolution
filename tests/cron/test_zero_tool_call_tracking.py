"""Tests for #701: persist the agent's tool-call count per cron run so the
deterministic watchdog can tell a legitimately idle stage (ran clean, used
tools, nothing to do) from a silently no-op'd one (ran "clean" but could not
call a single tool — broken/missing toolset)."""

from cron.scheduler import _count_tool_calls


class TestCountToolCalls:
    def test_counts_tool_role_messages(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "ok"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "2"}]},
            {"role": "tool", "tool_call_id": "2", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        assert _count_tool_calls(messages) == 2

    def test_zero_for_talk_only_run(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "answer"},
        ]
        assert _count_tool_calls(messages) == 0

    def test_tolerates_junk(self):
        assert _count_tool_calls(None) == 0
        assert _count_tool_calls([]) == 0
        assert _count_tool_calls(["garbage", 42, {"role": "tool"}]) == 1


class TestMarkJobRunPersistsToolCalls:
    def test_tool_calls_persisted_on_success(self):
        from cron.jobs import create_job, get_job, mark_job_run

        job = create_job(prompt="p", schedule="0 9 * * *", name="tc-test")
        mark_job_run(job["id"], success=True, tool_calls=0)
        assert get_job(job["id"])["last_tool_calls"] == 0

        mark_job_run(job["id"], success=True, tool_calls=5)
        assert get_job(job["id"])["last_tool_calls"] == 5

    def test_defaults_to_none_when_not_provided(self):
        from cron.jobs import create_job, get_job, mark_job_run

        job = create_job(prompt="p", schedule="0 9 * * *", name="tc-default")
        mark_job_run(job["id"], success=True)
        assert get_job(job["id"])["last_tool_calls"] is None
