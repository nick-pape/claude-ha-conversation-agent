/**
 * Authentication management for the Claude Agent add-on.
 *
 * Supports two modes:
 *   - api_key: Key passed per-request from the HA integration (no state here)
 *   - max: Claude Max subscription via OAuth PKCE flow.
 *          Credentials persist in /data/.claude/ (symlinked to ~/.claude/).
 *
 * The OAuth flow is implemented directly (not via the CLI) because the CLI
 * requires a TTY to accept the authorization code in headless environments.
 *
 * Uses a localhost redirect_uri so the authorization code appears in the
 * browser's address bar (the page won't load since localhost points to the
 * container, but the URL is visible and copyable).
 */

import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import crypto from "node:crypto";

const CLAUDE_CONFIG_DIR = "/data/.claude";
const CREDENTIALS_FILE = join(CLAUDE_CONFIG_DIR, ".credentials.json");

// OAuth constants (from the Claude Code CLI)
const BASE_URL = "https://claude.ai";
const CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";
const LOCALHOST_PORT = 18923;
const REDIRECT_URI = `http://localhost:${LOCALHOST_PORT}/callback`;
const TOKEN_URL = "https://platform.claude.com/v1/oauth/token";
const SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers";

// In-flight PKCE state for the current login attempt
let pendingLogin = null;

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
      const oauth = creds.claudeAiOauth;
      if (oauth?.accessToken) {
        maxAuthenticated = true;
        maxEmail = oauth.emailAddress || null;
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
 * Generate a cryptographically random URL-safe string.
 */
function randomUrlSafe(bytes) {
  return crypto.randomBytes(bytes).toString("base64url");
}

/**
 * Initiate the Max subscription login flow via OAuth PKCE.
 *
 * Generates a PKCE code_verifier/code_challenge pair and returns
 * the authorization URL for the user to visit.
 *
 * @returns {{ login_url: string|null, message: string }}
 */
export async function initiateLogin() {
  const codeVerifier = randomUrlSafe(32);
  const codeChallenge = crypto
    .createHash("sha256")
    .update(codeVerifier)
    .digest("base64url");
  const state = randomUrlSafe(32);

  pendingLogin = { codeVerifier, state };

  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    response_type: "code",
    redirect_uri: REDIRECT_URI,
    scope: SCOPES,
    code_challenge: codeChallenge,
    code_challenge_method: "S256",
    state,
  });

  const loginUrl = `${BASE_URL}/oauth/authorize?${params}`;

  return {
    login_url: loginUrl,
    message: "Open this URL in your browser to authenticate.",
  };
}

/**
 * Complete the login by exchanging the authorization code for tokens.
 *
 * Accepts either a raw authorization code or a full callback URL
 * (http://localhost:.../callback?code=...&state=...).
 *
 * @param {string} input - The authorization code or full callback URL
 * @returns {{ success: boolean, message: string }}
 */
export async function completeLogin(input) {
  if (!pendingLogin) {
    return {
      success: false,
      message: "No login in progress. Click 'Login' first.",
    };
  }

  const { codeVerifier, state } = pendingLogin;
  pendingLogin = null;

  // Extract the code from a full URL or use raw input
  let code = input;
  try {
    const url = new URL(input);
    const urlCode = url.searchParams.get("code");
    if (urlCode) code = urlCode;
  } catch {
    // Not a URL — use as raw code
  }

  try {
    const tokenBody = {
      grant_type: "authorization_code",
      code,
      redirect_uri: REDIRECT_URI,
      client_id: CLIENT_ID,
      code_verifier: codeVerifier,
      state,
    };

    const tokenRes = await fetch(TOKEN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(tokenBody),
    });

    const resText = await tokenRes.text();

    if (!tokenRes.ok) {
      return {
        success: false,
        message: `Token exchange failed (${tokenRes.status}): ${resText}`,
      };
    }

    const tokens = JSON.parse(resText);
    const accessToken = tokens.access_token;
    const refreshToken = tokens.refresh_token;
    const expiresIn = tokens.expires_in;

    if (!accessToken) {
      return {
        success: false,
        message: "Token exchange returned no access token.",
      };
    }

    // Fetch the user's profile to get email and subscription info
    let emailAddress = null;
    let subscriptionType = null;
    let rateLimitTier = null;
    try {
      const rolesRes = await fetch(
        "https://api.anthropic.com/api/oauth/claude_cli/roles",
        { headers: { Authorization: `Bearer ${accessToken}` } }
      );
      if (rolesRes.ok) {
        const roles = await rolesRes.json();
        emailAddress = roles.organization_name?.replace("'s Organization", "");
        subscriptionType = roles.subscription_type || "max";
        rateLimitTier = roles.rate_limit_tier || "default_claude_max_20x";
      }
    } catch {
      // Profile fetch is optional — credentials still work without it
    }

    // Persist credentials in the format the CLI expects:
    // File: .credentials.json (dot-prefixed)
    // Structure: { claudeAiOauth: { accessToken, refreshToken, expiresAt (ms), ... } }
    const credentials = {
      claudeAiOauth: {
        accessToken,
        refreshToken,
        expiresAt: expiresIn
          ? Date.now() + expiresIn * 1000
          : null,
        scopes: SCOPES.split(" "),
        subscriptionType: subscriptionType || "max",
        rateLimitTier: rateLimitTier || "default_claude_max_20x",
      },
    };

    // Store email in the credential for status display
    if (emailAddress) {
      credentials.claudeAiOauth.emailAddress = emailAddress;
    }

    mkdirSync(CLAUDE_CONFIG_DIR, { recursive: true });
    writeFileSync(CREDENTIALS_FILE, JSON.stringify(credentials));

    // Also write to ~/.claude/ so the CLI can find it
    const homeDir = process.env.HOME || "/root";
    const homeClaude = join(homeDir, ".claude");
    mkdirSync(homeClaude, { recursive: true });
    writeFileSync(join(homeClaude, ".credentials.json"), JSON.stringify(credentials));

    const email = emailAddress || "unknown";
    return {
      success: true,
      message: `Authenticated as ${email}`,
    };
  } catch (err) {
    return {
      success: false,
      message: `Login failed: ${err.message}`,
    };
  }
}
