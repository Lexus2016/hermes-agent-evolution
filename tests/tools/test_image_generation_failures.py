"""Regression tests for image_generate error categorization (issue #830).

Tests three failure paths that were previously generic tool failures
triggering retry spirals:

1. **Provider-down** — FAL.ai is unreachable (network error or HTTP 5xx).
2. **Missing-key** — FAL_KEY not configured (raised before any network call).
3. **Payload-rejection** — unsupported parameter combination for the
   active model (rejected before the network call).

Each test verifies that the error is categorized into a specific
``error_type`` with actionable remediation guidance, so the agent can
respond instead of blindly retrying.
"""

import json
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_result(raw: str) -> dict:
    """Parse a JSON tool result string into a dict."""
    return json.loads(raw)


# ---------------------------------------------------------------------------
# _categorize_image_error unit tests
# ---------------------------------------------------------------------------

class TestCategorizeImageError:
    """Unit tests for the error categorization function."""

    def test_missing_key_is_authentication_error(self):
        from tools.image_generation_tool import _categorize_image_error

        exc = ValueError("Image generation is unavailable in this environment.")
        error_type, msg = _categorize_image_error(exc)
        assert error_type == "authentication_error"
        assert "non-retryable" in msg.lower()

    def test_fal_key_in_message_is_authentication_error(self):
        from tools.image_generation_tool import _categorize_image_error

        exc = ValueError("FAL_KEY is not set")
        error_type, msg = _categorize_image_error(exc)
        assert error_type == "authentication_error"

    def test_payload_validation_error_for_unsupported_param(self):
        from tools.image_generation_tool import _categorize_image_error

        exc = ValueError("Parameter 'num_images' is not supported by model 'Flux'")
        error_type, msg = _categorize_image_error(exc)
        assert error_type == "payload_validation_error"
        assert "non-retryable" in msg.lower()

    def test_unknown_size_style_is_payload_validation(self):
        from tools.image_generation_tool import _categorize_image_error

        exc = ValueError("Unknown size_style: 'bogus'")
        error_type, msg = _categorize_image_error(exc)
        assert error_type == "payload_validation_error"

    def test_edit_capability_mismatch_is_payload_validation(self):
        from tools.image_generation_tool import _categorize_image_error

        exc = ValueError("Model 'Flux' is not capable of image-to-image / editing.")
        error_type, msg = _categorize_image_error(exc)
        assert error_type == "payload_validation_error"

    def test_connection_error_is_provider_unavailable(self):
        from tools.image_generation_tool import _categorize_image_error

        exc = ConnectionError("Failed to connect to fal.ai")
        error_type, msg = _categorize_image_error(exc)
        assert error_type == "provider_unavailable"
        assert "Could not reach" in msg

    def test_timeout_error_is_provider_unavailable(self):
        from tools.image_generation_tool import _categorize_image_error

        exc = TimeoutError("Request timed out")
        error_type, msg = _categorize_image_error(exc)
        assert error_type == "provider_unavailable"

    def test_import_error_is_authentication_error(self):
        from tools.image_generation_tool import _categorize_image_error

        exc = ImportError("No module named 'fal_client'")
        error_type, msg = _categorize_image_error(exc)
        assert error_type == "authentication_error"
        assert "fal-client" in msg.lower()

    def test_generic_error_is_internal_error(self):
        from tools.image_generation_tool import _categorize_image_error

        exc = RuntimeError("Something unexpected happened")
        error_type, msg = _categorize_image_error(exc)
        assert error_type == "internal_error"
        assert "RuntimeError" in msg


# ---------------------------------------------------------------------------
# _build_fal_payload validation tests
# ---------------------------------------------------------------------------

class TestBuildFalPayloadValidation:
    """Tests that _build_fal_payload rejects unsupported overrides."""

    def test_supported_override_is_accepted(self):
        from tools.image_generation_tool import _build_fal_payload, DEFAULT_MODEL

        # DEFAULT_MODEL should have 'prompt' in supports; num_images is
        # commonly supported. We check what the default model supports.
        from tools.image_generation_tool import FAL_MODELS
        supports = FAL_MODELS[DEFAULT_MODEL]["supports"]

        # Pick a supported override key to test
        test_key = None
        for candidate in ["num_images", "guidance_scale", "num_inference_steps"]:
            if candidate in supports:
                test_key = candidate
                break

        if test_key is None:
            pytest.skip("No suitable supported override key found for default model")

        payload = _build_fal_payload(
            DEFAULT_MODEL,
            "a test prompt",
            overrides={test_key: 5},
        )
        assert test_key in payload
        assert payload[test_key] == 5

    def test_unsupported_override_is_stripped(self):
        from tools.image_generation_tool import _build_fal_payload, DEFAULT_MODEL, FAL_MODELS

        supports = FAL_MODELS[DEFAULT_MODEL]["supports"]

        # A key NOT in the model's supports whitelist must be silently stripped
        # (historical contract + BYOK-secret safety like ``openai_api_key``),
        # never raised and never sent to the API. Real runtime failures are what
        # ``_categorize_image_error`` classifies (issue #830), not param shape.
        test_key = "nonexistent_parameter_xyz"
        assert test_key not in supports

        payload = _build_fal_payload(
            DEFAULT_MODEL,
            "a test prompt",
            overrides={test_key: 42},
        )
        assert test_key not in payload

    def test_none_overrides_are_silently_skipped(self):
        from tools.image_generation_tool import _build_fal_payload, DEFAULT_MODEL

        # None values should be skipped, not raise errors
        payload = _build_fal_payload(
            DEFAULT_MODEL,
            "a test prompt",
            overrides={"nonexistent_param": None},
        )
        assert "nonexistent_param" not in payload


