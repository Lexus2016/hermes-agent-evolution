"""Session list-row presentation, rebound onto the shared server runtime."""
from .method_ctx import bind_module

def _session_row_summary(row: dict, *, tip_row: dict | None = None, resolved_id=None, db=None) -> dict:
    """Compact session.list row; ``tip_row``/``resolved_id`` come from the compression tip.
    ``db`` adds ``live_message_count`` (see ``_live_count_field``)."""
    tip_row = tip_row or row
    return {"id": row["id"], **({} if resolved_id is None else {"resolved_id": resolved_id}),
            "title": row.get("title") or "", "preview": tip_row.get("preview") or "",
            "started_at": row.get("started_at") or 0, "message_count": tip_row.get("message_count") or 0,
            "source": row.get("source") or "",
            # Durable lineage root for compressed conversations — the same field REST
            # projects (api_server._session_response); RPC consumers (desktop pinning /
            # lineage dedup) group on it. #66663.
            "_lineage_root_id": row.get("_lineage_root_id"),
            **({} if db is None else _live_count_field(db, row["id"] if resolved_id is None else resolved_id))}


def register(server):
    bind_module(globals(), server)
