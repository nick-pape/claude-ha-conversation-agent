/**
 * Express HTTP server for the Claude Agent add-on.
 *
 * Endpoints:
 *   POST /api/chat       – SSE stream of agent responses
 *   GET  /api/health      – Health check
 *   GET  /api/auth/status – Current auth state
 *   POST /api/auth/login  – Initiate Max subscription login
 *   GET  /                – Ingress web UI
 */

import express from "express";
import { createReadStream } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { runAgent } from "./agent.js";
import { getAuthStatus, initiateLogin } from "./auth.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

const app = express();
app.use(express.json({ limit: "1mb" }));

// ── Health check ──────────────────────────────────────────────────────
app.get("/api/health", (_req, res) => {
  res.json({ status: "ok" });
});

// ── Auth status ───────────────────────────────────────────────────────
app.get("/api/auth/status", async (_req, res) => {
  try {
    const status = await getAuthStatus();
    res.json(status);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Max subscription login ────────────────────────────────────────────
app.post("/api/auth/login", async (_req, res) => {
  try {
    const result = await initiateLogin();
    res.json(result);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Chat (SSE stream) ─────────────────────────────────────────────────
app.post("/api/chat", async (req, res) => {
  const {
    system_prompt,
    user_text,
    model,
    session_id,
    mcp_servers,
    max_turns,
    auth_mode,
    api_key,
  } = req.body;

  if (!user_text) {
    return res.status(400).json({ error: "user_text is required" });
  }

  // Set up SSE headers
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });

  const sendEvent = (data) => {
    res.write(`data: ${JSON.stringify(data)}\n\n`);
  };

  try {
    await runAgent({
      systemPrompt: system_prompt,
      userText: user_text,
      model,
      sessionId: session_id,
      mcpServers: mcp_servers || {},
      maxTurns: max_turns || 10,
      authMode: auth_mode || "api_key",
      apiKey: api_key,
      sendEvent,
    });
  } catch (err) {
    sendEvent({
      type: "result",
      session_id: session_id || null,
      is_error: true,
      error: err.message,
    });
  }

  res.end();
});

// ── Ingress web UI ────────────────────────────────────────────────────
app.get("/", (_req, res) => {
  res.sendFile(join(__dirname, "ui", "index.html"));
});

// ── Start server ──────────────────────────────────────────────────────
const PORT = process.env.PORT || 3000;
app.listen(PORT, "0.0.0.0", () => {
  console.log(`Claude Agent add-on listening on port ${PORT}`);
});
