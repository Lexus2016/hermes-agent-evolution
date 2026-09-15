"""Result dataclasses and pure text helpers shared by ``tools.file_operations``
and its search/lint mixins. Leaf module (imports nothing from ``tools`` at module
scope) so nothing cycles. The ``to_dict`` output of every class here IS tool
behavior — key names, order and omission rules are pinned by tests and read by
the model.
"""

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Tuple


def classify_file_error(
    error: Optional[str],
    *,
    similar_files: Optional[List[str]] = None,
    structured_error: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """Classify file operation errors into structured error_class and recovery guidance (#216)."""
    if not error:
        return None

    error_lower = error.lower()

    # Promotion from structured_error if present
    if structured_error:
        struct_lower = structured_error.lower()
        if "error type: indentation_mismatch" in struct_lower:
            return (
                "indentation_mismatch",
                "Indentation mismatch detected. Inspect whitespace/indentation of old_string relative to the file content.",
            )
        if "error type: escape_drift" in struct_lower and ("escape" in error_lower or "something" in error_lower):
            return (
                "escape_drift",
                "Escape-drift detected. Check for literal backslash sequences in old_string / new_string.",
            )

    # 1. Permission / protected path
    if "write denied" in error_lower or "protected" in error_lower or "permission" in error_lower:
        return (
            "permission",
            "Write denied for protected path. Check allowed path permissions or choose a valid workspace location.",
        )

    # 2. Directory
    if "cannot read a directory" in error_lower or "cannot patch a directory" in error_lower or "is a directory" in error_lower:
        return (
            "directory",
            "Target is a directory. Use search_files or list_dir instead.",
        )

    # 3. Not found / fuzzy match with similar files
    if "file not found" in error_lower or "no such file" in error_lower or "failed to read file" in error_lower:
        if similar_files:
            sim_str = ", ".join(similar_files)
            return (
                "fuzzy_match",
                f"File not found. Did you mean one of these similar files: {sim_str}?",
            )
        return (
            "not_found",
            "File does not exist. Use write_file to create new files or search_files to find existing ones.",
        )

    # 4. Binary / image
    if "binary file" in error_lower or "cannot display as text" in error_lower:
        return (
            "binary",
            "Binary file cannot be displayed or edited as text.",
        )

    # 5. Verification failure
    if "verification failed" in error_lower:
        return (
            "verification",
            "Post-write verification failed. Re-read the file to inspect the resulting content.",
        )

    # 6. Escape drift
    if "escape-drift" in error_lower or "escape drift" in error_lower:
        return (
            "escape_drift",
            "Escape drift detected in backslash sequences. Ensure exact escaping matches the target file.",
        )

    # 7. Old string empty
    if "old_string cannot be empty" in error_lower or "old_string is empty" in error_lower:
        return (
            "old_string_empty",
            "old_string must be non-empty. Provide the exact text to replace.",
        )

    # 8. Ambiguous match / replace all intent (#2354)
    if "found" in error_lower and "matches" in error_lower and "old_string" in error_lower:
        if "replace_all" in error_lower:
            return (
                "replace_all_intent",
                "Multiple matches found and the resolution suggests replace_all. "
                "If you intend to replace ALL occurrences, re-send the patch with "
                "replace_all=True. Do NOT repeat ambiguous single replacements.",
            )
        if "more context" in error_lower or "surrounding context" in error_lower or "longer" in error_lower:
            return (
                "ambiguous_insufficient_context",
                "old_string is too short to be unique — multiple regions match. "
                "Re-read the file with read_file, then include more surrounding "
                "context lines (function signature, class name, unique nearby lines) "
                "so only one location matches. Do NOT retry the same old_string — "
                "it is inherently ambiguous.",
            )
        return (
            "ambiguous_not_unique",
            "Multiple matches found for old_string — it is not unique. Do NOT "
            "retry the same old_string (it will match the same locations again). "
            "Either add more surrounding context to make it unique, or use "
            "replace_all=True if you intend to replace all occurrences.",
        )

    # 9. Patch parse failure
    if "failed to parse patch" in error_lower or "invalid patch" in error_lower:
        return (
            "patch_parse",
            "Failed to parse patch format. Check patch syntax and unified diff headers.",
        )

    # 10. Fuzzy match / block no match
    if (
        "search block did not match" in error_lower
        or "could not find a match" in error_lower
        or "did not match the file content" in error_lower
    ):
        finer = _class_from_structured_error(structured_error)
        if finer:
            return finer
        return (
            "fuzzy_match",
            "Re-read the file to get the EXACT lines including whitespace and line breaks before retrying.",
        )

    # 11. Decompose remaining "other" failures (#2244)
    if (
        "unicode" in error_lower
        or "codec can't decode" in error_lower
        or "invalid byte" in error_lower
        or "invalid continuation byte" in error_lower
        or "can't decode" in error_lower
    ):
        return (
            "encoding_error",
            "The file has a byte sequence that can't be decoded as UTF-8. "
            "It may be a non-UTF-8 encoding or contain invalid bytes. "
            "Use write_file to replace the content, or handle the encoding "
            "explicitly via execute_code.",
        )
    if "line ending" in error_lower or "crlf" in error_lower or "line-ending" in error_lower:
        return (
            "line_ending_conflict",
            "The file uses different line endings (CRLF vs LF) than "
            "old_string. Re-read the file and copy the EXACT line endings, "
            "or use write_file to replace the whole file.",
        )
    if "bom" in error_lower or "ufeff" in error_lower or "u+feff" in error_lower or "byte order mark" in error_lower:
        return (
            "bom_conflict",
            "The file has a UTF-8 BOM (byte order mark) prefix that "
            "interferes with the match. Re-read the file from line 1 and "
            "include the BOM in old_string, or use write_file.",
        )
    if (
        ("concurrent" in error_lower)
        or ("modified" in error_lower and "since" in error_lower)
        or ("changed" in error_lower and "since" in error_lower)
        or ("stale" in error_lower and "handle" in error_lower)
    ):
        return (
            "concurrent_modification",
            "The file was modified between the read and the write. "
            "Re-read the current content and retry the patch against the "
            "latest version.",
        )

    # Last resort: the structured diagnostic may still classify an otherwise
    # generic message before we fall back to the catch-all "error" bucket (#1586).
    finer = _class_from_structured_error(structured_error)
    if finer:
        return finer

    # Fallback to generic error
    return (
        "error",
        "The operation failed with an unexpected error. Review the error message and CHANGE the call arguments.",
    )


# Sub-class recovery hints for causes the fuzzy matcher pinpoints in its
# structured_error field but whose raw error string is generic (#1586).
# Keys mirror the labels emitted by tools.fuzzy_match.classify_error /
# format_structured_error ("Error type: <key> — ...").
_STRUCTURED_SUBCLASS_RECOVERY: Dict[str, Tuple[str, str]] = {
    "indentation_mismatch": (
        "indentation_mismatch",
        "The text matches but the leading whitespace differs (spaces vs tabs, "
        "indent level). Re-read the file and copy the EXACT indentation into "
        "old_string, or use write_file to replace the whole region.",
    ),
    "escape_drift": (
        "escape_drift",
        "Tool-call serialization added a spurious backslash before a quote/apostrophe. "
        "Re-read the file and pass old_string/new_string without backslash-escaping "
        "those characters.",
    ),
    "old_string_empty": (
        "old_string_empty",
        "old_string was empty. Provide a non-empty search block; use read_file to "
        "see the current content if needed.",
    ),
}


def _class_from_structured_error(
    structured_error: Optional[str],
) -> Optional[Tuple[str, str]]:
    """Extract a ``(error_class, recovery)`` from a fuzzy_match structured_error
    string, if it advertises a known sub-class (#1586)."""
    if not structured_error:
        return None
    m = re.search(r"Error type:\s*([a-z_]+)", structured_error, re.IGNORECASE)
    if not m:
        return None
    label = m.group(1).lower()
    return _STRUCTURED_SUBCLASS_RECOVERY.get(label)


@dataclass
class ReadResult:
    """Result from reading a file."""
    content: str = ""
    total_lines: int = 0
    file_size: int = 0
    truncated: bool = False
    hint: Optional[str] = None
    is_binary: bool = False
    is_image: bool = False
    base64_content: Optional[str] = None
    mime_type: Optional[str] = None
    dimensions: Optional[str] = None  # For images: "WIDTHxHEIGHT"
    error: Optional[str] = None
    similar_files: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v is not None and v != []}
        if self.error:
            classified = classify_file_error(self.error, similar_files=self.similar_files)
            if classified:
                d["error_class"] = classified[0]
                d["recovery"] = classified[1]
        return d


