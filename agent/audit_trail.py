"""Tamper-evident append-only audit trail (issue #1719).

Hash-chained JSONL: each entry stores the SHA-256 of the previous entry's hash
plus its payload, so tampering is detectable via ``verify()``. Retention via
``security.audit.retention_days`` (default 90); ``prune()`` re-anchors the chain.
"""

import hashlib
import json
import time
from pathlib import Path

from hermes_constants import get_hermes_home

DEFAULT_RETENTION_DAYS = 90
_GENESIS = "genesis"


def retention_days() -> int:
    """Return the configured retention window in days (fail-open to default)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = (load_config_readonly().get("security") or {}).get("audit") or {}
        val = int(cfg.get("retention_days", DEFAULT_RETENTION_DAYS))
        return val if val > 0 else DEFAULT_RETENTION_DAYS
    except Exception:
        return DEFAULT_RETENTION_DAYS


def _audit_path() -> Path:
    return get_hermes_home() / "logs" / "audit-trail.jsonl"


def _hash(prev_hash: str, payload: str) -> str:
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


def _last_hash(path: Path) -> str:
    if not path.exists():
        return _GENESIS
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            try:
                return json.loads(line)["hash"]
            except (json.JSONDecodeError, KeyError):
                return _GENESIS
    return _GENESIS


def append(record: dict, *, path: Path | None = None) -> dict:
    """Append a record to the chained log and return it with hash fields."""
    path = path or _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, sort_keys=True)
    prev = _last_hash(path)
    entry = {
        "ts": int(time.time()),
        "prev_hash": prev,
        "payload": payload,
        "hash": _hash(prev, payload),
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def verify(path: Path | None = None) -> tuple[bool, int]:
    """Recompute the chain; return (valid, entry_count)."""
    path = path or _audit_path()
    if not path.exists():
        return True, 0
    prev, count = _GENESIS, 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return False, count
        if entry.get("prev_hash") != prev or entry.get("hash") != _hash(
            prev, entry.get("payload", "")
        ):
            return False, count
        prev = entry["hash"]
        count += 1
    return True, count


def prune(*, now: float | None = None, path: Path | None = None) -> int:
    """Drop entries older than the retention window; re-anchor the chain."""
    path = path or _audit_path()
    if not path.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - retention_days() * 86400
    kept, removed = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ts = json.loads(line)["ts"]
        except (json.JSONDecodeError, KeyError, TypeError):
            kept.append(line)
            continue
        if ts < cutoff:
            removed += 1
        else:
            kept.append(line)
    if not removed:
        return 0
    reanchored, prev = [], _GENESIS
    for line in kept:
        entry = json.loads(line)
        entry["prev_hash"] = prev
        entry["hash"] = _hash(prev, entry["payload"])
        reanchored.append(json.dumps(entry, sort_keys=True))
        prev = entry["hash"]
    out = "\n".join(reanchored) + ("\n" if reanchored else "")
    path.write_text(out, encoding="utf-8")
    return removed
