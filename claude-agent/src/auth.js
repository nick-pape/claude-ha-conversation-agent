/**
 * Authentication management for the Claude Agent add-on.
 *
 * Supports two modes:
 *   - api_key: Key passed per-request from the HA integration (no state here)
 *   - max: Claude Max subscription, authenticated via CLI login flow.
 *          Credentials persist in /data/.claude/ (symlinked to ~/.claude/).
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const CLAUDE_CONFIG_DIR = "/data/.claude";
const CREDENTIALS_FILE = join(CLAUDE_CONFIG_DIR, "credentials.json");

/**
 * Find the Claude CLI binary bundled inside @anthropic-ai/claude-agent-sdk.
 */
function findClaudeCli() {
  try {
    const require = createRequire(import.meta.url);
    const cliPath = require.resolve("@anthropic-ai/claude-agent-sdk/cli.js");
    return cliPath;
  } catch {
    return null;
  }
}

/**
 * Get the current authentication status.
 *
 * @returns {{ api_key_available: boolean, max_authenticated: boolean, max_email: string|null }}
 */
export async function getAuthStatus() {
  const apiKeyAvailable = !!process.env.ANTHROPIC_API_KEY;

  let maxAuthenticated = false;
  let maxEmail = null;

  if (existsSync(CREDENTIALS_FILE)) {
    try {
      const raw = readFileSync(CREDENTIALS_FILE, "utf-8");
      const creds = JSON.parse(raw);
      if (creds.oauthAccount?.emailAddress) {
        maxAuthenticated = true;
        maxEmail = creds.oauthAccount.emailAddress;
      }
    } catch {
      // Credentials file exists but is invalid
    }
  }

  return {
    api_key_available: apiKeyAvailable,
    max_authenticated: maxAuthenticated,
    max_email: maxEmail,
  };
}

// Keep a reference to any in-progress login process so it can finish
// receiving the OAuth callback after we return the URL to the user.
let loginProcess = null;

/**
 * Initiate the Max subscription login flow.
 *
 * Spawns `claude auth login`, captures the OAuth URL from its output,
 * and returns it immediately. The process keeps running in the background
 * to receive the OAuth callback and persist credentials.
 *
 * @returns {{ login_url: string|null, message: string }}
 */
export async function initiateLogin() {
  const cliPath = findClaudeCli();
  if (!cliPath) {
    return {
      login_url: null,
      message: "Login failed: Claude CLI not found in node_modules.",
    };
  }

  // Kill any previous login process
  if (loginProcess) {
    try { loginProcess.kill(); } catch {}
    loginProcess = null;
  }

  return new Promise((resolve) => {
    let output = "";
    let resolved = false;

    const proc = spawn("node", [cliPath, "auth", "login"], {
      env: { ...process.env, HOME: "/root" },
      stdio: ["ignore", "pipe", "pipe"],
    });

    loginProcess = proc;

    const onData = (chunk) => {
      output += chunk.toString();
      if (resolved) return;

      const urlMatch = output.match(/https:\/\/[^\s]+/);
      if (urlMatch) {
        resolved = true;
        resolve({
          login_url: urlMatch[0],
          message: "Open this URL in your browser to authenticate.",
        });
        // Don't kill proc — it needs to stay alive to receive the callback
      }
    };

    proc.stdout.on("data", onData);
    proc.stderr.on("data", onData);

    proc.on("close", (code) => {
      loginProcess = null;
      if (!resolved) {
        resolved = true;
        resolve({
          login_url: null,
          message: output || `Login process exited with code ${code}.`,
        });
      }
    });

    // Safety timeout: resolve with whatever we have after 15 seconds
    setTimeout(() => {
      if (!resolved) {
        resolved = true;
        resolve({
          login_url: null,
          message: output || "Login timed out. Check add-on logs.",
        });
      }
    }, 15000);
  });
}
