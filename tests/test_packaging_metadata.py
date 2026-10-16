"""Independent core/optional dependency and reviewed CVE policies."""
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]

# This is the explicit exception inventory retained from fork main 9efba6dcfa,
# independent of both files under test. The reasons are documented beside
# [tool.uv.exclude-newer-package] and in pm/AGENTS.md. It includes existing
# ranged, build and legacy exceptions, not only exact pins. Preserving this
# divergence from upstream does not establish that the exceptions are safe;
# changing or retiring them requires a separate dependency-policy review.
_FORK_RELEASE_AGE_EXEMPTIONS = frozenset("""
agent-client-protocol ai-edge-litert aiohttp aiohttp-socks aiosqlite
alibabacloud-dingtalk anthropic asyncpg azure-identity boto3 brotlicffi certifi
concurrent-log-handler croniter cryptography daytona debugpy defusedxml
dingtalk-stream discord-py edge-tts elevenlabs exa-py fal-client fastapi
faster-whisper fire firecrawl-anydoc firecrawl-py google-api-python-client
google-auth google-auth-httplib2 google-auth-oauthlib google-cloud-pubsub h2
hindsight-client honcho-ai httplib2 httpx httpx2 huggingface-hub jinja2 lark-oapi
markdown maturin mautrix mcp mem0ai microsoft-teams-apps mistralai modal nemo-relay
numpy onnxruntime openai opentelemetry-api opentelemetry-exporter-otlp-proto-http
opentelemetry-sdk openwakeword packaging parallel-web pathspec pillow
prompt-toolkit psutil pvporcupine pyasn1 pydantic pyjwt pytest pytest-asyncio
pytest-timeout python-dotenv python-multipart python-olm python-telegram-bot
pyyaml qrcode requests rich ruamel-yaml ruff sentencepiece setuptools
setuptools-rust sherpa-onnx slack-bolt slack-sdk snowballstemmer sounddevice
starlette supermemory tenacity ty tzdata unpaddedbase64 uvicorn vercel websockets
wheel youtube-transcript-api
""".split())


def _assert_fork_release_age_policy(manifest, lock):
    uv = manifest["tool"]["uv"]
    options = lock["options"]
    assert uv.get("exclude-newer") == "14 days", "manifest release-age cutoff changed"
    assert options.get("exclude-newer-span") == "P14D", "lock release-age cutoff changed"
    for source, config in (("manifest", uv), ("lock", options)):
        exemptions = config.get("exclude-newer-package", {})
        names = {canonicalize_name(name) for name in exemptions}
        assert names == _FORK_RELEASE_AGE_EXEMPTIONS, f"{source} exception inventory changed"
        assert len(names) == len(exemptions), f"{source} has duplicate normalized exceptions"
        assert all(value is False for value in exemptions.values()), (
            f"{source} exception values must be exactly false"
        )


@pytest.fixture
def release_age_metadata():
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return manifest, lock


def test_test_dependencies_are_group_only_in_manifest_and_lock():
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    hermes = next(package for package in lock["package"] if package["name"] == manifest["project"]["name"])
    assert manifest["tool"]["uv"]["default-groups"] == []
    assert "dev" in manifest["dependency-groups"]
    assert "dev" not in manifest["project"]["optional-dependencies"]
    assert "dev" in hermes["dev-dependencies"]
    assert "dev" not in hermes.get("optional-dependencies", {})


def test_core_and_optional_speech_dependencies():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = {Requirement(dep).name for dep in project["dependencies"]}
    assert "packaging" in core  # Runtime code imports it directly, not transitively.
    assert "faster-whisper" not in core
    assert "faster-whisper" in {
        Requirement(dep).name for dep in project["optional-dependencies"]["stt-whisper"]
    }


def test_starlette_server_pins_and_lock_exclude_cve_2026_48710():
    # BadHost's reviewed fixed boundary is independent of today's exact pin.
    floor = Version("1.0.1")
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    found = set()
    for extra, specs in metadata["project"]["optional-dependencies"].items():
        for requirement in map(Requirement, specs):
            if requirement.name != "starlette":
                continue
            pins = list(requirement.specifier)
            assert len(pins) == 1 and pins[0].operator == "==", (extra, requirement)
            assert Version(pins[0].version) >= floor, (extra, requirement)
            found.add(extra)
    assert {"web", "mcp", "computer-use"} <= found
    dev = [req for req in map(Requirement, metadata["dependency-groups"]["dev"])
           if req.name == "starlette"]
    assert len(dev) == 1
    pins = list(dev[0].specifier)
    assert len(pins) == 1 and pins[0].operator == "==" and Version(pins[0].version) >= floor
    versions = [Version(row["version"]) for row in lock["package"] if row["name"] == "starlette"]
    assert versions and all(version >= floor for version in versions)