@dataclass
class WriteResult:
    """Result from writing a file."""
    bytes_written: int = 0
    dirs_created: bool = False
    # True when the on-disk sha256 matched the intended content; None when the
    # backend couldn't verify (no sha256sum). A mismatch is a hard error, never a flag.
    verified: Optional[bool] = None
    lint: Optional[Dict[str, Any]] = None
    # LSP semantic diagnostics, kept separate from ``lint`` (syntax) so the model
    # reads the two as independent signals. None when LSP is off/inapplicable.
    lsp_diagnostics: Optional[str] = None
    error: Optional[str] = None
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class PatchResult:
    """Result from patching a file."""
    success: bool = False
    diff: str = ""
    files_modified: List[str] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)
    similar_files: List[str] = field(default_factory=list)
    lint: Optional[Dict[str, Any]] = None
    lsp_diagnostics: Optional[str] = None  # see WriteResult.lsp_diagnostics
    error: Optional[str] = None
    # Success-shaped no-op: the edit was already present, nothing written; ``note`` says why.
    no_change: bool = False
    note: Optional[str] = None
    structured_error: Optional[str] = None

    # Emission order is part of the output contract.
    _DICT_FIELDS: ClassVar[tuple] = (
        "diff", "files_modified", "files_created", "files_deleted",
        "similar_files", "lint", "lsp_diagnostics", "error",
    )

    def to_dict(self) -> dict:
        result: Dict[str, Any] = {"success": self.success}
        if self.no_change:
            result["no_change"] = True
        if self.note:
            result["note"] = self.note
        for key in self._DICT_FIELDS:
            value = getattr(self, key, None)
            if value is not None and value != []:
                result[key] = value
        if self.error:
            classified = classify_file_error(
                self.error,
                similar_files=self.similar_files,
                structured_error=self.structured_error,
            )
            if classified:
                result["error_class"] = classified[0]
                result["recovery"] = classified[1]
        if self.structured_error:
            result["_diagnostic"] = self.structured_error
        return result


