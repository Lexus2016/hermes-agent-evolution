"""Bounded, lossless storage for reviewed JSON evidence exceeding the blob cap.

Reconstruct an original (including all whitespace) from the working tree:
    python -m scripts.code_health.evidence_parts analysis/sync-health/v0221-manifest.json > /tmp/manifest.json
Add --revision HEAD to read a committed tree instead. The index is storage metadata,
not a trust anchor: inherited_debt still checks the original pinned SHA256.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.code_health import gitio

FORMAT = "hermes-reviewed-evidence-parts-v1"
PART_BYTES = 256 * 1024
MAX_PARTS = 8
MAX_BYTES = PART_BYTES * MAX_PARTS
INDEX_BYTES = 16 * 1024


def _no_symlinks(repo, path):
    target = repo
    for component in Path(path).parts:
        target = target / component
        if target.is_symlink():
            raise RuntimeError("Evidence parts must not traverse symlinks")


def _inventory(repo, revision, directory):
    if revision is not None:
        return set(filter(None, gitio.git(repo, "ls-tree", "-r", "--name-only", "-z",
                                         revision, "--", directory).split("\0")))
    _no_symlinks(repo, directory)
    target = repo / directory
    if not target.exists():
        return set()
    if not target.is_dir():
        raise RuntimeError("Evidence parts namespace must be a directory")
    # Entries must be immediate files; unexpected directories are rejected as extras.
    return {f"{directory}/{entry.name}" for entry in target.iterdir()}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("Duplicate evidence index key")
        result[key] = value
    return result


def _size(value, limit):
    return type(value) is int and 0 < value <= limit


def _validate_index(index, path, encoded_size):
    if encoded_size > INDEX_BYTES or set(index) != {"format", "bytes", "sha256", "parts"}:
        raise RuntimeError("Invalid evidence parts index schema")
    parts = index["parts"]
    if (index["format"] != FORMAT or not _size(index["bytes"], MAX_BYTES)
            or not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS):
        raise RuntimeError("Invalid evidence parts index bounds")
    total = 0
    for number, part in enumerate(parts, 1):
        if (not isinstance(part, dict) or set(part) != {"path", "bytes", "sha256"}
                or part["path"] != f"{path}.parts/{number:04d}.txt"
                or not _size(part["bytes"], PART_BYTES)):
            raise RuntimeError("Invalid evidence part path, order or size")
        total += part["bytes"]
    digests = [index["sha256"], *(part["sha256"] for part in parts)]
    if any(not isinstance(d, str) or len(d) != 64 or any(c not in "0123456789abcdef" for c in d)
           for d in digests):
        raise RuntimeError("Invalid evidence parts digest")
    if total != index["bytes"]:
        raise RuntimeError("Evidence part sizes do not match total")
    return parts


def read_evidence(repo, revision, path, read_blob):
    """Read direct JSON or reconstruct one exact, strictly bounded parts envelope."""
    if not path or path.startswith("/") or ".." in Path(path).parts:
        raise RuntimeError("Invalid inherited-debt evidence path")
    inventory = _inventory(repo, revision, f"{path}.parts")
    stored = read_blob(repo, revision, path, max_bytes=INDEX_BYTES if inventory else MAX_BYTES)
    if stored is None:
        return None
    data = stored[1]
    try:
        index = json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeDecodeError):
        if inventory:
            raise RuntimeError("Invalid evidence index with existing parts") from None
        return stored  # Preserve the caller's existing direct-file validation.
    if not isinstance(index, dict) or "format" not in index:
        if inventory:
            raise RuntimeError("Ambiguous direct evidence with leftover parts")
        return stored
    parts = _validate_index(index, path, len(data))
    if inventory != {part["path"] for part in parts}:
        raise RuntimeError("Evidence parts missing or extra")
    chunks = []
    for part in parts:
        if revision is None:
            _no_symlinks(repo, part["path"])
        stored_part = read_blob(repo, revision, part["path"], max_bytes=part["bytes"])
        if stored_part is None:
            raise RuntimeError("Evidence part missing")
        chunk = stored_part[1]
        if len(chunk) != part["bytes"] or hashlib.sha256(chunk).hexdigest() != part["sha256"]:
            raise RuntimeError("Evidence part size or digest mismatch")
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Evidence part is not UTF-8 text") from exc
        chunks.append(chunk)
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != index["sha256"]:
        raise RuntimeError("Reconstructed evidence digest mismatch")
    sha = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    return sha, data


def main():
    import argparse
    import sys

    from scripts.code_health.inherited_debt import blob

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--revision")
    args = parser.parse_args()
    result = read_evidence(gitio.repo_root(Path.cwd()), args.revision, args.path, blob)
    if result is None:
        parser.error("Evidence does not exist")
    sys.stdout.buffer.write(result[1])


if __name__ == "__main__":
    main()
