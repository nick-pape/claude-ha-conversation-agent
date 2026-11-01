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
  "with a soundbar. Use them for its sound (volume, mute), playback (pause, " +
  "play), moving around on screen and searching. Its media_player entities' " +
  "volume and mute don't reach the soundbar, so don't use those for this TV. " +
  "Each tool acts right away and says nothing back about the screen.";

/** Calls esphome.<node>_<action> with `data`; a tool result either way. */
async function callAction(node, action, data = {}) {
  const service = `${node.replaceAll("-", "_")}_${action}`;
  try {
    const res = await fetch(`${HA_API}/services/esphome/${service}`, {
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
        "tv_find_remote",
        "Make the TV remote beep so the user can find it.",
        {},
        () => callAction(node, "tv_find_remote"),
      ),
      tool(
        "tv_search",
        "Open the Fire TV's search results for a show, movie, actor or app.",
        { query: z.string().min(1).describe("What to search for") },
        ({ query }) => callAction(node, "tv_search", { query }),
      ),
    ],
  });
}
