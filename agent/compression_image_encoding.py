"""Image encoding and shrinking for compaction recovery."""
from __future__ import annotations
import contextlib
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional
logger = logging.getLogger("agent.conversation_compression")

_IMAGE_SHRINK_TARGET_BYTES = 4 * 1024 * 1024

_IMAGE_SUFFIX_BY_MIME = {
    "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/bmp": ".bmp",
}

def _shrink_data_url(url: str, *, max_dimension: int, resize_fn: Any) -> tuple:
    """Return ``(resized_url, unshrinkable)`` for a data URL.
    ``resized_url`` is None when no rewrite applied. ``unshrinkable`` is True only when the image violated a
    constraint and resizing failed to satisfy that same constraint, so the caller knows a retry is pointless.
    The accept gate MUST use the axis that triggered the shrink: a pixel downscale can re-encode to MORE bytes
    (PNG non-monotonic); a byte-only reject wedges."""
    target_bytes = _IMAGE_SHRINK_TARGET_BYTES
    if not isinstance(url, str) or not url.startswith("data:"):
        return None, False
    triggered_by = "bytes" if len(url) > target_bytes else None  # over byte budget
    if triggered_by is None:
        # Bytes fine; check pixels against the provider cap (tiny bytes, huge pixels).
        dims = _decode_pixels(url)
        if dims is None or max(dims) <= max_dimension:
            return None, False
        triggered_by = "dimension"
    try:
        header, _, data = url.partition(",")
        mime = _data_url_mime(header)
        import base64 as _b64
        raw = _b64.b64decode(data)
        tmp = tempfile.NamedTemporaryFile(
            prefix="hermes_shrink_", suffix=_IMAGE_SUFFIX_BY_MIME.get(mime, ".jpg"), delete=False
        )
        try:
            tmp.write(raw)
            tmp.close()
            resized = resize_fn(
                Path(tmp.name), mime_type=mime, max_base64_bytes=target_bytes, max_dimension=max_dimension
            )
        finally:
            with contextlib.suppress(Exception):
                Path(tmp.name).unlink(missing_ok=True)
        if not resized:
            return None, True  # Pillow couldn't help
        new_dims = _decode_pixels(resized)
        if triggered_by == "bytes":
            # Byte budget is binding — bytes must shrink; and the resizer may return an
            # over-cap blob (long side freezes at the 64px short-side floor) → still 400.
            ok = len(resized) < len(url) and (new_dims is None or max(new_dims) <= max_dimension)
        elif new_dims is not None:
            # Dimension cap is binding: accept a byte-larger re-encode if now within cap.
            ok = max(new_dims) <= max_dimension
        else:
            # Can't verify dimensions: fall back to the bytes-must-shrink gate so we never
            # accept an unverifiable byte-larger blob.
            ok = len(resized) < len(url)
        return (resized, False) if ok else (None, True)
    except Exception as exc:
        logger.warning("image-shrink recovery: re-encode failed — %s", exc)
        return None, triggered_by is not None


def _data_url_mime(header: str, default: str = "image/jpeg") -> str:
    """``image/*`` mime from a ``data:`` URL header, else ``default``."""
    if header.startswith("data:"):
        candidate = header[len("data:") :].split(";", 1)[0].strip()
        if candidate.startswith("image/"):
            return candidate
    return default

def _decode_pixels(data_url: str) -> Optional[tuple]:
    """``(width, height)`` of a base64 data URL; None when Pillow is missing or the payload is corrupt."""
    try:
        import base64, io
        _, _, data_d = data_url.partition(",")
        if not data_d or not data_url.startswith("data:"):
            return None
        from PIL import Image
        with Image.open(io.BytesIO(base64.b64decode(data_d))) as _img:
            return _img.size
    except Exception:
        return None

def _source_to_data_url(source: Any) -> Optional[str]:
    """Anthropic ``{"type": "base64", ...}`` image source → data URL, else None."""
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    data = source.get("data")
    if not isinstance(data, str) or not data:
        return None
    media_type = str(source.get("media_type") or "image/jpeg").strip()
    return f"data:{media_type if media_type.startswith('image/') else 'image/jpeg'};base64,{data}"

def _write_data_url_to_source(source: dict, data_url: str) -> dict:
    """Return a NEW source dict carrying the re-encoded payload.
    Copy-on-write: parts may be shared with the persistent history, so mutating in place would store the
    degraded image; the caller replaces the part."""
    header, _, data = data_url.partition(",")
    return {**source, "type": "base64", "media_type": _data_url_mime(header), "data": data}
