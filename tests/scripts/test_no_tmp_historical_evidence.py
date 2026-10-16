"""Historical data classification never waives new runnable or edited content."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_no_tmp_literals.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_no_tmp_literals", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _receipt(mod, root):
    rel = next(iter(mod._historical_evidence()))
    raw = (SCRIPT.parents[1] / rel).read_bytes()
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return target, raw


def test_exact_original_data_allowed_but_modified_copied_moved_and_new_scanned(tmp_path):
    mod = _load()
    target, raw = _receipt(mod, tmp_path)
    assert mod.scan(root=tmp_path) == {}
    assert mod.scan([target], root=tmp_path) == {}
    copied = target.with_name("copied.json")
    copied.write_bytes(raw)
    fresh = target.with_name("new.json")
    fresh.write_text('{"output": "/tmp/new"}\n')
    assert set(mod.scan(root=tmp_path)) == {
        copied.relative_to(tmp_path).as_posix(), fresh.relative_to(tmp_path).as_posix(),
    }
    target.write_bytes(raw + b'\n{"extra": "/tmp/new"}\n')
    assert target.relative_to(tmp_path).as_posix() in mod.scan(root=tmp_path)
    target.write_bytes(raw)
    moved = tmp_path / "elsewhere" / target.name
    moved.parent.mkdir()
    target.rename(moved)
    assert moved.relative_to(tmp_path).as_posix() in mod.scan(root=tmp_path)


@pytest.mark.parametrize("rel,content", [
    ("analysis/sync-health/new.py", 'OUTPUT = "/tmp/bad"\n'),
    ("analysis/sync-health/new.sh", 'echo data > /tmp/bad\n'),
    ("analysis/sync-health/new.md", '```sh\necho data > /tmp/bad\n```\n'),
    ("skills/new/SKILL.md", 'Save results to /tmp/bad\n'),
    ("analysis/sync-health/new.json.parts/0001.txt", '{"output": "/tmp/bad"}\n'),
])
def test_sources_prompts_runnable_docs_and_new_parts_still_fail(tmp_path, rel, content):
    mod = _load()
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_text(content)
    assert rel in mod.scan(root=tmp_path)


def test_invalid_or_tampered_inventory_fails_cli_closed(tmp_path, monkeypatch, capsys):
    mod = _load()
    policy = tmp_path / "inventory.json"
    policy.write_bytes(mod._HISTORICAL_MAP.read_bytes() + b" ")
    monkeypatch.setattr(mod, "_HISTORICAL_MAP", policy)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    assert mod.main([]) == 1
    assert "digest mismatch" in capsys.readouterr().err
    policy.unlink()
    assert mod.main([]) == 1


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(schema_version=True),
    lambda d: d.update(purpose="arbitrary exemptions"),
    lambda d: d.update(files=[]),
    lambda d: d["files"].append(d["files"][0]),
    lambda d: d["files"][0].update(path="../outside.json"),
    lambda d: d["files"][0].update(path="/absolute.json"),
    lambda d: d["files"][0].update(path="analysis//sync-health/receipt.json"),
    lambda d: d["files"][0].update(path="analysis/sync-health/../receipt.json"),
    lambda d: d["files"][0].update(path="analysis\\sync-health\\receipt.json"),
    lambda d: d["files"][0].update(path="analysis/sync-health/runnable.md"),
    lambda d: d["files"][0].update(sha256="not-a-digest"),
])
def test_invalid_schema_or_paths_rejected_even_with_matching_map_digest(tmp_path, monkeypatch, mutation):
    mod = _load()
    data = json.loads(mod._HISTORICAL_MAP.read_bytes())
    mutation(data)
    raw = json.dumps(data).encode()
    policy = tmp_path / "inventory.json"
    policy.write_bytes(raw)
    monkeypatch.setattr(mod, "_HISTORICAL_MAP", policy)
    monkeypatch.setattr(mod, "_HISTORICAL_MAP_SHA256", hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError):
        mod.scan(root=tmp_path)


@pytest.mark.platforms("posix")
def test_symlink_to_receipt_never_inherits_classification(tmp_path):
    mod = _load()
    target, raw = _receipt(mod, tmp_path)
    other = tmp_path / "other.json"
    other.write_bytes(raw)
    target.unlink()
    target.symlink_to(other)
    assert target.relative_to(tmp_path).as_posix() in mod.scan([target], root=tmp_path)
