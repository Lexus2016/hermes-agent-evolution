"""Read Tirith policy once without treating an unreadable policy as disabled."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class TirithConfigurationError(ValueError):
    """Active scanner policy could not be established safely."""


def configuration_error_result() -> dict:
    return {
        "action": "block", "configuration_error": True,
        "failure_class": "configuration_error", "findings": [],
        "summary": "Tirith security configuration could not be read. Repair the active config with "
                   "'hermes config edit' and 'hermes config check', then retry. No command was approved.",
    }


def read_security_policy() -> dict:
    try:
        from hermes_cli.config_read_errors import FailedConfigRead
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except Exception as exc:
        logger.debug("Tirith policy unavailable (%s)", type(exc).__name__,
                     exc_info=(RuntimeError, RuntimeError(type(exc).__name__), None))
        raise TirithConfigurationError(f"Security configuration unavailable ({type(exc).__name__})") from None
    if isinstance(config, FailedConfigRead) or not isinstance(config, dict):
        raise TirithConfigurationError("Security configuration unavailable")
    security = config.get("security", {})
    if security is None:
        security = {}
    if not isinstance(security, dict):
        raise TirithConfigurationError("Security configuration must be a mapping")

    return security


def load_security_config(*, default_enabled: bool = False) -> dict:
    security = read_security_policy()

    def boolean(key, default):
        value = os.getenv(key, default)
        return str(value).lower() in {"1", "true", "yes"}

    try:
        timeout = int(os.getenv("TIRITH_TIMEOUT", security.get("tirith_timeout", 5)))
    except (TypeError, ValueError):
        # Preserve the existing invalid-environment-value fallback.
        timeout = security.get("tirith_timeout", 5)
    return {
        "tirith_enabled": boolean("TIRITH_ENABLED", security.get("tirith_enabled", default_enabled)),
        "tirith_fail_open": boolean("TIRITH_FAIL_OPEN", security.get("tirith_fail_open", True)),
        "tirith_path": os.getenv("TIRITH_BIN", security.get("tirith_path", "tirith")),
        "tirith_timeout": timeout,
    }
