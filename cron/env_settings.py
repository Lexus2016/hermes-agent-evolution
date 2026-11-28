"""Profile-scoped reads of the ``HERMES_*`` tuning settings cron honours from ``.env``.

A standalone ``hermes -p X gateway run`` loads X's ``.env`` into ``os.environ``, so a bare
``os.getenv("HERMES_CRON_TIMEOUT")`` is X's value. Under ``gateway.multiplex_profiles`` the same
tick runs inside the default profile's process, where ``os.environ`` holds the DEFAULT profile's
``.env``. With a secret scope installed (job run + delivery) the scope is authoritative; the tick
loop itself (due-job scan, pool sizing) runs under the profile's home override only, so the
setting is read from that home's ``.env``. Outside multiplex the read is the plain environ.
"""

from __future__ import annotations

import logging
import os

from agent.secret_scope import current_secret_scope, is_multiplex_active, load_env_file
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def cron_env_setting(name: str, default: str = "") -> str:
    if not is_multiplex_active():
        return os.getenv(name) or default
    scope = current_secret_scope()
    if scope is None:
        scope = load_env_file(get_hermes_home() / ".env")
    value = scope.get(name)
    return default if value is None else str(value)


def cron_seconds_setting(env_name: str, config_key: str, default: float) -> float:
    """Resolve an env-over-config ``cron`` seconds setting.

    ``env_name`` (profile-scoped, see :func:`cron_env_setting`) wins over
    ``cron.<config_key>`` in the effective ``config.yaml``. Missing or non-numeric values return
    *default*; any numeric value is passed through unchanged, because the callers interpret
    ``<= 0`` (and a non-finite value) themselves: unlimited inactivity or no drain.
    """
    raw = cron_env_setting(env_name).strip()
    if raw:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning("Invalid %s=%r; reading cron.%s instead", env_name, raw, config_key)
        else:
            return value

    try:
        from hermes_cli.config_backend import config_exists
        from hermes_cli.config_effective import load_user_config_effective

        cfg_path = get_hermes_home() / "config.yaml"
        if not config_exists(cfg_path):
            return default
        cfg = load_user_config_effective(cfg_path)
    except Exception as exc:  # config plane down: a tick must not fail on a tuning read
        logger.debug("cron.%s: config read failed (%s); using default %ss", config_key, exc, default)
        return default
    cron_cfg = cfg.get("cron") if isinstance(cfg, dict) else None
    raw_cfg = cron_cfg.get(config_key) if isinstance(cron_cfg, dict) else None
    if raw_cfg is None or isinstance(raw_cfg, bool):
        return default
    try:
        value = float(raw_cfg)
    except (TypeError, ValueError):
        logger.warning("Invalid cron.%s=%r; using default %ss", config_key, raw_cfg, default)
        return default
    return value
