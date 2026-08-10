"""Tests for decomposing the patch 'other' error bucket (#2244).

The generic "error" catch-all in classify_file_error was the 2nd largest
patch failure mode (55 failures/7d). This test verifies that common
sub-reasons — encoding/Unicode issues, line-ending conflicts, BOM markers,
and concurrent modification — now classify into specific categories with
targeted recovery hints instead of falling through to the generic bucket.
"""

import pytest

from tools.file_operations import classify_file_error


class TestEncodingErrorClassification:
    def test_unicode_decode_error_classified(self):
        error = "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff"
        cls, hint = classify_file_error(error)
        assert cls == "encoding_error"
        assert "write_file" in hint or "encoding" in hint.lower()

    def test_invalid_continuation_byte_classified(self):
        error = "invalid continuation byte at position 42"
        cls, _ = classify_file_error(error)
        assert cls == "encoding_error"

    def test_codec_cant_decode_classified(self):
        error = "'utf-8' codec can't decode byte 0x80 in position 0"
        cls, _ = classify_file_error(error)
        assert cls == "encoding_error"


class TestLineEndingConflictClassification:
    def test_crlf_conflict_classified(self):
        error = "Line ending mismatch: file uses CRLF"
        cls, hint = classify_file_error(error)
        assert cls == "line_ending_conflict"
        assert "line ending" in hint.lower() or "write_file" in hint

    def test_line_ending_hyphenated_classified(self):
        error = "Line-ending conflict detected"
        cls, _ = classify_file_error(error)
        assert cls == "line_ending_conflict"


class TestBomConflictClassification:
    def test_bom_in_error_classified(self):
        error = "BOM marker found at start of file"
        cls, hint = classify_file_error(error)
        assert cls == "bom_conflict"
        assert "BOM" in hint or "write_file" in hint

    def test_ufeff_classified(self):
        error = "Unexpected U+FEFF character at position 0"
        cls, _ = classify_file_error(error)
        assert cls == "bom_conflict"


class TestConcurrentModificationClassification:
    def test_concurrent_modification_classified(self):
        error = "concurrent modification detected"
        cls, hint = classify_file_error(error)
        assert cls == "concurrent_modification"
        assert "re-read" in hint.lower() or "retry" in hint.lower()

    def test_file_modified_since_read_classified(self):
        error = "File was modified since last read"
        cls, _ = classify_file_error(error)
        assert cls == "concurrent_modification"

    def test_stale_handle_classified(self):
        error = "stale file handle"
        cls, _ = classify_file_error(error)
        assert cls == "concurrent_modification"


class TestNoRegression:
    """Existing classifications still work correctly."""

    def test_permission_denied_still_works(self):
        cls, _ = classify_file_error("Permission denied")
        assert cls == "permission"

    def test_not_found_still_works(self):
        cls, _ = classify_file_error("File not found: /tmp/foo.txt")
        assert cls == "not_found"

    def test_generic_error_still_falls_through(self):
        """Truly unknown errors still get the generic 'error' class."""
        cls, _ = classify_file_error("Something completely unexpected happened")
        assert cls == "error"
