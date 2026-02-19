/**
 * MCP server configuration management.
 *
 * Persists MCP server configs to /data/mcp_servers.json.
 * Each server has a name, type (http/sse), and url.
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
 * @param {{type: string, url: string}} config
 */
export function setMcpServer(name, config) {
  const servers = loadMcpServers();
  servers[name] = { type: config.type || "http", url: config.url };
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
