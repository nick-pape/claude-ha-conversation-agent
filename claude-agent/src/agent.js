/**
 * Agent SDK wrapper.
 *
 * Runs each turn on a long-lived Agent SDK session (see sessions.js),
 * extracts streaming text deltas, and emits them as SSE events.
 *
 * Built-in Claude Code tools (Bash, Read, Write, etc.) are explicitly
 * disallowed -- only MCP tools are permitted.
 */

import { existsSync, readFileSync } from "node:fs";
import { loadMcpServers } from "./mcp-config.js";
import { SessionPool } from "./sessions.js";
import { tvServer } from "./tv.js";

const pool = new SessionPool();

/** The add-on's options from its Home Assistant configuration tab. */
function addonOptions() {
  try {
    return JSON.parse(readFileSync("/data/options.json", "utf-8"));
  } catch {
    return {};
  }
}

/** Home Assistant's DATE_TIME_PROMPT line, as rendered into the system prompt. */
const DATE_TIME_LINE = /Current time is \d{1,2}:\d{2}:\d{2}\. Today's date is \d{4}-\d{2}-\d{2}\.\n?/;

/**
 * Native Claude Code CLI installed by the Dockerfile (musl build for Alpine).
 * Passed explicitly so the SDK never falls back to a glibc binary it can't exec.
 */
const CLAUDE_EXECUTABLE = process.env.CLAUDE_CODE_EXECUTABLE || "/usr/local/bin/claude";

if (existsSync(CLAUDE_EXECUTABLE)) {
  console.log(`Claude Code executable: ${CLAUDE_EXECUTABLE}`);
} else {
  console.error(
    `Claude Code executable not found at ${CLAUDE_EXECUTABLE} -- ` +
      "the musl CLI package is missing from the image; rebuild the add-on. " +
      "Falling back to the SDK's own binary lookup."
  );
}

/** Built-in tools that must never be available to the agent. */
const DISALLOWED_BUILTIN_TOOLS = [
  "Bash",
  "Read",
  "Write",
  "Edit",
  "Glob",
  "Grep",
  "WebFetch",
  "WebSearch",
  "NotebookEdit",
  "Task",
  "TodoRead",
  "TodoWrite",
];

/**
 * Build the MCP server config dict for the Agent SDK.
 *
 * Merges persisted MCP servers (from /data/mcp_servers.json)
 * with any per-request overrides. Persisted servers are the
 * primary source; per-request servers can add or override.
 * With alwaysLoad, every server's tools are in the prompt from the start
 * instead of behind tool search.
 */
function buildMcpServers(userMcpServers, alwaysLoad) {
  const persisted = loadMcpServers();
  const merged = { ...persisted, ...userMcpServers };
  for (const [name, server] of Object.entries(merged)) {
    merged[name] = {
      ...server,
      ...(server.headers && { headers: expandEnv(server.headers) }),
      ...(alwaysLoad && { alwaysLoad: true }),
    };
  }
  return merged;
}

/**
 * Replace ${NAME} in header values with environment variables, e.g.
 * "Bearer ${SUPERVISOR_TOKEN}" for HA's own MCP server via the Supervisor.
 */
function expandEnv(headers) {
  return Object.fromEntries(
    Object.entries(headers).map(([k, v]) => [
      k,
      String(v).replace(/\$\{(\w+)\}/g, (_, n) => process.env[n] ?? ""),
    ]),
  );
}

/**
 * Build the list of allowed tools (only MCP tools).
 */
function buildAllowedTools(mcpServers) {
  return Object.keys(mcpServers).map((name) => `mcp__${name}__*`);
}

/**
 * Run the agent loop and emit SSE events via sendEvent callback.
 *
 * @param {object} params
 * @param {string} params.systemPrompt
 * @param {string} params.userText
 * @param {string} [params.model]
 * @param {string} [params.sessionId]
 * @param {object} [params.mcpServers]
 * @param {number} [params.maxTurns]
 * @param {string} params.authMode  - "api_key" or "max"
 * @param {string} [params.apiKey]  - Required when authMode is "api_key"
 * @param {object} [params.satellite] - The alfred-satellite that heard the
 *   request: {node, tv}; with tv, the agent gets the TV tools (tv.js) for it
 * @param {function} params.sendEvent - Callback to emit SSE data
 */
