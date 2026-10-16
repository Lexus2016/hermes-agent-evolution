"""Image wire conversion for auxiliary Anthropic-compatible requests."""
from __future__ import annotations

_ANTHROPIC_MEDIA_BLOCKS = {"image_url": ("image", "image/png"), "video_url": ("video", "video/mp4")}

def _convert_openai_images_to_anthropic(messages: list) -> list:
    """Convert OpenAI ``image_url``/``video_url`` blocks to Anthropic ``image``/``video``;
    only list-content messages with such blocks change."""
    converted = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            converted.append(msg)
            continue
        new_content = []
        changed = False
        for block in content:
            block_type = block.get("type")
            if block_type not in _ANTHROPIC_MEDIA_BLOCKS:
                new_content.append(block)
                continue
            url = (block.get(block_type) or {}).get("url", "")
            anth_type, media_type = _ANTHROPIC_MEDIA_BLOCKS[block_type]
            if url.startswith("data:"):
                header, _, b64data = url.partition(",")
                if ":" in header and ";" in header:
                    media_type = header.split(":", 1)[1].split(";", 1)[0]
                source = {"type": "base64", "media_type": media_type, "data": b64data}
            else:
                source = {"type": "url", "url": url}
            new_content.append({"type": anth_type, "source": source})
            changed = True
        converted.append({**msg, "content": new_content} if changed else msg)
    return converted
