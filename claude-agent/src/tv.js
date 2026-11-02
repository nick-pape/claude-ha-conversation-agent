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
 * tv_current_status is how the model sees the TV: the satellite replies with
 * JSON (what's on screen as text, the app, what's playing), as screenshots are
 * black in DRM apps. tv_system_info is what the TV is (its apps, equipment and
 * what each can do), read from the Cube itself; it's fetched once per Claude
 * process and summed up in the server's instructions (describeTv).
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
  "The other tools act right away and say nothing about the screen; " +
  "tv_current_status tells you what's on it. Check it before moving around " +
  "with tv_keys, and after, to see where you ended up.";

/** The system info, per satellite, refetched after this long. */
const SYSTEM_INFO_TTL_MS = 10 * 60 * 1000;
const systemInfoCache = new Map(); // node -> { at, info }

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

/** The satellite's tv_system_info, cached; null if it can't be had. */
export async function tvSystemInfo(node) {
  const hit = systemInfoCache.get(node);
  if (hit && Date.now() - hit.at < SYSTEM_INFO_TTL_MS) return hit.info;
  const result = await callAction(node, "tv_system_info", {}, { response: true });
  const info = result.isError ? null : JSON.parse(result.content[0].text);
  if (info) systemInfoCache.set(node, { at: Date.now(), info });
  return info;
}

/** A short description of the TV from its system info, for the instructions. */
export function describeTv(info) {
  if (!info) return "";
  const has = (d, f) => (d.functions || []).includes(f);
  const lines = (info.equipment || []).map((d) => {
    const can = [];
    if (has(d, "POWER_ON") && has(d, "POWER_OFF")) can.push("power on/off");
    else if (has(d, "POWER_TOGGLE")) can.push("power toggles");
    if (has(d, "VOLUME_UP")) can.push("volume");
    if (has(d, "MUTE_ON") || has(d, "MUTE_OFF")) can.push("mute on/off");
    else if (has(d, "MUTE_TOGGLE")) can.push("mute only toggles");
    if (has(d, "LOCATE_DEVICE")) can.push("can ring");
    const name = [d.brand, d.name].filter((x, i, a) => x && a.indexOf(x) === i).join(" ");
    return `${d.type.toLowerCase()}: ${name || "unnamed"} (${[...(d.control || []), ...(d.cec ? ["cec"] : [])].join(", ")}; ${can.join(", ") || "no controls"})`;
  });
  const volume = info.fire_tv?.volume_device;
  const apps = (info.apps || []).map((a) => a.name);
  return (
    ` This TV, as set up on its Fire TV (${info.cube?.model}, ${info.cube?.fire_os}): ${lines.join("; ")}.` +
    (volume ? ` Volume and mute go to the ${volume.toLowerCase()}.` : "") +
    (apps.length ? ` Installed apps: ${apps.join(", ")}.` : "")
  );
}

function failed(text) {
  console.error(`[tv] ${text}`);
  return { content: [{ type: "text", text }], isError: true };
}

/**
 * The "tv" MCP server for satellite `node`, described with its system info
 * (tvSystemInfo). Build one per Claude process: an in-process server instance
 * serves one process at a time.
 */
export function tvServer(node, info = null) {
  return createSdkMcpServer({
    name: "tv",
    instructions: INSTRUCTIONS + describeTv(info),
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
        "Open an installed app on the TV by name (the instructions list them). " +
          "Turn the TV on first if it's off.",
        { app: z.string().min(1).describe("The app's name, or its Android package") },
        ({ app }) => callAction(node, "tv_launch", { app }),
      ),
      tool(
        "tv_current_status",
        "What the TV is doing now, as JSON: screen (on, off, screensaver), " +
          "audio_output (tv, or cube speaker when the TV is off), the app in " +
          "front, media that's playing or paused, and on_screen (focused: what " +
          "the remote's cursor is on; selected tabs; items you can move to, each " +
          "with its center on the 1920x1080 screen; other text). Apps that draw " +
          "their own UI, like Netflix, have no on-screen text. About 2 seconds.",
        {},
        () => callAction(node, "tv_current_status", {}, { response: true }),
      ),
      tool(
        "tv_system_info",
        "What the TV is, as JSON, read from it: the Fire TV, the installed apps " +
          "(with what requests each takes), the equipment (TV, soundbar, remote) " +
          "with how it's controlled and its functions, and the controls offered. " +
          "The instructions already sum this up; use it for the details.",
        {},
        () => callAction(node, "tv_system_info", {}, { response: true }),
      ),
    ],
  });
}
