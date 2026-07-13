"""Tests for read/patch structured error_class + recovery routing (#216)."""

from tools.file_operations import (
    PatchResult,
    ReadResult,
    classify_file_error,
)


class TestClassifyFileError:
    def test_none_when_no_error(self):
        assert classify_file_error(None) is None
        assert classify_file_error("") is None

    def test_permission(self):
        klass, rec = classify_file_error("Write denied: '/etc/x' is a protected system/credential file.")
        assert klass == "permission" and "allowed path" in rec

    def test_not_found_without_similars_routes_to_write_file(self):
        klass, rec = classify_file_error("File not found: /tmp/missing.py")
        assert klass == "not_found" and "write_file" in rec

    def test_not_found_with_similars_is_fuzzy_match(self):
        klass, rec = classify_file_error(
            "File not found: /tmp/utils.py", similar_files=["/tmp/util.py", "/tmp/utils2.py"]
        )
        assert klass == "fuzzy_match" and "util.py" in rec

    def test_patch_parse(self):
        klass, rec = classify_file_error("Failed to parse patch: bad context")
        assert klass == "patch_parse"

    def test_block_no_match_is_fuzzy(self):
        klass, rec = classify_file_error("search block did not match the file content")
        assert klass == "fuzzy_match" and "EXACT" in rec

    def test_ambiguous_match_is_classified(self):
        """'Found N matches' errors get a specific error_class and recovery (#976)."""
        klass, rec = classify_file_error(
            "Found 3 matches for old_string at:\n  Line 10: def foo()\n"
            "Provide more context to make it unique, or use replace_all=True."
        )
        assert klass == "ambiguous_match"
        assert "replace_all" in rec or "context" in rec

    def test_verification(self):
        klass, _ = classify_file_error("Post-write verification failed: could not re-read x")
        assert klass == "verification"

    def test_binary(self):
        klass, _ = classify_file_error("Binary file - cannot display as text.")
        assert klass == "binary"

    def test_unknown_falls_back_to_error(self):
        klass, rec = classify_file_error("something inexplicable happened")
        assert klass == "error" and "CHANGE the call" in rec


class TestResultToDict:
    def test_read_result_success_has_no_error_class(self):
        d = ReadResult(content="hi", total_lines=1).to_dict()
        assert "error_class" not in d and "recovery" not in d

    def test_read_result_not_found_routes(self):
        d = ReadResult(error="File not found: /x", similar_files=["/y"]).to_dict()
        assert d["error_class"] == "fuzzy_match"
        assert "recovery" in d

    def test_patch_result_error_classified(self):
        d = PatchResult(success=False, error="Failed to parse patch: x").to_dict()
        assert d["error_class"] == "patch_parse" and "recovery" in d

    def test_patch_result_ambiguous_match_classified(self):
        """PatchResult with ambiguous-match error gets error_class (#976)."""
        d = PatchResult(
            success=False,
            error="Found 3 matches for old_string at:\n  Line 10: x\n"
            "Provide more context to make it unique, or use replace_all=True.",
        ).to_dict()
        assert d["error_class"] == "ambiguous_match"
        assert "replace_all" in d["recovery"] or "context" in d["recovery"]

    def test_patch_result_success_clean(self):
        d = PatchResult(success=True, diff="--- a").to_dict()
        assert "error_class" not in d
