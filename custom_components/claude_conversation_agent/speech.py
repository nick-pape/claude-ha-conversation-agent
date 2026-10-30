"""Keep agent replies speakable: strip markdown and cap the spoken length.

Replies are streamed into Home Assistant's chat log and from there straight
into TTS. A markdown table or a long answer reads badly and can take longer to
synthesize than to play. This sits between the agent's text deltas and the
chat log, emitting whole sentences until a character budget is spent; the rest
of the stream is still consumed (so session state is kept) but not spoken.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

# A sentence ends at . ! ? (optionally followed by a closing quote/bracket)
# and whitespace, or at a newline.
_BOUNDARY = re.compile(r"[.!?][\"'”’)\]]*\s+|\n+")
_TABLE_RULE = re.compile(r"^[\s|:\-]+$")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+")
_DECORATION = re.compile(r"[*#`_~>]+")
_SPACES = re.compile(r"\s+")


def clean_markdown(text: str) -> str:
    """Turn one sentence or line of markdown into plain speakable text."""
    if _TABLE_RULE.match(text):
        return ""
    text = _LIST_MARKER.sub("", text)
    if "|" in text:
        cells = [c.strip() for c in text.strip().strip("|").split("|")]
        text = ", ".join(c for c in cells if c)
    text = _DECORATION.sub("", text)
    return _SPACES.sub(" ", text).strip()


async def limit_spoken(
    stream: AsyncIterable[dict[str, Any]], max_chars: int
) -> AsyncIterator[dict[str, Any]]:
    """Pass through chat deltas, cleaning markdown and capping spoken length.

    `max_chars` <= 0 disables the cap (markdown is still cleaned).
    """
    buf = ""
    spoken = 0
    done = False

    def take(segment: str) -> dict[str, Any] | None:
        nonlocal spoken, done
        text = clean_markdown(segment)
        if not text or done:
            return None
        piece = (" " if spoken else "") + text
        spoken += len(piece)
        if max_chars > 0 and spoken >= max_chars:
            done = True
        return {"content": piece}

    async for delta in stream:
        if "content" not in delta:
            yield delta  # role markers etc.
            continue
        buf += delta["content"] or ""
        while (m := _BOUNDARY.search(buf)) is not None:
            segment, buf = buf[: m.end()], buf[m.end():]
            if (out := take(segment)) is not None:
                yield out
    if buf and (out := take(buf)) is not None:
        yield out
