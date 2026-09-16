"""Tool-argument type coercion: repair string-typed values the model emitted against a tool's JSON Schema.

Models emit "42" for integers, "true" for booleans, JSON-encoded strings for
arrays/objects (also nested inside containers), and bare scalars where an array
is expected (wrapped in a one-element list). Coercion is schema-guided and
conservative: originals are kept whenever a repair is not unambiguous.
"""

import json
import logging
import re
from typing import Any, Dict

from tools.registry import registry

# Logger name kept as "model_tools": these messages were always emitted under
# that name and log-based tooling filters on it.
logger = logging.getLogger("model_tools")


def coerce_tool_args(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce string-typed args to their JSON-Schema types; originals kept on failure."""
    if not args or not isinstance(args, dict):
        return args

    schema = registry.get_schema(tool_name)
    properties = ((schema or {}).get("parameters") or {}).get("properties")
    if not properties:
        return args

    # The model saw the SANITIZED schema (provider-illegal property keys were
    # renamed); map those keys back to the registry's wire names first.
    try:
        from tools.schema_sanitizer import unrename_tool_args
        args = unrename_tool_args(schema.get("parameters"), args)
    except Exception:  # pragma: no cover — never break dispatch
        pass

    for key, value in list(args.items()):
        prop_schema = properties.get(key)
        if not prop_schema:
            continue
        expected = prop_schema.get("type")
        is_container = isinstance(value, (list, tuple))

        # ── Stringified-array guard for string-typed params (#1681) ──
        _expected_types = (
            {expected} if isinstance(expected, str) else set(expected or [])
        )
        _accepts_string = "string" in _expected_types
        _accepts_array = "array" in _expected_types
        if (
            _accepts_string
            and isinstance(value, str)
            and (
                value.lstrip().startswith("[")
                or ("," in value and _is_path_like_param(key))
            )
        ):
            if _accepts_array:
                parsed_list = _parse_stringified_array_to_list(value)
                if parsed_list is not None:
                    logger.info(
                        "coerce_tool_args: %s.%s received a stringified "
                        "array string (%.80s...) for a string|array union "
                        "param — parsing into native list of %d items.",
                        tool_name,
                        key,
                        value,
                        len(parsed_list),
                    )
                    args[key] = parsed_list
                    continue
            extracted = _extract_first_from_stringified_array(value)
            if extracted is not None:
                logger.info(
                    "coerce_tool_args: %s.%s received a stringified JSON "
                    "array string (%.80s...) for a string-typed param — "
                    "extracting first element.",
                    tool_name,
                    key,
                    value,
                )
                args[key] = extracted
                continue

        # Bare non-list value for an array schema. Strings go through
        # _coerce_value first so a JSON-encoded array is parsed and a nullable
        # "null" becomes None (not ["null"]). None itself is preserved: the tool's
        # own default handling decides between "omit" and "empty list".
        if expected == "array" and value is not None and not is_container:
            if isinstance(value, str):
                coerced = _coerce_value(value, expected, schema=prop_schema, context=f"{tool_name}.{key}")
                if coerced is not value:
                    args[key] = coerced
                    continue
                if value.strip().startswith("["):
                    logger.warning("coerce_tool_args: %s.%s looks like a JSON array string "
                                   "but could not be parsed — model may have emitted a "
                                   "JSON-encoded string instead of a native array. "
                                   "Falling back to single-element list.", tool_name, key)
                args[key] = [value]
                logger.info("coerce_tool_args: wrapped bare string in list for %s.%s", tool_name, key)
                continue
            args[key] = [value]
            logger.info("coerce_tool_args: wrapped bare %s in list for %s.%s", type(value).__name__, tool_name, key)
            continue

        if not isinstance(value, str):
            # Native container: still normalize JSON-encoded elements/sub-fields.
            if (expected == "array" and is_container) or (expected == "object" and isinstance(value, dict)):
                args[key] = _normalize_json_strings_for_schema(value, prop_schema)
            continue
        if not expected and not _schema_allows_null(prop_schema):
            continue
        coerced = _coerce_value(value, expected, schema=prop_schema, context=f"{tool_name}.{key}")
        if coerced is not value:
            args[key] = coerced
            if isinstance(coerced, (list, tuple, dict)):
                args[key] = _normalize_json_strings_for_schema(coerced, prop_schema)
            continue
        if (
            _schema_accepts_kind(prop_schema, "array")
            and not _schema_accepts_kind(prop_schema, "string")
        ):
            args[key] = [value]
            logger.warning(
                "coerce_tool_args: %s.%s JSON-parse failed for list-typed param "
                "(value %.80r) — wrapping in single-element list as recovery fallback",
                tool_name,
                key,
                value,
            )

    return args



_PATH_SEPARATOR_RE = re.compile(r"[/\\]|[.]\w{1,10}\Z|\.\.", re.IGNORECASE)
_PATH_LIKE_PARAM_RE = re.compile(
    r"path|file|url|dir|dest|src|dst|target|glob|pattern|prefix|output|input",
    re.IGNORECASE,
)


def _looks_like_path(token: str) -> bool:
    """Return True when *token* looks like a filesystem path."""
    if not token:
        return False
    if _PATH_SEPARATOR_RE.search(token):
        return True
    if any(token.startswith(p) for p in ("~", ".", "/")):
        return True
    return len(token) >= 3


def _is_path_like_param(key: str) -> bool:
    """Return True when *key* names a param that typically holds a path/URL."""
    if not key:
        return False
    return _PATH_LIKE_PARAM_RE.search(key) is not None


def _looks_like_path_list_rest(rest: str) -> bool:
    """Return True when *rest* looks like the remainder of a path list."""
    if not rest:
        return False
    return _PATH_SEPARATOR_RE.search(rest) is not None


def _split_path_list(value: str) -> list[str] | None:
    """Split a comma-separated / bracket-wrapped list of bare path-like items."""
    if not value or not value.strip():
        return None
    s = value.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    items = [part.strip().strip("'\"") for part in s.split(",") if part.strip()]
    if len(items) >= 2:
        return items
    return None


def _extract_first_from_stringified_array(value: str) -> str | None:
    """Extract the first element from a stringified JSON array (#1681)."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        if isinstance(first, str):
            return first
        return None
    _comma_list = re.match(
        r"^[\[\s]*(?P<first>[^,\[\]\s][^,]*?)"
        r"\s*,\s*(?P<rest>[^\]].*?)[\]\s]*$",
        stripped,
        re.DOTALL,
    )
    if not _comma_list:
        return None
    first_token = _comma_list.group("first").strip()
    rest = _comma_list.group("rest").strip()
    if _looks_like_path(first_token) and _looks_like_path_list_rest(rest):
        logger.info(
            "coerce_tool_args: extracted first item from comma-separated "
            "path list (%.60s...) for a string-typed param.",
            value,
        )
        return first_token
    return None


def _parse_stringified_array_to_list(value: str) -> list[str] | None:
    """Parse a stringified JSON array or comma-separated path list into a
    native ``list[str]`` (#1681 regression fix).
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, list) and parsed:
        if all(isinstance(item, str) for item in parsed):
            return parsed
        return None
    _comma_list = re.match(
        r"^[\[\s]*(?P<first>[^,\[\]\s][^,]*?)"
        r"\s*,\s*(?P<rest>[^\]].*?)[\]\s]*$",
        stripped,
        re.DOTALL,
    )
    if not _comma_list:
        return None
    first_token = _comma_list.group("first").strip()
    rest = _comma_list.group("rest").strip()
    if not (_PATH_SEPARATOR_RE.search(first_token) and _PATH_SEPARATOR_RE.search(rest)):
        return None
    inner = stripped.strip("[] \n\t")
    tokens = [t.strip() for t in inner.split(",")]
    path_tokens = [t for t in tokens if t and _PATH_SEPARATOR_RE.search(t)]
    if len(path_tokens) >= 2:
        return path_tokens
    return None


def _schema_accepts_kind(schema: Any, kind: str) -> bool:
    """True when *schema* permits JSON type *kind* via ``type`` or any anyOf/oneOf/allOf branch."""
    if not isinstance(schema, dict):
        return False
    t = schema.get("type")
    if t == kind or (isinstance(t, list) and kind in t):
        return True
    return any(isinstance(branches := schema.get(union_key), list) and any(_schema_accepts_kind(b, kind) for b in branches)
               for union_key in ("anyOf", "oneOf", "allOf"))


def _normalize_json_strings_for_schema(value: Any, schema: Any) -> Any:
    """Recursively parse JSON-encoded strings where the schema expects array/object.

    Schema-guided: a string is only parsed when its schema position expects a
    container, so legitimate JSON-looking ``type: string`` fields survive.
    Returns the same object when nothing changed (identity = cheap no-op check).

    Ported from cline/cline#11803, adapted to hermes-agent's coercion layer.
    """
    if not isinstance(schema, dict):
        return value

    if isinstance(value, str):
        trimmed = value.strip()
        expects_array = _schema_accepts_kind(schema, "array")
        expects_object = _schema_accepts_kind(schema, "object")
        if not ((expects_array and trimmed.startswith("[")) or (expects_object and trimmed.startswith("{"))):
            return value
        try:
            parsed = json.loads(trimmed)
        except (ValueError, TypeError):
            return value
        if not ((isinstance(parsed, list) and expects_array) or (isinstance(parsed, dict) and expects_object)):
            return value
        value = parsed

    if isinstance(value, list):
        items_schema = schema.get("items")
        if not isinstance(items_schema, dict):
            return value
        out = [_normalize_json_strings_for_schema(item, items_schema) for item in value]
        return out if any(n is not o for n, o in zip(out, value)) else value

    if isinstance(value, dict):
        props = schema.get("properties")
        if not isinstance(props, dict):
            return value
        out = dict(value)
        for k, prop_schema in props.items():
            if k in value and isinstance(prop_schema, dict):
                out[k] = _normalize_json_strings_for_schema(value[k], prop_schema)
        return out if any(out[k] is not v for k, v in value.items()) else value

    return value


def _coerce_value(value: str, expected_type, schema: dict | None = None, context: str = ""):
    """Coerce string *value* to *expected_type* (str or union list); original on failure."""
    if _schema_allows_null(schema) and value.strip().lower() == "null":
        return None

    if isinstance(expected_type, list):
        quiet = "string" in expected_type
        for t in expected_type:
            r = _coerce_value(value, t, schema=schema, context=context)
            if r is not value:
                return r
        return value

    if expected_type == "array":
        quiet = schema is not None and _schema_accepts_kind(schema, "string")
        return _coerce_json(value, list, context=context, quiet=quiet)
    if expected_type == "object":
        quiet = schema is not None and _schema_accepts_kind(schema, "string")
        return _coerce_json(value, dict, context=context, quiet=quiet)

    coercer = _SCALAR_COERCERS.get(expected_type)
    if coercer is not None:
        return coercer(value)
    return None if expected_type == "null" and value.strip().lower() == "null" else value


def _schema_allows_null(schema: dict | None) -> bool:
    """True when a JSON Schema fragment explicitly permits null."""
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == "null" or (isinstance(schema_type, list) and "null" in schema_type):
        return True
    if schema.get("nullable") is True:
        return True
    return any(isinstance(variants := schema.get(union_key), list)
               and any(isinstance(v, dict) and v.get("type") == "null" for v in variants)
               for union_key in ("anyOf", "oneOf"))


def _coerce_json(
    value: str, expected_python_type: type, context: str = "", quiet: bool = False
):
    """Parse *value* as JSON when the schema expects an array or object.

    ``context`` (e.g. ``"tool_name.param"``) is included in the parse-failure
    WARNING so the agent gets a deterministic recovery hint (issue #2953).
    ``quiet`` suppresses that WARNING when the value is already valid for a
    sibling union member (e.g. a scalar string on ``["string","array"]``),
    where a failed JSON parse is expected rather than an error.
    """
    if not value or not value.strip():
        if expected_python_type is list:
            return []
        if expected_python_type is dict:
            return {}
        return value
    name = expected_python_type.__name__
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as exc:
        if expected_python_type is list:
            items = _split_path_list(value)
            if items is not None:
                logger.debug(
                    "coerce_tool_args: split comma-separated list string for expected "
                    "type list: %r -> %r",
                    value,
                    items,
                )
                return items
        if not quiet:
            ctx = f"{context} " if context else ""
            logger.warning(
                "coerce_tool_args: %sfailed to parse string as JSON for expected type %s "
                "(value %.80r): %s",
                ctx,
                name,
                value,
                exc,
            )
        return value
    if isinstance(parsed, expected_python_type):
        logger.debug("coerce_tool_args: coerced string to %s via json.loads", name)
        return parsed
    logger.warning(
        "coerce_tool_args: JSON-parsed value is %s, expected %s — skipping coercion",
        type(parsed).__name__,
        name,
    )
    return value


def _coerce_number(value: str, integer_only: bool = False):
    """Parse *value* as a number; original string on failure, inf/nan, or decimals when integer_only."""
    try:
        f = float(value)
    except (ValueError, OverflowError):
        return value
    if f != f or f in (float("inf"), float("-inf")):
        return value  # not JSON-serializable
    return int(f) if f == int(f) else value if integer_only else f


def _coerce_boolean(value: str):
    """Parse "true"/"false" (case-insensitive); original string otherwise."""
    return {"true": True, "false": False}.get(value.strip().lower(), value)


# JSON-Schema scalar/container type -> coercer; "null" and unions are handled in _coerce_value.
_SCALAR_COERCERS = {
    "integer": lambda v: _coerce_number(v, integer_only=True),
    "number": _coerce_number,
    "boolean": _coerce_boolean,
    "array": lambda v: _coerce_json(v, list),
    "object": lambda v: _coerce_json(v, dict),
}
