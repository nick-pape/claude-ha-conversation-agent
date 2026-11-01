"""Tests for tv_commands.py – obvious TV commands that skip the agent."""

from __future__ import annotations

import pytest

from custom_components.claude_conversation_agent.tv_commands import TvCommand, match, normalize


class TestNormalize:
    def test_addressing_and_polite_words_are_trimmed(self):
        assert normalize("Hey Snoop, turn it up please!") == "turn it up"
        assert normalize("Snoop Dogg, can you pause?") == "pause"
        assert normalize("  Mute.  ") == "mute"


class TestMatch:
    @pytest.mark.parametrize("text", ["Mute", "unmute", "mute the TV", "Snoop, unmute the sound."])
    def test_mute_toggles(self, text):
        assert match(text) == TvCommand("tv_mute")

    @pytest.mark.parametrize(
        ("text", "steps"),
        [
            ("Volume up", 3),
            ("Turn it down.", -3),
            ("turn up the volume", 3),
            ("Turn the volume down a little", -2),
            ("turn it up a bit", 2),
            ("Louder", 3),
            ("quieter a lot", -8),
            ("volume up 5", 5),
            ("turn it down four notches", -4),
            ("volume up 50", 20),
        ],
    )
    def test_volume(self, text, steps):
        assert match(text) == TvCommand("tv_volume", {"steps": steps})

    @pytest.mark.parametrize("text", ["Pause", "pause it", "pause the movie"])
    def test_pause(self, text):
        assert match(text) == TvCommand("tv_keys", {"keys": "pause"})

    @pytest.mark.parametrize("text", ["Play", "unpause", "Resume the show", "continue"])
    def test_play(self, text):
        assert match(text) == TvCommand("tv_keys", {"keys": "play"})

    @pytest.mark.parametrize("text", ["Find my remote", "where's the remote?", "ring remote"])
    def test_find_remote(self, text):
        assert match(text) == TvCommand("tv_find_remote")

    def test_search(self):
        assert match("Search for Bluey") == TvCommand("tv_search", {"query": "bluey"})
        assert match("search the office") == TvCommand("tv_search", {"query": "the office"})

    @pytest.mark.parametrize(
        ("text", "keys"),
        [
            ("Down", "down"),
            ("select", "select"),
            ("OK", "select"),
            ("go home", "home"),
            ("back", "back"),
            ("down down right, select", "down down right select"),
            ("go down three times then select", "down down down select"),
            ("right 2", "right right"),
            ("press the back button", "back"),
        ],
    )
    def test_remote_keys(self, text, keys):
        assert match(text) == TvCommand("tv_keys", {"keys": keys})

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Play Bluey on Disney+",
            "play some music",
            "turn the volume up so I can hear the dialogue",
            "open the second one",
            "go down to the bottom",
            "what's on TV",
            "turn off the lights",
            "down " * 21,
        ],
    )
    def test_anything_else_goes_to_the_agent(self, text):
        assert match(text) is None