def test_fork_fourteen_day_release_age_and_explicit_exceptions(release_age_metadata):
    """Lock-time age policy preserves the fork's cutoff and bounded exception table."""
    _assert_fork_release_age_policy(*release_age_metadata)


@pytest.mark.parametrize("target", ["manifest", "lock"])
@pytest.mark.parametrize("remove", [False, True], ids=["shortened", "missing"])
def test_release_age_policy_rejects_cutoff_changes(release_age_metadata, target, remove):
    manifest, lock = release_age_metadata
    config, key, value = (
        (manifest["tool"]["uv"], "exclude-newer", "3 days")
        if target == "manifest" else (lock["options"], "exclude-newer-span", "P3D")
    )
    if remove:
        del config[key]
    else:
        config[key] = value
    with pytest.raises(AssertionError, match=f"{target} release-age cutoff changed"):
        _assert_fork_release_age_policy(manifest, lock)


@pytest.mark.parametrize("requirement", [
    "unreviewed-package", "unreviewed-package>=1,<2", "unreviewed-package==1.0",
], ids=["unpinned", "ranged", "exact-pinned"])
def test_new_dependency_does_not_authorize_release_age_exemption(release_age_metadata, requirement):
    manifest, lock = release_age_metadata
    manifest["project"]["dependencies"].append(requirement)
    # Even synchronized manifest/lock exemptions need an independent review;
    # deriving the allowlist from their union or from exact pins would miss this.
    for config in (manifest["tool"]["uv"], lock["options"]):
        config["exclude-newer-package"]["unreviewed-package"] = False
    with pytest.raises(AssertionError, match="exception inventory changed"):
        _assert_fork_release_age_policy(manifest, lock)


@pytest.mark.parametrize("target", ["manifest", "lock"])
@pytest.mark.parametrize("name,value,error", [
    ("*", False, "exception inventory changed"),
    ("aio*", False, "exception inventory changed"),
    ("aiohttp", "2027-01-01", "exception values must be exactly false"),
    ("aiohttp", True, "exception values must be exactly false"),
    ("aiohttp", 0, "exception values must be exactly false"),
], ids=["wildcard", "prefix", "date", "true", "false-like-integer"])
def test_release_age_policy_rejects_broad_or_changed_exemptions(
    release_age_metadata, target, name, value, error,
):
    manifest, lock = release_age_metadata
    config = manifest["tool"]["uv"] if target == "manifest" else lock["options"]
    config["exclude-newer-package"][name] = value
    with pytest.raises(AssertionError, match=f"{target} {error}"):
        _assert_fork_release_age_policy(manifest, lock)


@pytest.mark.parametrize("target", ["manifest", "lock"])
def test_release_age_policy_rejects_missing_exemptions(release_age_metadata, target):
    manifest, lock = release_age_metadata
    config = manifest["tool"]["uv"] if target == "manifest" else lock["options"]
    del config["exclude-newer-package"]["setuptools"]
    with pytest.raises(AssertionError, match=f"{target} exception inventory changed"):
        _assert_fork_release_age_policy(manifest, lock)


def test_httpx2_pins_and_lock_exclude_ghsa_7mj9_2mp8_4m2p_family():
    # 2.12.0 is the first httpx2/httpcore2 release clear of GHSA-7mj9-2mp8-4m2p,
    # GHSA-f2fp-rgf2-35cp, GHSA-h4x7-gw46-3wm6, GHSA-pf96-p4fj-6566 and
    # GHSA-8xx6-hgc6-gc2m; the boundary is independent of today's exact pin.
    floor = Version("2.12.0")
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    declared = dict(metadata["project"]["optional-dependencies"])
    declared["group:dev"] = metadata["dependency-groups"]["dev"]
    found = set()
    for where, specs in declared.items():
        for requirement in map(Requirement, specs):
            if requirement.name != "httpx2":
                continue
            pins = list(requirement.specifier)
            assert len(pins) == 1 and pins[0].operator == "==", (where, requirement)
            assert Version(pins[0].version) >= floor, (where, requirement)
            found.add(where)
    assert {"mcp", "computer-use", "group:dev"} <= found
    for name in ("httpx2", "httpcore2"):
        versions = [Version(row["version"]) for row in lock["package"] if row["name"] == name]
        assert versions and all(version >= floor for version in versions), (name, versions)