@dataclass
class SearchMatch:
    """A single search match."""
    path: str
    line_number: int
    content: str
    mtime: float = 0.0  # Modification time for sorting


@dataclass
class SearchResult:
    """Result from searching."""
    matches: List[SearchMatch] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    total_count: int = 0
    truncated: bool = False
    limit_reason: Optional[str] = None
    warning: Optional[str] = None
    error: Optional[str] = None

    # Below this many matches the verbose array is already compact enough that
    # a path-grouping header would cost more tokens than it saves.
    _DENSIFY_MIN_MATCHES: ClassVar[int] = 5

    def _densify_matches(self) -> Optional[str]:
        """Lossless path-grouped text block: path once, then ``  <line>: <content>``
        rows. Relies on rg/grep emitting a file's hits consecutively. None when
        too few matches to be worth it."""
        if len(self.matches) < self._DENSIFY_MIN_MATCHES:
            return None
        lines: list[str] = []
        current_path: Optional[str] = None
        for m in self.matches:
            if m.path != current_path:
                lines.append(m.path)
                current_path = m.path
            # rstrip only: leading indentation is meaningful code and kept verbatim.
            lines.append(f"  {m.line_number}: {m.content.rstrip()}")
        return "\n".join(lines)

    def to_dict(self, densify: bool = False) -> dict:
        result: dict[str, object] = {"total_count": self.total_count}
        if self.matches:
            dense = self._densify_matches() if densify else None
            if dense is not None:
                # Self-describing so the model never guesses the block's shape.
                result["matches_format"] = (
                    "path-grouped: each file path on its own line, followed by "
                    "indented '<line>: <content>' rows for matches in that file"
                )
                result["matches_text"] = dense
            else:
                result["matches"] = [
                    {"path": m.path, "line": m.line_number, "content": m.content} for m in self.matches
                ]
        if self.files:
            result["files"] = self.files
        if self.counts:
            result["counts"] = self.counts
        if self.truncated:
            result["truncated"] = True
            result["total_count_is_lower_bound"] = True
        for key in ("limit_reason", "warning", "error"):
            value = getattr(self, key)
            if value:
                result[key] = value
        return result


@dataclass
class LintResult:
    """Result from linting a file."""
    success: bool = True
    skipped: bool = False
    output: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        if self.skipped:
            return {"status": "skipped", "message": self.message}
        result = {"status": "ok" if self.success else "error", "output": self.output}
        if self.message:
            result["message"] = self.message
        return result