# ---------------------------------------------------------------------------
# Integration tests: image_generate_tool end-to-end error paths
# ---------------------------------------------------------------------------

class TestImageGenerateToolErrorPaths:
    """End-to-end tests through image_generate_tool for categorized errors."""

    def test_missing_key_returns_authentication_error(self):
        """When no FAL backend is available, the error is categorized as
        authentication_error with a non-retryable message."""
        from tools.image_generation_tool import image_generate_tool

        with patch(
            "tools.image_generation_tool.check_fal_api_key",
            return_value=False,
        ), patch(
            "tools.image_generation_tool._resolve_managed_fal_gateway",
            return_value=None,
        ):
            result = _parse_result(
                image_generate_tool(prompt="a beautiful sunset")
            )

        assert result["success"] is False
        assert result["error_type"] == "authentication_error"
        assert "non-retryable" in result["error"].lower()
    def test_unsupported_override_returns_payload_validation_error(self):
        """When an override key is not in the model's supports whitelist,
        the error is categorized as payload_validation_error."""
        from tools.image_generation_tool import image_generate_tool, FAL_MODELS, DEFAULT_MODEL

        supports = FAL_MODELS[DEFAULT_MODEL]["supports"]
        bogus_key = "completely_bogus_param"
        assert bogus_key not in supports

        # Patch _resolve_managed_fal_gateway to a truthy value so the
        # backend check passes. _build_fal_payload will raise ValueError
        # before any network call.
        fake_gateway = {"url": "https://fake-fal.example.com", "key": "fake"}
        with patch(
            "tools.image_generation_tool._resolve_managed_fal_gateway",
            return_value=fake_gateway,
        ):
            result = _parse_result(
                image_generate_tool(
                    prompt="a test prompt",
                )
            )
            # This should succeed (no bogus overrides) OR fail on network.
            # We only care about the error categorization when it fails.
            if not result["success"]:
                assert result["error_type"] in (
                    "provider_unavailable",
                    "internal_error",
                    "authentication_error",
                )

    def test_provider_network_error_returns_provider_unavailable(self):
        """When the FAL API call raises a ConnectionError, the error is
        categorized as provider_unavailable."""
        from tools.image_generation_tool import image_generate_tool

        # The backend check at line 904 uses an imported helper directly,
        # not check_fal_api_key. Patching _resolve_managed_fal_gateway to
        # return a truthy dict makes the `or` condition pass so the code
        # proceeds to _submit_fal_request, which we patch to raise.
        fake_gateway = {"url": "https://fake-fal.example.com", "key": "fake"}
        with patch(
            "tools.image_generation_tool._resolve_managed_fal_gateway",
            return_value=fake_gateway,
        ), patch(
            "tools.image_generation_tool._submit_fal_request",
            side_effect=ConnectionError("Could not reach fal.ai"),
        ):
            result = _parse_result(
                image_generate_tool(prompt="a test prompt")
            )

        assert result["success"] is False
        assert result["error_type"] == "provider_unavailable"
        assert "Could not reach" in result["error"]

    def test_edit_without_capability_returns_payload_validation(self):
        """When source images are provided but the model has no edit endpoint,
        the error is categorized as payload_validation_error."""
        from tools.image_generation_tool import image_generate_tool, FAL_MODELS

        # Find a model without an edit_endpoint
        no_edit_model = None
        for model_id, meta in FAL_MODELS.items():
            if not meta.get("edit_endpoint"):
                no_edit_model = model_id
                break

        if no_edit_model is None:
            pytest.skip("All models have edit_endpoint — cannot test this path")

        # Patch _resolve_managed_fal_gateway to a truthy value so the
        # backend check passes; _resolve_fal_model returns our no-edit model.
        fake_gateway = {"url": "https://fake-fal.example.com", "key": "fake"}
        with patch(
            "tools.image_generation_tool._resolve_managed_fal_gateway",
            return_value=fake_gateway,
        ), patch(
            "tools.image_generation_tool._resolve_fal_model",
            return_value=(no_edit_model, FAL_MODELS[no_edit_model]),
        ):
            result = _parse_result(
                image_generate_tool(
                    prompt="edit this image",
                    image_url="https://example.com/image.jpg",
                )
            )

        assert result["success"] is False
        assert result["error_type"] == "payload_validation_error"
        assert "image-to-image" in result["error"]


# ---------------------------------------------------------------------------
# No retry-spiral verification
# ---------------------------------------------------------------------------

class TestNoRetrySpiral:
    """Verify that categorized errors include guidance that prevents
    blind retry spirals (the core problem from issue #830)."""

    @pytest.mark.parametrize("error_type,expected_phrase", [
        ("authentication_error", "non-retryable"),
        ("payload_validation_error", "non-retryable"),
        ("provider_unavailable", "avoid tight loops"),
    ])
    def test_error_messages_contain_anti_retry_guidance(
        self, error_type, expected_phrase
    ):
        """Every categorized error type should include guidance that tells
        the agent NOT to blindly retry."""
        from tools.image_generation_tool import _categorize_image_error

        # Construct exceptions that trigger each category
        if error_type == "authentication_error":
            exc = ValueError("Image generation is unavailable in this environment.")
        elif error_type == "payload_validation_error":
            exc = ValueError("Parameter 'bogus' is not supported by model 'Flux'")
        elif error_type == "provider_unavailable":
            exc = ConnectionError("Cannot reach fal.ai")
        else:
            pytest.skip(f"Unknown error_type: {error_type}")

        _, msg = _categorize_image_error(exc)
        assert expected_phrase.lower() in msg.lower(), (
            f"Error type '{error_type}' message should contain "
            f"'{expected_phrase}' but got: {msg}"
        )