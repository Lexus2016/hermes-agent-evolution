"""The sandbox payload must trust the MITM CA, including Node.

The fake-internet proxy terminates TLS with a throwaway CA. curl/Python/git
replace their store with that file. Node's NODE_EXTRA_CA_CERTS is additive and
already includes the public roots in real-ca.pem, so pointing it at real-ca.pem
leaves every proxy-minted leaf untrusted. That is how ``npm install`` died in
under a second (empty output under ``--silent``) on the installer E2E once a
failed npm install became fatal.

Asserted by running stage2-run.sh --dump-tls-env, not by grepping the script.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE2 = REPO_ROOT / "scripts" / "sandbox" / "stage2-run.sh"
MITM_CA = "/work/certs/ca.pem"


@pytest.mark.skipif(not STAGE2.is_file(), reason="sandbox stage2 script missing")
def test_payload_tls_env_points_node_at_the_mitm_ca() -> None:
    proc = subprocess.run(
        ["bash", str(STAGE2), "--dump-tls-env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    env = dict(
        line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
    )

    for key in (
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "GIT_SSL_CAINFO",
        "NODE_EXTRA_CA_CERTS",
        "npm_config_cafile",
    ):
        assert env[key] == MITM_CA, f"{key}={env.get(key)!r}"

    assert "real-ca.pem" not in env["NODE_EXTRA_CA_CERTS"]
    assert "real-ca.pem" not in env["npm_config_cafile"]
