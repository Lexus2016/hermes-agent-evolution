"""Tests for the /verify command's model-diverse verification wiring (#909).

Adversarial verification (#825) is only valuable if the verifier isn't
sharing the generator's blind spots. _handle_verify_command now routes the
verifier call through call_llm(task="adversarial_verification", ...), which
honors auxiliary.adversarial_verification.{provider,model} — a config seam
that lets the verifier run on a different model/provider than the one that
produced the solution being checked — and warns the user when it can tell
verification is running same-family.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


_APPROVED_JSON = (
    '```json\n{"verdict": "approved", "confidence": 0.9, '
    '"summary": "ok", "issues": []}\n```'
)


class TestHandleVerifyCommand(unittest.TestCase):
    def _make_cli(self, *, agent_model="anthropic/claude-opus-4.8"):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli.conversation_history = [
            {"role": "user", "content": "please implement X"},
            {"role": "assistant", "content": "here is my solution"},
        ]
        cli.agent = SimpleNamespace(model=agent_model)
        cli._console_print = MagicMock()
        return cli

    @patch("agent.auxiliary_client.call_llm")
    def test_calls_call_llm_with_adversarial_verification_task(self, mock_call_llm):
        mock_call_llm.return_value = _make_response(_APPROVED_JSON)
        cli = self._make_cli()

        with patch.dict("cli.CLI_CONFIG", {"auxiliary": {}}, clear=False):
            cli._handle_verify_command()

        self.assertEqual(mock_call_llm.call_count, 1)
        _, kwargs = mock_call_llm.call_args
        self.assertEqual(kwargs["task"], "adversarial_verification")
        messages = kwargs["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("here is my solution", messages[1]["content"])

    @patch("agent.auxiliary_client.call_llm")
    def test_warns_when_verification_runs_same_family(self, mock_call_llm):
        mock_call_llm.return_value = _make_response(_APPROVED_JSON)
        cli = self._make_cli(agent_model="anthropic/claude-opus-4.8")

        with patch.dict(
            "cli.CLI_CONFIG",
            {
                "auxiliary": {
                    "adversarial_verification": {"provider": "auto", "model": ""}
                }
            },
            clear=False,
        ):
            cli._handle_verify_command()

        warned = any(
            "#909" in str(call.args[0]) and "SAME model family" in str(call.args[0])
            for call in cli._console_print.call_args_list
        )
        self.assertTrue(warned, "expected a same-family warning to be printed")

    @patch("agent.auxiliary_client.call_llm")
    def test_no_warning_when_cross_family_model_configured(self, mock_call_llm):
        mock_call_llm.return_value = _make_response(_APPROVED_JSON)
        cli = self._make_cli(agent_model="anthropic/claude-opus-4.8")

        with patch.dict(
            "cli.CLI_CONFIG",
            {
                "auxiliary": {
                    "adversarial_verification": {
                        "provider": "openrouter",
                        "model": "google/gemini-3-flash-preview",
                    }
                }
            },
            clear=False,
        ):
            cli._handle_verify_command()

        warned = any(
            "#909" in str(call.args[0]) for call in cli._console_print.call_args_list
        )
        self.assertFalse(
            warned, "should not warn when verifier is configured cross-family"
        )

    def test_no_last_response_skips_verification(self):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli.conversation_history = []
        cli.agent = SimpleNamespace(model="anthropic/claude-opus-4.8")
        cli._console_print = MagicMock()

        with patch("agent.auxiliary_client.call_llm") as mock_call_llm:
            cli._handle_verify_command()
            mock_call_llm.assert_not_called()

        self.assertTrue(
            any(
                "No agent response to verify" in str(call.args[0])
                for call in cli._console_print.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
