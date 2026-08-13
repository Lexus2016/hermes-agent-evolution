"""Tests for source-chain provenance (tools/skill_provenance.py)."""

from tools.skill_provenance import (
    set_current_write_origin,
    reset_current_write_origin,
    init_source_chain,
    reset_source_chain,
    add_provenance_entry,
    get_recorded_chain,
    get_skill_provenance,
    BACKGROUND_REVIEW,
    provenance_ok,
    record_promotion,
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


def test_provenance_ok_empty_chain():
    """No chain at all → rejected (no attribution)."""
    ok, reason = provenance_ok(chain=[])
    assert ok is False
    assert "no source_chain" in reason


def test_provenance_ok_no_trusted_sources():
    """Chain with only untrusted sources → rejected."""
    chain = [
        {
            "source_type": "web_extract",
            "source_id": "https://evil.example.com",
            "trusted": False,
        },
        {"source_type": "web_search", "source_id": "query", "trusted": False},
    ]
    ok, reason = provenance_ok(chain=chain)
    assert ok is False
    assert "no trusted sources" in reason


def test_provenance_ok_has_trusted_source():
    """Chain with at least one trusted source → passes."""
    chain = [
        {
            "source_type": "web_extract",
            "source_id": "https://example.com",
            "trusted": False,
        },
        {"source_type": "terminal", "source_id": "/tmp/proof", "trusted": True},
    ]
    ok, reason = provenance_ok(chain=chain)
    assert ok is True
    assert reason == ""


def test_provenance_ok_all_trusted():
    """Chain where every source is trusted → passes."""
    chain = [
        {"source_type": "read_file", "source_id": "/tmp/a", "trusted": True},
        {"source_type": "execute_code", "source_id": "cell1", "trusted": True},
    ]
    ok, _ = provenance_ok(chain=chain)
    assert ok is True


def test_provenance_ok_reads_live_chain():
    """provenance_ok() with no arg reads the live ContextVar chain."""
    token = init_source_chain()
    try:
        ok, reason = provenance_ok()
        assert ok is False  # empty live chain
        wtoken = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            add_provenance_entry("search_files", "/tmp")
            ok, _ = provenance_ok()
            assert ok is True  # trusted source present
        finally:
            reset_current_write_origin(wtoken)
    finally:
        reset_source_chain(token)


def test_record_promotion_best_effort():
    """record_promotion never raises even for a nonexistent skill."""
    record_promotion("nonexistent-skill-xyz", reason="test")
