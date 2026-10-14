"""write_file refusals carry a ``reason`` subclass + ``recovery_hint`` (#3468).

Before this, 32/34 write_file failures in the 7d introspection window (94%,
0.139/session) landed in the opaque 'other' bucket: the envelope said a write
failed but never *why*, so the model blind-retried (the documented 15-deep
write_file spiral) and the digest could not attribute a regression in one mode
(#1325). These test the additive envelope contract plus the tag vocabulary that
scripts/introspection_extract.py mirrors for the digest.
"""
import json

from tools.file_tools import write_file_tool
from tools.file_tools_write_guards import _stale_write_refusal, write_failure_reason


def test_taxonomy_tags_the_known_guard_refusals():
    cases = {
        "Refusing to write to sensitive system path: /etc/hosts": "write-sensitive-path",
        "Refusing to write to Hermes config file: the active profile config": "write-sensitive-path",
        "Refusing to write plain text to binary document 'x.pdf' (.pdf)": "write-binary-content",
        "/tmp/x.py has not seen its full current content": "write-stale-overwrite",
        "Refusing to write internal read_file display text as file content": "write-internal-content",
        "target path is a directory": "write-path-is-directory",
    }
    for message, expected in cases.items():
        reason, hint = write_failure_reason(message)
        assert reason == expected, message
        assert hint  # a tag without a next step is only half of the fix


def test_unknown_message_stays_a_bare_error():
    """Additive by contract: outside the taxonomy the envelope is unchanged."""
    assert write_failure_reason("something unusual happened") == (None, None)
    assert write_failure_reason("") == (None, None)


def test_sensitive_path_refusal_is_tagged_on_the_envelope():
    payload = json.loads(write_file_tool("/etc/hermes-evolution-3468-probe", "x", task_id="t3468"))
    assert payload["error"]
    assert payload["reason"] == "write-sensitive-path"
    assert payload["recovery_hint"]


def test_stale_write_refusal_payload_is_tagged():
    payload = _stale_write_refusal("a.py", "changed on disk", "a.py")
    assert payload["stale_write_blocked"] is True
    assert payload["reason"] == "write-stale-overwrite"
    assert "read_file" in payload["recovery_hint"]
