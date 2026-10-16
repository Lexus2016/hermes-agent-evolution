"""PM-owned embedded Hindsight dependencies and profile database identity."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from hermes_constants import get_hermes_home, hermes_home_key, profile_name_for_home

ENVIRONMENT_NAME = "hindsight-embedded"
REQUIREMENTS = ("hindsight-all==0.6.1", "hindsight-embed==0.6.1", "hindsight-client==0.6.1", "pg0-embedded==0.15.2", "psutil==7.2.2")


def install_runtime(*, explicit: bool = True) -> Path:
    from pm import ensure_environment

    return ensure_environment(ENVIRONMENT_NAME, REQUIREMENTS, explicit=explicit, python_version="3.12")


def runtime_python() -> Path:
    from pm import environment_python

    python = environment_python(ENVIRONMENT_NAME, python_version="3.12")
    if python is None:
        raise RuntimeError("Embedded Hindsight is not installed. Run 'hermes memory setup' and select local_embedded.")
    return python


def probe_runtime() -> tuple[bool, str | None]:
    """Read-only availability probe; never installs or imports the server into Hermes."""
    try:
        from tools.environments.local import hermes_subprocess_env

        python = runtime_python()
        result = subprocess.run(
            [str(python), "-I", str(Path(__file__).with_name("daemon_launcher.py")), "--probe"],
            capture_output=True, text=True, timeout=30, check=False, env=hermes_subprocess_env(),
        )
        if result.returncode:
            return False, result.stderr.strip() or "Isolated embedded runtime probe failed"
        return True, None
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def database_identity(config: dict, *, home: Path | None = None) -> str:
    """Never silently copy/adopt a database shared by old named-profile defaults."""
    home = home or get_hermes_home()
    profile = str(config.get("profile") or "hermes")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise ValueError("Hindsight profile must contain only letters, numbers, underscore or hyphen")
    legacy = f"hindsight-embed-{profile}"
    if profile_name_for_home(home) == "default":
        return legacy
    digest = hashlib.sha256(hermes_home_key(home).encode()).hexdigest()[:20]
    identity = f"hermes-{digest}-{profile}"
    legacy_path = Path.home() / ".pg0" / "instances" / legacy
    if legacy_path.exists() and config.get("embedded_database_choice") != "fresh":
        raise RuntimeError(
            f"Legacy shared Hindsight database exists at {legacy_path}. This profile cannot adopt it automatically. "
            "Stop its users and back it up; identify its owner before an operator-reviewed import. "
            "To intentionally start a separate empty database, set embedded_database_choice to 'fresh' "
            "in this profile's hindsight/config.json. Legacy bytes are left unchanged."
        )
    return identity


def runtime_cache_key(home: Path, identity: str, config: dict) -> tuple[str, str, str]:
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return hermes_home_key(home), identity, fingerprint