@dataclass
class ExecuteResult:
    """Result from executing a shell command."""
    stdout: str = ""
    exit_code: int = 0


# ---------------------------------------------------------------------------
# Pure text helpers (no I/O)
# ---------------------------------------------------------------------------

_OSC_SEQUENCE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_FENCE_MARKER_RE = re.compile(r"'?\x07?__HERMES_FENCE_[A-Za-z0-9]+__\x07?'?")


def _strip_terminal_fence_leaks(text: str) -> str:
    """Strip leaked terminal fence wrappers (OSC sequences, fence markers) from
    command output; drops lines that were nothing but wrapper."""
    if not text:
        return text
    cleaned_lines: List[str] = []
    for line in text.splitlines(keepends=True):
        had_terminal_wrapper = "__HERMES_FENCE_" in line or "\x1b]" in line
        cleaned = _FENCE_MARKER_RE.sub("", _OSC_SEQUENCE_RE.sub("", line)).replace("\x07", "")
        if had_terminal_wrapper and cleaned.strip("'\r\n\t ") == "":
            continue
        cleaned_lines.append(cleaned)
    return "".join(cleaned_lines)


def _detect_line_ending(sample: str) -> Optional[str]:
    """Dominant line ending of ``sample`` (``\\r\\n`` if any CRLF in the first 4KB,
    else ``\\n``), or None for empty/single-line content. Preserves a file's
    endings across write_file/patch: bare-LF tool args would otherwise silently
    normalize CRLF files, and patch would produce mixed endings."""
    head = sample[:4096] if sample else ""
    if "\r\n" in head:
        return "\r\n"
    if "\n" in head:
        return "\n"
    return None


def _normalize_line_endings(text: str, target: str) -> str:
    """Convert every line ending (CRLF, lone CR, LF) in ``text`` to ``target``.
    Idempotent. Collapses to LF first — separate replacements would
    double-convert CRLF → LFLF."""
    lf_normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "\n":
        return lf_normalized
    if target == "\r\n":
        return lf_normalized.replace("\n", "\r\n")
    return text


# UTF-8 BOM (EF BB BF == U+FEFF), prepended by some Windows editors. Stripped on
# read so the model never sees a phantom first character (and patch's first-line
# match works), restored on write when the on-disk file had one.
_UTF8_BOM = "\ufeff"


def _strip_bom(text: str) -> tuple[str, bool]:
    """Return (text-without-leading-BOM, had_bom). Only a leading BOM is
    stripped; mid-content U+FEFF is legitimate data."""
    if _has_bom(text):
        return text[len(_UTF8_BOM):], True
    return text, False


def _has_bom(text: Optional[str]) -> bool:
    """True if ``text`` begins with a UTF-8 BOM."""
    return bool(text) and text.startswith(_UTF8_BOM)


# ---------------------------------------------------------------------------
# Pagination clamps
# ---------------------------------------------------------------------------

DEFAULT_READ_OFFSET = 1
DEFAULT_READ_LIMIT = 2000
DEFAULT_SEARCH_OFFSET = 0
DEFAULT_SEARCH_LIMIT = 50


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort integer coercion for tool pagination inputs."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_read_pagination(offset: Any = DEFAULT_READ_OFFSET,
                              limit: Any = DEFAULT_READ_LIMIT) -> tuple[int, int]:
    """Clamp read_file pagination so invalid values can never reach a sed range
    like ``0,-1p`` (schemas declare bounds, but not every caller enforces them).
    The ``limit`` ceiling is ``tool_output.max_lines`` from config.yaml."""
    from tools.tool_output_limits import get_max_lines
    normalized_offset = max(1, _coerce_int(offset, DEFAULT_READ_OFFSET))
    normalized_limit = max(1, min(_coerce_int(limit, DEFAULT_READ_LIMIT), get_max_lines()))
    return normalized_offset, normalized_limit


def normalize_search_pagination(offset: Any = DEFAULT_SEARCH_OFFSET,
                                limit: Any = DEFAULT_SEARCH_LIMIT) -> tuple[int, int]:
    """Return safe search pagination bounds for shell head/tail pipelines."""
    return max(0, _coerce_int(offset, DEFAULT_SEARCH_OFFSET)), max(1, _coerce_int(limit, DEFAULT_SEARCH_LIMIT))
