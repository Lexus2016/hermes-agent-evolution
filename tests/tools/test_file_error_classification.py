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

    def test_could_not_find_match_is_fuzzy(self):
        """The fuzzy matcher's dominant failure message — "Could not find a
        match for old_string in the file" — routes to fuzzy_match recovery,
        not the generic 'error' class (#1537). Before the fix this message
        contained neither "did not match" nor "no match", so the model got
        the unhelpful "The operation failed. ... CHANGE the call." recovery
        with no signal to re-read the file, driving the observed retry
        spirals (up to 6 deep)."""
        klass, rec = classify_file_error(
            "Could not find a match for old_string in the file"
        )
        assert klass == "fuzzy_match" and ("Re-read" in rec or "EXACT" in rec)

    def test_ambiguous_match_decomposed_replace_all(self):
        """'Found N matches' errors mentioning replace_all → replace_all_intent (#2354).

        The ambiguous_match bucket is decomposed into subclasses so the
        recovery hint tells the model exactly what to change. This error
        mentions replace_all → the model likely wants all occurrences.
        """
        klass, rec = classify_file_error(
            "Found 3 matches for old_string at:\n  Line 10: def foo()\n"
            "Provide more context to make it unique, or use replace_all=True."
        )
        assert klass == "replace_all_intent"
        assert "replace_all" in rec
        assert "Do NOT" in rec

    def test_verification(self):
        klass, _ = classify_file_error("Post-write verification failed: could not re-read x")
        assert klass == "verification"

    def test_binary(self):
        klass, _ = classify_file_error("Binary file - cannot display as text.")
        assert klass == "binary"

    def test_directory(self):
        """read_file on a directory yields the actionable 'directory' class (#1681)."""
        klass, rec = classify_file_error(
            "Cannot read a directory: /tmp/x is a directory, not a file."
        )
        assert klass == "directory"
        assert "search_files" in rec

    def test_directory_variant_patch(self):
        """The patch operation variant routes to the same directory class."""
        klass, rec = classify_file_error(
            "Cannot patch a directory: /tmp/dir is a directory, not a file."
        )
        assert klass == "directory"
        assert "search_files" in rec

    def test_unknown_falls_back_to_error(self):
        klass, rec = classify_file_error("something inexplicable happened")
        assert klass == "error" and "CHANGE the call" in rec

    # ── #1586: sub-classify the fuzzy matcher's distinctive failure strings ──
    # These previously collapsed into the generic "error" (other) bucket, which
    # was 87% of patch failures (68/78). Each now gets a targeted error_class
    # and recovery so the model can correct on its next turn.

    def test_escape_drift_from_raw_string(self):
        """The fuzzy matcher's 'Escape-drift detected: ...' string now classifies
        as escape_drift instead of the generic 'error' bucket."""
        klass, rec = classify_file_error(
            "Escape-drift detected: old_string and new_string contain a "
            "literal backslash-quote sequence but the matched region does not."
        )
        assert klass == "escape_drift"
        assert "backslash" in rec

    def test_escape_drift_no_hyphen_variant(self):
        klass, _ = classify_file_error("Escape drift detected in old_string")
        assert klass == "escape_drift"

    def test_old_string_empty_from_raw_string(self):
        klass, rec = classify_file_error("old_string cannot be empty")
        assert klass == "old_string_empty"
        assert "non-empty" in rec

    def test_old_string_empty_variant(self):
        klass, _ = classify_file_error("old_string is empty")
        assert klass == "old_string_empty"

    def test_indentation_mismatch_promoted_from_structured_error(self):
        """When the raw error is the generic 'Could not find a match' but the
        structured_error diagnostic pinpointed indentation_mismatch, the class is
        promoted to indentation_mismatch (#1586). This is the core fix: the
        finer classification is already computed by fuzzy_match — we just surface
        it instead of collapsing to the coarse 'fuzzy_match' bucket."""
        klass, rec = classify_file_error(
            "Could not find a match for old_string in the file",
            structured_error=(
                "Error type: indentation_mismatch — indentation differs between "
                "old_string and file content\n\nFile: /x.py"
            ),
        )
        assert klass == "indentation_mismatch"
        assert "whitespace" in rec or "indent" in rec

    def test_structured_error_ignored_when_unrelated(self):
        """A structured_error whose label we don't promote must NOT change the
        coarse classification — the generic fuzzy_match arm still wins."""
        klass, _ = classify_file_error(
            "Could not find a match for old_string in the file",
            structured_error="Error type: no_match — old_string not found in file",
        )
        assert klass == "fuzzy_match"

    def test_structured_error_promotes_from_tail_error_bucket(self):
        """An otherwise-unclassifiable raw string still benefits from the
        structured diagnostic before falling back to the catch-all 'error'."""
        klass, _ = classify_file_error(
            "something weird happened",
            structured_error="Error type: escape_drift — serialization artifact",
        )
        assert klass == "escape_drift"

    def test_no_structured_error_keeps_legacy_behavior(self):
        """Callers that don't pass structured_error get identical behavior to
        before — backward compatible."""
        klass, _ = classify_file_error("Could not find a match for old_string in the file")
        assert klass == "fuzzy_match"


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

    def test_patch_result_ambiguous_decomposed_replace_all(self):
        """PatchResult with ambiguous-match error mentioning replace_all (#2354)."""
        d = PatchResult(
            success=False,
            error="Found 3 matches for old_string at:\n  Line 10: x\n"
            "Provide more context to make it unique, or use replace_all=True.",
        ).to_dict()
        assert d["error_class"] == "replace_all_intent"
        assert "replace_all" in d["recovery"]

    def test_patch_result_success_clean(self):
        d = PatchResult(success=True, diff="--- a").to_dict()
        assert "error_class" not in d
