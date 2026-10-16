"""Restore stored transcript metadata without changing provider-facing payloads."""
from agent.message_metadata import normalize_message_origin as _normalize_message_origin
from hermes_state_common import _json_or


def restore_replay_metadata(db, row, msg, *, include_summary_markers):
    # Provenance must round-trip: compress() and every other consumer
    # read the in-memory dict, never the row, so a column alone would
    # be invisible to them.
    # Normalised on the way OUT as well as in: the column carries no
    # CHECK constraint, so a value written by an import, another tool
    # or direct SQL reaches this point unvalidated. Reads fail closed.
    if "origin" in row.keys() and (row_origin := _normalize_message_origin(row["origin"])):
        msg["origin"] = row_origin
    if row["display_metadata"] and (decoded := db._decode_display_metadata(row["display_metadata"])) is not None:
        msg["display_metadata"] = decoded
    if include_summary_markers and row["_compressed_summary"]:
        msg["_compressed_summary"] = True
    msg.update(
        (col, row[col]) for col in ("timestamp", "tool_call_id", "tool_name", "effect_disposition") if row[col])
    if row["tool_calls"]:
        msg["tool_calls"] = _json_or(
            row["tool_calls"], [], "Failed to deserialize tool_calls in conversation replay, falling back to []")
    if row["platform_message_id"]:  # platform-side id exposed as ``message_id`` (JSONL transcript compat)
        msg["message_id"] = row["platform_message_id"]
    if row["observed"]:
        msg["observed"] = True
