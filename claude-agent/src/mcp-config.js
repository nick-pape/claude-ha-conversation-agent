/**
 * MCP server configuration management.
 *
 * Persists MCP server configs to /data/mcp_servers.json.
 * Each server has a name, type (http/sse), url, and optional headers.
 * Header values may reference environment variables as ${NAME}; they are
 * expanded per request (see agent.js), so secrets such as SUPERVISOR_TOKEN
 * are never written to disk.
 */

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const DATA_DIR = "/data";
const CONFIG_FILE = join(DATA_DIR, "mcp_servers.json");

/**
 * Load all configured MCP servers.
 * @returns {Object<string, {type: string, url: string}>}
 */
export function loadMcpServers() {
  try {
    const raw = readFileSync(CONFIG_FILE, "utf-8");
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

/**
 * Save MCP server config to disk.
 * @param {Object<string, {type: string, url: string}>} servers
 */
function saveMcpServers(servers) {
  mkdirSync(DATA_DIR, { recursive: true });
  writeFileSync(CONFIG_FILE, JSON.stringify(servers, null, 2));
}

/**
 * Add or update an MCP server.
 * @param {string} name
 * @param {{type: string, url: string, headers?: Object<string, string>}} config
 */
export function setMcpServer(name, config) {
  const servers = loadMcpServers();
  servers[name] = { type: config.type || "http", url: config.url };
  if (config.headers && Object.keys(config.headers).length > 0) {
    servers[name].headers = config.headers;
  }
  saveMcpServers(servers);
  return servers;
}

/**
 * Remove an MCP server.
 * @param {string} name
 */
export function removeMcpServer(name) {
  const servers = loadMcpServers();
  delete servers[name];
  saveMcpServers(servers);
  return servers;
}
