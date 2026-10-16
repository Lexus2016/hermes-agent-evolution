"""Windows BOM-prefixed metrics inputs remain readable without changing writes."""

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_json_and_jsonl_loaders_accept_windows_bom(tmp_path, monkeypatch, bom):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from evolution_agentopt_report import load_records
    from evolution_funnel import _load_json

    record = {"task_class": "review", "outcome": "success"}
    payload = bom + json.dumps(record).encode("utf-8")
    json_path = tmp_path / "record.json"
    json_path.write_bytes(payload)
    assert _load_json(json_path) == record
    lines_path = tmp_path / "records.jsonl"
    lines_path.write_bytes(payload + b"\nnot-json\n[]\n")
    assert load_records(lines_path) == [record]
    assert json_path.read_bytes() == payload
