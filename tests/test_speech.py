"""Tests for speech.py: markdown cleanup and the spoken-length cap."""

import pytest

from custom_components.claude_conversation_agent.speech import (
    clean_markdown,
    limit_spoken,
)


async def _deltas(text, size=7):
    yield {"role": "assistant"}
    for i in range(0, len(text), size):
        yield {"content": text[i : i + size]}


async def _spoken(text, max_chars):
    out = [d async for d in limit_spoken(_deltas(text), max_chars)]
    assert out[0] == {"role": "assistant"}
    return "".join(d.get("content", "") for d in out[1:])


class TestCleanMarkdown:
    def test_table_rule_is_dropped(self):
        assert clean_markdown("|---|:---:|---|") == ""

    def test_table_row_becomes_commas(self):
        assert clean_markdown("| Dining | Dining Room | off |") == "Dining, Dining Room, off"

    def test_list_markers_and_emphasis_removed(self):
        assert clean_markdown("- **Kitchen** is on") == "Kitchen is on"
        assert clean_markdown("2. `light.dining`") == "light.dining"

    def test_plain_text_untouched(self):
        assert clean_markdown("It's 11:16, Master Pape.") == "It's 11:16, Master Pape."


class TestLimitSpoken:
    @pytest.mark.asyncio
    async def test_markdown_table_reply_is_speakable(self):
        reply = (
            "Very good.\n\n| Light | Area | State |\n|---|---|---|\n"
            "| Dining | Dining Room | off |\n\nShall I turn on the **Dining** light?"
        )
        spoken = await _spoken(reply, 350)
        assert "|" not in spoken and "**" not in spoken and "---" not in spoken
        assert spoken.endswith("Shall I turn on the Dining light?")

    @pytest.mark.asyncio
    async def test_stops_on_sentence_boundary_after_budget(self):
        spoken = await _spoken("One here. Two here. Three here. Four here.", 15)
        assert spoken == "One here. Two here."

    @pytest.mark.asyncio
    async def test_zero_disables_cap(self):
        text = "Sentence. " * 100
        spoken = await _spoken(text, 0)
        assert spoken.count("Sentence.") == 100

    @pytest.mark.asyncio
    async def test_short_reply_passes_through(self):
        assert await _spoken("It's quarter past eleven.", 350) == "It's quarter past eleven."
