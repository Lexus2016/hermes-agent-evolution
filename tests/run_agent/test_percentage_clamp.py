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
    """Verify the actual source files have min(100, ...) applied."""

    @staticmethod
    def _read_file(rel_path: str) -> str:
        import os
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        with open(os.path.join(base, rel_path)) as f:
            return f.read()

    def test_gateway_run_clamped(self):
        # The /usage stats handler was extracted from gateway/run.py into
        # gateway/slash_commands.py (god-file decomposition Phase 3b).
        src = self._read_file("gateway/slash_commands.py")
        # Check that the stats handler clamps the context pct with min(100, ...).
        # Assert the clamp intent, not a specific local name (the occupancy
        # value is read into a clamped `_lpt` local, #50421).
        assert "min(100, _lpt / ctx.context_length" in src, (
            "gateway/slash_commands.py stats pct is not clamped with min(100, ...)"
        )

    def test_cli_clamped(self):
        src = self._read_file("cli.py")
        assert "min(100, (last_prompt" in src, (
            "cli.py /stats pct is not clamped with min(100, ...)"
        )

    def test_memory_tool_clamped(self):
        """Every user-facing percentage in memory_tool.py must be clamped <=100.

        The invariant under guard: no display path can ever emit a percentage
        above 100% (token/char counts can transiently overshoot the limit
        during streaming or before compaction fires). The original guard
        hard-coded the literal ``min(100, int((current / limit)`` and counted
        occurrences, which silently broke when #537 renamed the local
        ``limit`` -> ``effective_limit`` in ``_success_response`` for the
        per-apply_batch override (the clamp was preserved, only the variable
        name changed). We now assert the real invariant directly: every
        ``... * 100`` percentage expression is wrapped in a ``min(100, ...)``
        clamp, which is refactor-resilient and strictly stronger than a
        literal-line count.
        """
        import re

        src = self._read_file("tools/memory_tool.py")

        # Find every percentage expression: an ``int(...)`` cast that scales a
        # ratio by 100 (the display-pct idiom in this file, e.g.
        # ``int((current / limit) * 100)``). Each one MUST be immediately
        # preceded by ``min(100, `` so the result can never exceed 100. We
        # anchor on a single ``int(`` (so the standard single-paren form
        # ``int(current / limit * 100)`` is also guarded, not only the
        # double-paren idiom) and accept the ``* 100`` factor in either order.
        # The match deliberately stops at the ``100`` factor (not the closing
        # parens) so it captures both the clamped form (``... * 100))``) and a
        # hypothetical unclamped regression (``... * 100)``).
        pct_exprs = list(
            re.finditer(r"int\((?:[^\n]*?\*\s*100|[^\n]*?100\s*\*[^\n]*?)", src)
        )
        assert pct_exprs, (
            "expected at least one ``int(... * 100`` percentage expression "
            "in memory_tool.py — has the display format changed?"
        )

        unclamped = [
            src[m.start():m.start() + 40]
            for m in pct_exprs
            if not src[max(0, m.start() - 9):m.start()].endswith("min(100, ")
        ]
        assert not unclamped, (
            "memory_tool.py has unclamped percentage expression(s) that can "
            f"emit >100%: {unclamped}. Wrap each in min(100, ...)."
        )

        # Secondary sanity check: the two original distinct display sites
        # (_success_response + _render_block, plus _compact's usage line) are
        # still present. Count the shared clamp prefix rather than a single
        # variable-specific literal so a future rename does not break this.
        clamp_sites = src.count("min(100, int((")
        assert clamp_sites >= 2, (
            f"memory_tool.py has only {clamp_sites} clamped pct site(s), "
            "expected >= 2 (the success-response and render-block displays)"
        )
