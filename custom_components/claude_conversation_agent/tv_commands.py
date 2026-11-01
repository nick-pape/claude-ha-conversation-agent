"""Obvious TV commands, matched without the agent.

These need no judgment: a short, exact phrase maps straight to one of the
satellite's TV actions (alfred-satellite --tv offers them to Home Assistant as
esphome.<node>_tv_*). Anything that doesn't match exactly goes to Claude, so
"play Bluey on Disney+" or "open the second one" still get the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass(frozen=True)
class TvCommand:
    """One satellite TV action and its arguments."""

    action: str
    data: dict = field(default_factory=dict)


# Volume steps for "a little", plain, and "a lot".
STEPS_LITTLE = 2
STEPS_DEFAULT = 3
STEPS_LOT = 8

_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "once": 1, "twice": 2,
}
_KEYS = {
    "up": "up", "down": "down", "left": "left", "right": "right",
    "select": "select", "ok": "select", "okay": "select", "enter": "select",
    "back": "back", "home": "home",
}
_FILLER = {"go", "press", "move", "then", "and", "the", "button", "times", "time", "click", "hit"}
_OBJECT = r"(?: (?:it|this|the (?:tv|show|movie|video)))?"

_LEADING = re.compile(
    r"^(?:(?:hey|yo|ok|okay)\s+)?(?:snoop(?:\s+dogg?)?\s+)?(?:(?:please|can you|could you)\s+)?"
)
_TRAILING = re.compile(r"\s+(?:please|thanks|thank you)$")


def normalize(text: str) -> str:
    """Lower case, punctuation stripped, with polite or addressing words trimmed."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    t = " ".join(t.split())
    t = _LEADING.sub("", t)
    return _TRAILING.sub("", t).strip()


def match(text: str) -> TvCommand | None:
    """The TV action for an obvious command, or None to let the agent decide."""
    t = normalize(text)
    if not t:
        return None
    if re.fullmatch(r"(?:un)?mute(?: (?:it|the (?:tv|sound|soundbar|volume)))?", t):
        return TvCommand("tv_mute")  # our soundbar only toggles
    if (steps := _volume(t)) is not None:
        return TvCommand("tv_volume", {"steps": steps})
    if re.fullmatch("pause" + _OBJECT, t):
        return TvCommand("tv_keys", {"keys": "pause"})
    if re.fullmatch("(?:play|unpause|resume|continue)" + _OBJECT, t):
        return TvCommand("tv_keys", {"keys": "play"})
    if re.fullmatch(r"(?:find|ring|where's|where is) (?:my |the )?remote", t):
        return TvCommand("tv_find_remote")
    if m := re.fullmatch(r"search (?:for )?(.+)", t):
        return TvCommand("tv_search", {"query": m.group(1)})
    if keys := _keys(t):
        return TvCommand("tv_keys", {"keys": " ".join(keys)})
    return None


def _volume(t: str) -> int | None:
    m = (
        re.fullmatch(r"(?:turn (?:it|the (?:tv|volume|sound)) |volume )(up|down)(?: (.+))?", t)
        or re.fullmatch(r"turn (up|down) (?:the )?(?:volume|sound|tv)(?: (.+))?", t)
        or re.fullmatch(r"(louder|quieter|softer)(?: (.+))?", t)
    )
    if not m:
        return None
    sign = 1 if m.group(1) in ("up", "louder") else -1
    amount = m.group(2)
    if amount is None:
        steps = STEPS_DEFAULT
    elif re.fullmatch(r"a (?:little|bit|tad)(?: bit)?|slightly", amount):
        steps = STEPS_LITTLE
    elif re.fullmatch(r"a lot|way (?:up|down)", amount):
        steps = STEPS_LOT
    elif n := re.fullmatch(r"(\w+)(?: (?:steps|notches|clicks|times))?", amount):
        word = n.group(1)
        if word.isdigit():
            steps = int(word)
        elif word in _NUMBERS:
            steps = _NUMBERS[word]
        else:
            return None
    else:
        return None  # not an obvious amount: let the agent decide
    return sign * min(steps, 20)


def _keys(t: str) -> list[str] | None:
    """A run of remote keys, like "down down right select" or "go down three times"."""
    words = t.split()
    keys: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word in _FILLER:
            continue
        if word not in _KEYS:
            return None
        count = 1
        if i < len(words) and (words[i].isdigit() or words[i] in _NUMBERS):
            count = int(words[i]) if words[i].isdigit() else _NUMBERS[words[i]]
            i += 1
        keys += [_KEYS[word]] * count
    if not keys or len(keys) > 20:
        return None
    return keys
