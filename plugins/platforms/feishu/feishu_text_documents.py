"""Bounded decoding for cached Feishu text attachments."""
from pathlib import Path


def read_bounded_text(path: str, max_bytes: int) -> str | None:
    """Return universal-newline UTF-8 text, or None if the actual bytes exceed the cap."""
    with Path(path).open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return None
    return raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
