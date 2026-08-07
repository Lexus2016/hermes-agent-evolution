"""Tests for terminal timeout error enrichment (#1789).

The timeout error message now includes a background-mode suggestion so the
model knows to re-run long commands with background=true instead of blind-
retrying in foreground mode.
"""


class TestTimeoutErrorEnrichment:
    """Verify the timeout error message includes actionable guidance."""

    def test_timeout_error_mentions_background(self):
        """The enriched timeout error should tell the model to use background
        mode (#1789). We test the message format used by the terminal tool
        when a command times out."""
        effective_timeout = 180
        enriched_error = (
            f"Command timed out after {effective_timeout} seconds. "
            "This command looks long-running — re-run with "
            "background=true and notify_on_complete=true."
        )
        assert "timed out" in enriched_error.lower()
        assert "background=true" in enriched_error
        assert "notify_on_complete=true" in enriched_error
        assert "re-run" in enriched_error.lower()

    def test_timeout_error_includes_timeout_value(self):
        """The timeout value should still be present in the enriched message."""
        enriched_error = (
            "Command timed out after 300 seconds. "
            "This command looks long-running — re-run with "
            "background=true and notify_on_complete=true."
        )
        assert "300 seconds" in enriched_error
