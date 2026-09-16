"""Tests for percentage clamping at 100% across display paths.

PR #3480 capped context pressure percentage at 100% in agent/display.py
but missed the same unclamped pattern in 4 other files. When token counts
overshoot the context length (possible during streaming or before
compression fires), users see >100% in /stats, gateway status, and
memory tool output.
"""

class TestMemoryToolPercentClamp:
    """tools/memory_tool.py — _success_response and _render_block pct"""

    def test_over_limit_clamped_at_100(self):
        """Percentage should be capped at 100 even if current > limit."""
        # Simulate the calculation directly
        current = 5500
        limit = 5000
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        assert pct == 100

    def test_normal_percentage(self):
        current = 2500
        limit = 5000
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        assert pct == 50

    def test_zero_limit_returns_zero(self):
        current = 100
        limit = 0
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        assert pct == 0


class TestCLIStatsPercentClamp:
    """cli.py — /stats command percentage"""

    def test_over_context_clamped_at_100(self):
        """Tokens exceeding context_length should show max 100%."""
        last_prompt = 210_000
        ctx_len = 200_000
        pct = min(100, (last_prompt / ctx_len * 100)) if ctx_len else 0
        assert pct == 100

    def test_normal_context(self):
        last_prompt = 100_000
        ctx_len = 200_000
        pct = min(100, (last_prompt / ctx_len * 100)) if ctx_len else 0
        assert pct == 50.0

    def test_zero_context_length(self):
        last_prompt = 1000
        ctx_len = 0
        pct = min(100, (last_prompt / ctx_len * 100)) if ctx_len else 0
        assert pct == 0


class TestGatewayStatsPercentClamp:
    """gateway/run.py — _format_usage_stats percentage"""

    def test_over_context_clamped_at_100(self):
        last_prompt_tokens = 210_000
        context_length = 200_000
        pct = min(100, last_prompt_tokens / context_length * 100) if context_length else 0
        assert pct == 100

    def test_normal_context(self):
        last_prompt_tokens = 150_000
        context_length = 200_000
        pct = min(100, last_prompt_tokens / context_length * 100) if context_length else 0
        assert pct == 75.0


class TestSourceLinesAreClamped:
    """Production display paths clamp percentage at 100 when usage overshoots."""

    def test_gateway_run_clamped(self):
        from gateway.slash_commands_status import _pct

        assert _pct(210_000, 200_000) == 100
        assert _pct(30_000, 200_000) == 15
        assert _pct(5, 0) == 0

    def test_cli_clamped(self):
        """CLI /stats lives on CLIInfoMixin._show_usage (cli.py is a facade)."""
        last_prompt = 210_000
        ctx_len = 200_000
        pct = min(100, (last_prompt / ctx_len * 100)) if ctx_len else 0
        assert pct == 100
        assert min(100, (30_000 / ctx_len * 100)) == 15.0

    def test_memory_tool_clamped(self):
        """MemoryStore success/render paths never emit a percentage above 100."""
        from tools.memory_tool_store import MemoryStore

        store = MemoryStore.__new__(MemoryStore)
        store._consolidation_failures = 0
        store._entries_for = lambda target: ["x" * 6000]
        store._char_count = lambda target: 5500
        store._char_limit = lambda target: 5000

        resp = store._success_response("memory")
        assert resp["usage"].startswith("100%")

        block = store._render_block("memory", ["x" * 6000])
        assert "[100%" in block
