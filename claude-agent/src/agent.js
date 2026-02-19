/**
 * Agent SDK wrapper.
 *
 * Wraps @anthropic-ai/claude-agent-sdk query() to run the agent loop,
 * extract streaming text deltas, and emit them as SSE events.
 *
 * Built-in Claude Code tools (Bash, Read, Write, etc.) are explicitly
 * disallowed -- only MCP tools are permitted.
 */

import { query } from "@anthropic-ai/claude-agent-sdk";

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
 * MCP servers are configured by the user in the HA integration
 * and passed per-request. The add-on does not auto-discover them.
 */
function buildMcpServers(userMcpServers) {
  return { ...userMcpServers };
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
  sendEvent,
}) {
  const mcpServers = buildMcpServers(userMcpServers);
  const allowedTools = buildAllowedTools(mcpServers);

  // Build environment -- API key auth passes the key via env
  const env = { ...process.env };
  if (authMode === "api_key" && apiKey) {
    env.ANTHROPIC_API_KEY = apiKey;
  }
  // For "max" auth mode, the CLI uses its own persisted credentials
  // in /data/.claude/ (symlinked to ~/.claude/)

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

  // Resume previous conversation if session ID provided
  if (sessionId) {
    options.resume = sessionId;
  }

  let currentSessionId = sessionId || null;
  let textStarted = false;

  for await (const message of query({ prompt: userText, options })) {
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

    // ── Streaming text deltas ──
    if (message.type === "stream_event" && message.event) {
      const event = message.event;

      if (event.type === "content_block_start") {
        const block = event.content_block;
        if (block && block.type === "text") {
          if (!textStarted) {
            sendEvent({ type: "role", role: "assistant" });
            textStarted = true;
          }
        }
      } else if (event.type === "content_block_delta") {
        const delta = event.delta;
        if (delta && delta.type === "text_delta") {
          if (!textStarted) {
            sendEvent({ type: "role", role: "assistant" });
            textStarted = true;
          }
          sendEvent({ type: "delta", content: delta.text });
        }
      }
      continue;
    }

    // ── Result message: send final status ──
    if (message.type === "result") {
      currentSessionId = message.session_id || currentSessionId;
      sendEvent({
        type: "result",
        session_id: currentSessionId,
        is_error: message.is_error || false,
      });
      continue;
    }
  }
}
