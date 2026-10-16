"""Lossless evidence storage must fail closed without changing policy trust anchors."""

import hashlib
import json
import subprocess

import pytest

from scripts.code_health.evidence_parts import FORMAT, MAX_BYTES, MAX_PARTS, PART_BYTES, read_evidence
from scripts.code_health.inherited_debt import blob

PATH = "analysis/report.json"


def _package(repo):
    original = '{\n  "message": "Привіт",\n  "entries": []\n}\n'.encode()
    chunks = [original[:2], original[2:]]
    parts = []
    for number, chunk in enumerate(chunks, 1):
        path = f"{PATH}.parts/{number:04d}.txt"
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(chunk)
        parts.append({"path": path, "bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
    index = {"format": FORMAT, "bytes": len(original), "sha256": hashlib.sha256(original).hexdigest(), "parts": parts}
    _write_index(repo, index)
    return original, index


def _write_index(repo, index):
    (repo / PATH).write_text(json.dumps(index), encoding="utf-8")


def _tree(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    return subprocess.check_output(["git", "write-tree"], cwd=repo, text=True, encoding="utf-8").strip()


@pytest.mark.parametrize("committed", [False, True])
def test_exact_bytes_and_original_blob_identity(tmp_path, committed):
    original, _ = _package(tmp_path)
    revision = _tree(tmp_path) if committed else None
    result = read_evidence(tmp_path, revision, PATH, blob)
    assert result[1] == original
    assert result[0] == hashlib.sha1(b"blob " + str(len(original)).encode() + b"\0" + original).hexdigest()
    if committed:
        (tmp_path / PATH).write_text("worktree differs", encoding="utf-8")
        assert read_evidence(tmp_path, revision, PATH, blob) == result


@pytest.mark.parametrize("committed", [False, True])
def test_legacy_direct_json(tmp_path, committed):
    target = tmp_path / PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(b'{"entries": []}\n')
    revision = _tree(tmp_path) if committed else None
    assert read_evidence(tmp_path, revision, PATH, blob) == blob(tmp_path, revision, PATH)


def _add_nested_extra(part):
    (part.parent / "extra").mkdir()
    (part.parent / "extra/file").write_text("extra", encoding="utf-8")


def _damage_package(repo, index, damage):
    first = index["parts"][0]
    part = repo / first["path"]
    mutations = {
        "missing": part.unlink,
        "duplicate": lambda: index["parts"].__setitem__(1, first.copy()),
        "reordered": index["parts"].reverse,
        "tampered": lambda: part.write_bytes(b"xx"),
        "extra": lambda: (part.parent / "extra.txt").write_text("extra", encoding="utf-8"),
        "nested_extra": lambda: _add_nested_extra(part),
        "escape": lambda: first.update(path="../escape"),
        "absolute": lambda: first.update(path="/tmp/escape"),
        "total": lambda: index.update(bytes=index["bytes"] + 1),
        "digest": lambda: index.update(sha256="0" * 64),
        "boolean": lambda: first.update(bytes=True),
        "empty": lambda: first.update(bytes=0),
        "oversize": lambda: (first.update(bytes=PART_BYTES + 1), index.update(bytes=MAX_BYTES + 1)),
        "too_many": lambda: index.update(parts=index["parts"] * MAX_PARTS),
        "unknown_key": lambda: index.update(entries=[]),
        "unknown_format": lambda: index.update(format="unknown"),
        "bad_digest": lambda: first.update(sha256=[]),
    }
    if damage != "direct":
        mutations[damage]()


@pytest.mark.parametrize("damage", ["missing", "duplicate", "reordered", "tampered", "extra", "nested_extra",
                                    "escape", "absolute", "direct", "total", "digest", "boolean", "empty",
                                    "oversize", "too_many", "unknown_key", "unknown_format", "bad_digest"])
@pytest.mark.parametrize("committed", [False, True])
def test_invalid_package_fails_closed(tmp_path, damage, committed):
    original, index = _package(tmp_path)
    _damage_package(tmp_path, index, damage)
    _write_index(tmp_path, index)
    if damage == "direct":
        (tmp_path / PATH).write_bytes(original)
    revision = _tree(tmp_path) if committed else None
    with pytest.raises(RuntimeError):
        read_evidence(tmp_path, revision, PATH, blob)


def test_duplicate_index_keys_fail_closed(tmp_path):
    _, index = _package(tmp_path)
    (tmp_path / PATH).write_text(json.dumps(index)[:-1] + ', "bytes": 1}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Duplicate"):
        read_evidence(tmp_path, None, PATH, blob)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("committed", [False, True])
def test_symlink_part_is_rejected(tmp_path, committed):
    _, index = _package(tmp_path)
    part = tmp_path / index["parts"][0]["path"]
    target = tmp_path / "target"
    part.rename(target)
    part.symlink_to(target)
    revision = _tree(tmp_path) if committed else None
    with pytest.raises(RuntimeError, match="symlink|regular Git blob"):
        read_evidence(tmp_path, revision, PATH, blob)


@pytest.mark.platforms("posix")
def test_symlink_parent_inside_repo_is_rejected(tmp_path):
    _package(tmp_path)
    directory = tmp_path / f"{PATH}.parts"
    target = tmp_path / "parts"
    directory.rename(target)
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        read_evidence(tmp_path, None, PATH, blob)


@pytest.mark.parametrize("path", ["scripts/code_health/evidence_parts.py", f"{PATH}.parts/0001.txt"])
def test_packaging_cannot_bypass_independent_review(path):
    from scripts.evolution_admission import _baseline_evidence
    from scripts.evolution_merge_gate import check_merge_policy

    if path.startswith("analysis/"):
        path = path.replace("analysis/", "analysis/sync-health/", 1)
    assert any("HIGH_RISK_PATH" in error for error in check_merge_policy([{"path": path, "additions": 1}]))
    run = {"pull_requests": [{"number": 1, "head": {"sha": "head"}, "base": {"sha": "base"}}]}

    def runner(_cmd):
        return 0, json.dumps([{"files": [{"filename": path, "status": "added"}]}]), ""

    with pytest.raises(ValueError, match="requires human review"):
        _baseline_evidence(run, "head", "owner/repo", runner, 1)


@pytest.mark.parametrize("damage", ["malformed", "oversized_index", "invalid_utf8"])
@pytest.mark.parametrize("committed", [False, True])
def test_invalid_text_envelope_fails_closed(tmp_path, damage, committed):
    _, index = _package(tmp_path)
    target = tmp_path / PATH
    if damage == "malformed":
        target.write_bytes(b"{")
    elif damage == "oversized_index":
        target.write_bytes(target.read_bytes() + b" " * (16 * 1024))
    else:
        first = index["parts"][0]
        chunk = b"\xff\xff"
        (tmp_path / first["path"]).write_bytes(chunk)
        first["sha256"] = hashlib.sha256(chunk).hexdigest()
        _write_index(tmp_path, index)
    revision = _tree(tmp_path) if committed else None
    with pytest.raises(RuntimeError):
        read_evidence(tmp_path, revision, PATH, blob)


@pytest.mark.parametrize("committed", [False, True])
def test_actual_oversize_part_cannot_hide_behind_small_declaration(tmp_path, committed):
    _, index = _package(tmp_path)
    part = tmp_path / index["parts"][0]["path"]
    part.write_bytes(b"x" * (PART_BYTES + 1))
    revision = _tree(tmp_path) if committed else None
    with pytest.raises(RuntimeError, match="storage byte limit"):
        read_evidence(tmp_path, revision, PATH, blob)


@pytest.mark.parametrize("committed", [False, True])
def test_direct_evidence_read_is_bounded(tmp_path, committed):
    path = tmp_path / PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * (MAX_BYTES + 1))
    revision = _tree(tmp_path) if committed else None
    with pytest.raises(RuntimeError, match="storage byte limit"):
        read_evidence(tmp_path, revision, PATH, blob)
    # Ordinary source reads retain their existing unbounded behavior.
    assert len(blob(tmp_path, revision, PATH)[1]) == MAX_BYTES + 1
