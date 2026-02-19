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
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const CLAUDE_CONFIG_DIR = "/data/.claude";
const CREDENTIALS_FILE = join(CLAUDE_CONFIG_DIR, "credentials.json");

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

/**
 * Initiate the Max subscription login flow.
 *
 * Returns a URL the user must open in their browser to complete login.
 * The CLI handles the OAuth flow and persists credentials to ~/.claude/.
 *
 * @returns {{ login_url: string|null, message: string }}
 */
export async function initiateLogin() {
  try {
    // Use the Claude CLI to start the login flow
    // The CLI prints a URL to stdout for the user to open
    const { stdout, stderr } = await execFileAsync("claude", ["login"], {
      timeout: 30000,
      env: { ...process.env, HOME: "/root" },
    });

    // Extract URL from output
    const urlMatch = stdout.match(/https:\/\/[^\s]+/);
    if (urlMatch) {
      return {
        login_url: urlMatch[0],
        message: "Open this URL in your browser to authenticate.",
      };
    }

    return {
      login_url: null,
      message: stdout || stderr || "Login initiated. Check add-on logs.",
    };
  } catch (err) {
    return {
      login_url: null,
      message: `Login failed: ${err.message}`,
    };
  }
}
