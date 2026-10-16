"""Model execution guidance contracts, independent of filesystem prompt discovery."""
from tests.agent.test_prompt_builder import GOOGLE_MODEL_OPERATIONAL_GUIDANCE, OPENAI_MODEL_EXECUTION_GUIDANCE, PARALLEL_TOOL_CALL_GUIDANCE, TOOL_USE_ENFORCEMENT_MODELS

class TestOpenAIModelExecutionGuidance:
    """Tests for GPT/Codex-specific execution discipline guidance."""

    def test_guidance_covers_tool_persistence(self):
        text = OPENAI_MODEL_EXECUTION_GUIDANCE.lower()
        assert "tool_persistence" in text
        assert "retry" in text
        assert "empty" in text or "partial" in text

    def test_guidance_covers_prerequisite_checks(self):
        text = OPENAI_MODEL_EXECUTION_GUIDANCE.lower()
        assert "prerequisite" in text
        assert "dependency" in text

    def test_guidance_covers_verification(self):
        text = OPENAI_MODEL_EXECUTION_GUIDANCE.lower()
        assert "verification" in text or "verify" in text
        assert "correctness" in text

    def test_guidance_covers_missing_context(self):
        text = OPENAI_MODEL_EXECUTION_GUIDANCE.lower()
        assert "missing_context" in text or "missing context" in text
        assert "hallucinate" in text or "guess" in text

    def test_guidance_uses_xml_tags(self):
        assert "<tool_persistence>" in OPENAI_MODEL_EXECUTION_GUIDANCE
        assert "</tool_persistence>" in OPENAI_MODEL_EXECUTION_GUIDANCE
        assert "<verification>" in OPENAI_MODEL_EXECUTION_GUIDANCE
        assert "</verification>" in OPENAI_MODEL_EXECUTION_GUIDANCE

    def test_guidance_is_string(self):
        assert isinstance(OPENAI_MODEL_EXECUTION_GUIDANCE, str)
        assert len(OPENAI_MODEL_EXECUTION_GUIDANCE) > 100

    def test_guidance_covers_external_write_readback(self):
        text = OPENAI_MODEL_EXECUTION_GUIDANCE.lower()
        assert "read" in text and "back" in text
        assert "successful tool call is not a successful task" in text

    def test_guidance_covers_count_reconciliation(self):
        text = OPENAI_MODEL_EXECUTION_GUIDANCE.lower()
        assert "has_more" in text
        assert "hard assertions" in text

    def test_guidance_covers_literal_preservation(self):
        text = OPENAI_MODEL_EXECUTION_GUIDANCE.lower()
        assert "normalize" in text
        assert "malformed" in text

    def test_guidance_covers_retry_differently(self):
        text = OPENAI_MODEL_EXECUTION_GUIDANCE.lower()
        assert "suspiciously narrow" in text
        assert "retry" in text

    def test_guidance_gates_completion_on_verification(self):
        text = OPENAI_MODEL_EXECUTION_GUIDANCE.lower()
        assert "plausible subset" in text


class TestExecutionGuidanceModels:
    """Behavior contracts for the default auto-match model list."""

    def test_includes_historical_families(self):
        from agent.prompt_builder import EXECUTION_GUIDANCE_MODELS
        for fam in ("gpt", "codex", "grok"):
            assert fam in EXECUTION_GUIDANCE_MODELS

    def test_includes_composio_eval_families(self):
        from agent.prompt_builder import EXECUTION_GUIDANCE_MODELS
        for fam in ("deepseek", "kimi", "qwen", "glm", "minimax", "mimo", "mistral"):
            assert fam in EXECUTION_GUIDANCE_MODELS

    def test_muse_spark_gets_both_guidance_blocks(self):
        # Muse Spark closes the turn after a chat-only response on defaults
        # (#96550) — it needs tool-use enforcement AND execution guidance.
        from agent.prompt_builder import EXECUTION_GUIDANCE_MODELS
        assert any(p in "meta/muse-spark-1.3-contributor" for p in TOOL_USE_ENFORCEMENT_MODELS)
        assert any(p in "meta/muse-spark-1.3-contributor" for p in EXECUTION_GUIDANCE_MODELS)

    def test_excludes_google_and_claude(self):
        # Gemini/Gemma get GOOGLE_MODEL_OPERATIONAL_GUIDANCE instead;
        # Claude doesn't exhibit the targeted failure modes.
        from agent.prompt_builder import EXECUTION_GUIDANCE_MODELS
        for fam in ("gemini", "gemma", "claude"):
            assert fam not in EXECUTION_GUIDANCE_MODELS


class TestParallelToolCallGuidance:
    """Behavior contracts for the universal parallel-tool-call guidance block.

    Asserts the invariants the block must satisfy (steer batching, scope to
    independent calls, stay short for the cached prompt) rather than freezing
    its exact wording.
    """

    def test_is_nonempty_string(self):
        assert isinstance(PARALLEL_TOOL_CALL_GUIDANCE, str)
        assert PARALLEL_TOOL_CALL_GUIDANCE.strip()

    def test_steers_batching_into_one_response(self):
        text = PARALLEL_TOOL_CALL_GUIDANCE.lower()
        # Must tell the model to group independent calls together — accept any
        # phrasing that means "one turn" without freezing exact wording.
        assert "single response" in text or ("same" in text and "turn" in text)
        assert "independent" in text

    def test_carves_out_dependent_calls(self):
        # Must NOT tell the model to batch dependent calls — that would break
        # ordering (read-before-patch). The block has to acknowledge the
        # serialize-when-dependent case.
        text = PARALLEL_TOOL_CALL_GUIDANCE.lower()
        assert "depend" in text

    def test_stays_short_for_cached_prompt(self):
        # Shipped in every cached system prompt — keep it tight. The existing
        # task-completion block is ~600 chars; allow generous headroom but
        # guard against accidental essay growth.
        assert len(PARALLEL_TOOL_CALL_GUIDANCE) < 900


    def test_not_duplicated_in_google_guidance(self):
        # The universal block is now the single source of parallel-batching
        # steer. The Google-only block must NOT carry its own copy, otherwise
        # Gemini/Gemma would receive the instruction twice in one prompt.
        assert "parallel tool call" not in GOOGLE_MODEL_OPERATIONAL_GUIDANCE.lower()
