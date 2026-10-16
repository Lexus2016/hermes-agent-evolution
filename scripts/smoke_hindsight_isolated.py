"""Real pinned Hindsight smoke for trusted Linux CI (no provider credentials).

Run with the checkout interpreter:
    .venv/bin/python scripts/smoke_hindsight_isolated.py --work-root /fresh/scratch/path

Requires Linux process inspection, loopback TCP and PostgreSQL Unix sockets,
registry access for canonical PM provisioning, and at least 12 GiB free space.
The root must not exist. Artifacts are retained on failure for diagnosis; never
remove an unresolved lifecycle record to bypass cleanup. This is an integration
gate: unsupported sandbox/network conditions fail rather than skip.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("This integration gate requires a trusted Linux runner")
    root = args.work_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    if shutil.disk_usage(root).free < 12 * 1024**3:
        raise RuntimeError("Hindsight smoke requires at least 12 GiB free scratch space")
    # Establish disposable profile/toolchain/cache roots before importing Hermes.
    for key, path in {"HERMES_HOME": root / "home", "HERMES_RUNTIME_DIR": root / "runtime",
                      "UV_CACHE_DIR": root / "cache", "HOME": root / "os-home",
                      "HF_HOME": root / "hf-cache"}.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import pm
    from plugins.memory.hindsight.daemon_bridge import OwnedDaemon, _live_descendants
    from plugins.memory.hindsight.isolated_runtime import ENVIRONMENT_NAME, REQUIREMENTS

    pm.ensure_environment(ENVIRONMENT_NAME, REQUIREMENTS, root=root / "environment",
                          explicit=True, timeout=1800, python_version="3.12")
    python = pm.environment_python(ENVIRONMENT_NAME, root=root / "environment", python_version="3.12")
    if python is None:
        raise RuntimeError("Canonical PM did not return the selected Python 3.12 generation")
    lock = python.parents[2] / "uv.lock"
    print(json.dumps({"phase": "installed", "python": str(python),
                      "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest()}), flush=True)
    launcher = Path(__file__).resolve().parents[1] / "plugins/memory/hindsight/daemon_launcher.py"
    subprocess.run([str(python), "-I", str(launcher), "--probe"], check=True, timeout=60)

    provider_calls = []

    class RejectProvider(BaseHTTPRequestHandler):
        def do_POST(self):
            provider_calls.append(self.path)
            self.send_error(503, "Provider execution is forbidden in this smoke")

        def do_GET(self):
            provider_calls.append(self.path)
            self.send_error(503, "Provider execution is forbidden in this smoke")

        def log_message(self, *_args):
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), RejectProvider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    environment = {
        "HINDSIGHT_API_LLM_PROVIDER": "none",
        "HINDSIGHT_API_SKIP_LLM_VERIFICATION": "true",
        "HINDSIGHT_API_LAZY_RERANKER": "true",
        "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "synthetic-local-only",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "text-embedding-3-small",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": f"http://127.0.0.1:{provider.server_port}/v1",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    }
    daemon = OwnedDaemon(root / "client", "hermes-synthetic-ci", environment, 0,
                         health_timeout=180, python=python)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def status(url, token=None):
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=10) as response:
                response.read()
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    owned = []
    process = None
    try:
        url = daemon.ensure_started()
        process = daemon.process
        if status(url + "/health") != 200:
            raise AssertionError("Unauthenticated readiness failed")
        actual = [status(url + "/v1/default/banks", token)
                  for token in (None, "synthetic-wrong-token", daemon.api_key)]
        if actual != [401, 401, 200]:
            raise AssertionError(f"Protected bank endpoint auth mismatch: {actual}")
        owned = list(daemon._descendants.values())
        if not owned:
            raise AssertionError("No owned PostgreSQL process identity was recorded")
        print(json.dumps({"phase": "healthy_authenticated", "health": 200,
                          "bank_auth_statuses": actual, "owned_processes": len(owned)}), flush=True)
    finally:
        try:
            daemon.stop()
        finally:
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=5)
    if provider_calls:
        raise AssertionError(f"Unexpected synthetic provider requests: {provider_calls}")
    if (not daemon._clean_receipt or daemon._ledger.exists() or daemon._file_lock is not None
            or _live_descendants(owned) or process is None or process.poll() is None):
        raise AssertionError("Owned process shutdown was not positively verified")
    print(json.dumps({"phase": "passed", "clean_receipt": True, "provider_calls": 0,
                      "remaining_owned_processes": 0}), flush=True)


if __name__ == "__main__":
    main()
