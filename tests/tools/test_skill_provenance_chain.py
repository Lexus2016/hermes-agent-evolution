"""Tests for source-chain provenance (tools/skill_provenance.py)."""

from tools.skill_provenance import (
    set_current_write_origin, reset_current_write_origin,
    init_source_chain, reset_source_chain, add_provenance_entry,
    get_recorded_chain, get_skill_provenance, BACKGROUND_REVIEW,
)


def test_add_entry_guards():
    """add_provenance_entry is a no-op outside background-review or without chain."""
    token = init_source_chain()
    try:
        add_provenance_entry("terminal", "/tmp")  # not in bg review
        assert get_recorded_chain() == []
        wtoken = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            add_provenance_entry("terminal", "/tmp")
            chain = get_recorded_chain()
            assert len(chain) == 1 and chain[0]["trusted"] is True
        finally:
            reset_current_write_origin(wtoken)
    finally:
        reset_source_chain(token)
    # No chain initialized
    wtoken = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        add_provenance_entry("terminal", "x")
        assert get_recorded_chain() == []
    finally:
        reset_current_write_origin(wtoken)


def test_untrusted_classification():
    wtoken = set_current_write_origin(BACKGROUND_REVIEW)
    token = init_source_chain()
    try:
        add_provenance_entry("web_extract", "https://example.com")
        add_provenance_entry("terminal", "/tmp/file")
        chain = get_recorded_chain()
        assert chain[0]["trusted"] is False and chain[1]["trusted"] is True
    finally:
        reset_source_chain(token)
        reset_current_write_origin(wtoken)


def test_get_skill_provenance_empty():
    assert get_skill_provenance("nonexistent-skill-xyz") == []