export async function runAgent({
  systemPrompt,
  userText,
  model,
  sessionId,
  mcpServers: userMcpServers,
  maxTurns,
  authMode,
  apiKey,
  satellite,
  sendEvent,
}) {
  // Tool search off: no lookup step mid-turn before a tool call, which costs
  // voice latency and once had the model write its call out as text.
  const toolSearch = addonOptions().tool_search || "auto";
  const mcpServers = buildMcpServers(userMcpServers, toolSearch === "disabled");
  const tvNode = satellite?.tv ? satellite.node : null;
  const allowedTools = buildAllowedTools(tvNode ? { ...mcpServers, tv: true } : mcpServers);

  // Build environment -- API key auth passes the key via env
  const env = { ...process.env };
  if (authMode === "api_key" && apiKey) {
    env.ANTHROPIC_API_KEY = apiKey;
  }
  // For "max" auth mode, the CLI uses its own persisted credentials
  // in /data/.claude/ (symlinked to ~/.claude/)

  // Home Assistant puts the current time (to the second) in the system prompt,
  // which would make every turn's options differ. Move it into the user's
  // message so sessions and the prompt cache can be reused.
  const dateTime = systemPrompt?.match(DATE_TIME_LINE);
  if (dateTime) systemPrompt = systemPrompt.replace(DATE_TIME_LINE, "");
  const turnText = dateTime ? `[${dateTime[0].trim()}]\n${userText}` : userText;

  const options = {
    systemPrompt,
    model,
    maxTurns,
    mcpServers,
    allowedTools,
    disallowedTools: DISALLOWED_BUILTIN_TOOLS,
    permissionMode: "dontAsk",
    includePartialMessages: true,
    env,
    stderr: (data) => console.error("[claude-cli]", data.trimEnd()),
  };

  if (existsSync(CLAUDE_EXECUTABLE)) {
    options.pathToClaudeCodeExecutable = CLAUDE_EXECUTABLE;
  }

  // Thinking off keeps voice latency steady: no thinking pass before the
  // first word, and models then narrate a short sentence before tool calls.
  const thinking = addonOptions().thinking || "adaptive";
  if (thinking === "disabled") options.thinking = { type: "disabled" };

  // Options are fixed when a session starts, so a pre-warmed spare can only
  // serve requests with the same options; the key is everything but secrets
  // and callbacks.
  const key = JSON.stringify({ systemPrompt, model, maxTurns, mcpServers, tvNode, authMode, apiKey: Boolean(apiKey), thinking });
  // Each Claude process gets its own TV server instance.
  const makeOptions = () => (tvNode ? { ...options, mcpServers: { ...mcpServers, tv: tvServer(tvNode) } } : options);
  const started = Date.now();
  const { session, how } = await pool.acquire({ sessionId, key, makeOptions });

  let currentSessionId = sessionId || null;
  let textStarted = false;
  let firstText = null;
  let lastChar = ""; // last character sent, to space text blocks apart
  let newBlock = false;
  let textBlockOpen = false;

  for await (const message of session.turn(turnText)) {
    // ── Init message: capture session ID and MCP status ──
    if (message.type === "system" && message.subtype === "init") {
      currentSessionId = message.session_id;
      sendEvent({
        type: "init",
        session_id: currentSessionId,
        mcp_servers: message.mcp_servers || [],
      });
      continue;
    }

    // ── New assistant turn (between tool rounds) ──
    if (message.type === "assistant") {
      // Reset so the next text block emits a fresh role event
      textStarted = false;
      continue;
    }

    // ── Streaming content blocks ──
    if (message.type === "stream_event" && message.event) {
      const event = message.event;

      if (event.type === "content_block_start") {
        const block = event.content_block;
        if (block && block.type === "text") {
          newBlock = true;
          textBlockOpen = true;
          if (!textStarted) {
            sendEvent({ type: "role", role: "assistant" });
            textStarted = true;
          }
        } else if (block && block.type === "tool_use") {
          // Emit tool start so the client knows something is happening
          sendEvent({
            type: "tool_start",
            tool: block.name || "unknown",
            tool_id: block.id || null,
          });
        }
      } else if (event.type === "content_block_delta") {
        const delta = event.delta;
        if (delta && delta.type === "text_delta") {
          if (!textStarted) {
            sendEvent({ type: "role", role: "assistant" });
            textStarted = true;
          }
          firstText ??= Date.now() - started;
          let text = delta.text;
          // Text blocks either side of a tool call ("Let me check." / "It's
          // on.") arrive with nothing between them; space them apart so they
          // read, and split into sentences, correctly.
          if (newBlock && lastChar && !/\s/.test(lastChar) && !/^\s/.test(text)) text = ` ${text}`;
          newBlock = false;
          if (text) lastChar = text.at(-1);
          sendEvent({ type: "delta", content: text });
        }
      } else if (event.type === "content_block_stop" && textBlockOpen) {
        // End the block with a space right away: a sentence splitter only
        // knows "Let me check on that." is complete once whitespace follows,
        // and the next text may not come until a tool call has finished.
        textBlockOpen = false;
        if (lastChar && !/\s/.test(lastChar)) {
          lastChar = " ";
          sendEvent({ type: "delta", content: " " });
        }
      }
      continue;
    }

    // ── Tool result (tool finished executing) ──
    // The SDK delivers tool results as user messages with tool_result blocks.
    if (message.type === "user" && Array.isArray(message.message?.content)) {
      for (const block of message.message.content) {
        if (block?.type === "tool_result") {
          sendEvent({ type: "tool_done", tool_id: block.tool_use_id || null });
        }
      }
      continue;
    }

    // ── Result message: send final status ──
    if (message.type === "result") {
      currentSessionId = message.session_id || currentSessionId;
      console.log(
        `Turn on ${how} session ${currentSessionId}: first text ${firstText ?? "-"} ms, ` +
          `done ${Date.now() - started} ms`,
      );
      sendEvent({
        type: "result",
        session_id: currentSessionId,
        is_error: message.is_error || false,
      });
      continue;
    }
  }
}
