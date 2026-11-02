/**
 * TV tools for the TV in the room the user is talking from.
 *
 * An alfred-satellite started with --tv (a Fire TV Cube) offers its TV
 * control to Home Assistant as ESPHome actions, esphome.<node>_tv_*. The
 * integration names the satellite that heard the request; these tools call
 * its actions through Home Assistant's API with the add-on's SUPERVISOR_TOKEN.
 *
 * The Fire TV's own media_player volume and mute don't reach our soundbar (it
 * is on optical), so the instructions steer the model to these tools.
 * tv_status is how the model sees the TV: the satellite replies with JSON
 * (what's on screen as text, the app, what's playing), as screenshots are
 * black in DRM apps.
 */

import { createSdkMcpServer, tool } from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";

const HA_API = process.env.HA_API_URL || "http://supervisor/core/api";

const KEYS = [
  "up", "down", "left", "right", "select", "back", "home", "menu",
  "play", "pause", "play_pause", "rewind", "fast_forward",
];

const INSTRUCTIONS =
  "These tools control the TV in the room the user is talking from: a Fire TV " +
  "with a soundbar. Use them for its power, sound (volume, mute), playback " +
  "(pause, play), moving around on screen and searching. Its media_player " +
  "entities' power, volume and mute don't reach the TV and soundbar the way " +
  "these do, so don't use those for this TV. " +
  "The other tools act right away and say nothing about the screen; tv_status " +
  "tells you what's on it. Check it before moving around with tv_keys, and " +
  "after, to see where you ended up.";

/**
 * Calls esphome.<node>_<action> with `data`; a tool result either way. With
 * `response`, the result is the JSON the action replied with.
 */
async function callAction(node, action, data = {}, { response = false } = {}) {
  const service = `${node.replaceAll("-", "_")}_${action}`;
  try {
    const res = await fetch(`${HA_API}/services/esphome/${service}${response ? "?return_response" : ""}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${process.env.SUPERVISOR_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(data),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      const body = (await res.text()).slice(0, 200);
      return failed(`Home Assistant refused esphome.${service} (${res.status}): ${body}`);
    }
    if (response) {
      const { service_response: reply } = await res.json();
      return { content: [{ type: "text", text: JSON.stringify(reply) }] };
    }
    return { content: [{ type: "text", text: "Done." }] };
  } catch (err) {
    return failed(`Could not reach Home Assistant: ${err.message}`);
  }
}

function failed(text) {
  console.error(`[tv] ${text}`);
  return { content: [{ type: "text", text }], isError: true };
}

/**
 * The "tv" MCP server for satellite `node`. Build one per Claude process: an
 * in-process server instance serves one process at a time.
 */
export function tvServer(node) {
  return createSdkMcpServer({
    name: "tv",
    instructions: INSTRUCTIONS,
    alwaysLoad: true, // a handful of small tools; skip the tool-search round trip
    tools: [
      tool(
        "tv_keys",
        "Press TV remote buttons in order, e.g. \"down down right select\" to move " +
          "to a tile and open it, \"pause\", \"play\", \"back\" or \"home\".",
        { keys: z.string().describe(`1-20 space-separated buttons, each one of: ${KEYS.join(", ")}`) },
        ({ keys }) => callAction(node, "tv_keys", { keys }),
      ),
      tool(
        "tv_volume",
        "Turn the TV's sound (the soundbar) up or down by steps: about 2 for a " +
          "little, 3 for plain \"turn it up\", 8 for a lot. Negative is down.",
        { steps: z.number().int().min(-20).max(20).describe("Volume steps, -20 to 20, not 0") },
        ({ steps }) => callAction(node, "tv_volume", { steps }),
      ),
      tool(
        "tv_mute",
        "Mute or unmute the TV's sound. The soundbar only toggles, and nothing " +
          "reports whether it is muted now, so one call serves both.",
        {},
        () => callAction(node, "tv_mute"),
      ),
      tool(
        "tv_power",
        "Turn the TV on or off, the way Alexa does: on also switches the TV to " +
          "the Fire TV.",
        { on: z.boolean().describe("true to turn the TV on, false to turn it off") },
        ({ on }) => callAction(node, on ? "tv_turn_on" : "tv_turn_off"),
      ),
      tool(
        "tv_find_remote",
        "Make the TV remote beep so the user can find it.",
        {},
        () => callAction(node, "tv_find_remote"),
      ),
      tool(
        "tv_search",
        "Open the Fire TV's search results for a show, movie, actor or app. The " +
          "user's words come from speech recognition and may be misheard, so " +
          "search for the real title or name that sounds closest to what was " +
          "heard (\"the Richard Jones movie\" is likely Richard Jewell).",
        { query: z.string().min(1).describe("The title or name to search for, corrected if misheard") },
        ({ query }) => callAction(node, "tv_search", { query }),
      ),
      tool(
        "tv_launch",
        "Open an app on the TV by name: Netflix, Prime Video, YouTube, Disney+, " +
          "Max, Hulu, Peacock, Paramount+, Tubi, Spotify and others (tv_status " +
          "lists what's installed). Turn the TV on first if it's off.",
        { app: z.string().min(1).describe("The app's name, or its Android package") },
        ({ app }) => callAction(node, "tv_launch", { app }),
      ),
      tool(
        "tv_status",
        "What the TV is doing now, as JSON: screen (on, off, screensaver), " +
          "audio_output (tv, or cube speaker when the TV is off), the app in " +
          "front, media that's playing or paused, on_screen (focused: what the " +
          "remote's cursor is on; selected tabs; items you can move to, each with " +
          "its center on the 1920x1080 screen; other text) and installed apps. " +
          "Apps that draw their own UI, like Netflix, have no on-screen text. " +
          "Takes about 2 seconds.",
        {},
        () => callAction(node, "tv_status", {}, { response: true }),
      ),
    ],
  });
}
