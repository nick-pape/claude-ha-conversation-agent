# Claude Conversation Agent for Home Assistant

A custom Home Assistant integration that uses Anthropic's Claude as a conversation agent. Claude controls your smart home exclusively through [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers, giving it access to the same tools and services that Home Assistant exposes via its built-in `/api/mcp` endpoint (or any external MCP server you configure).

Home Assistant owns the voice pipeline (wake word, STT, TTS, Wyoming satellites). This integration slots in as the "brain" -- it receives transcribed text, runs an agentic loop against the Claude API, streams text back to TTS in real time, and executes tool calls through MCP.

## Features

- **Streaming responses** -- text is yielded to TTS as it arrives from Claude, so the user hears speech before tool calls finish.
- **MCP-only tool access** -- Claude never uses HA's native intent system or built-in service calls. All actions go through MCP, keeping the tool surface explicit and auditable.
- **Automatic transport negotiation** -- connects to MCP servers via Streamable HTTP, falling back to SSE if the server returns HTTP 405.
- **Multi-turn conversations** -- conversation history is kept in memory with a 5-minute session timeout, matching HA's default pipeline behavior.
- **Configurable system prompt** -- supports Jinja2 templates, so you can inject dynamic context (entity states, time of day, etc.) into the prompt.
- **Model selection** -- choose from Claude Sonnet 4.5, Claude Haiku 4.5, Claude Opus 4.5, or enter a custom model ID.
- **Tunable parameters** -- max tokens (up to 32,768) and temperature (0.0 -- 2.0).
- **HACS installable** -- no manual file copying required.

## Architecture

```
Wyoming satellite / HA app
        |
        v
  HA Voice Pipeline (wake word -> STT -> [conversation agent] -> TTS)
        |
        v
  ClaudeConversationEntity._async_handle_message()
        |
        v
  run_agent_loop()  ----stream text deltas----> ChatLog ----> TTS
        |                                          ^
        v                                          |
  Claude Messages API (streaming)                  |
        |                                          |
        +-- tool_use block? ----> MCPManager.call_tool()
        |                              |
        |                              v
        |                        MCP Server (HA /api/mcp or external)
        |                              |
        +<---- tool result ------------+
        |
        v
  Next Claude iteration (up to 10 tool rounds)
```

Key components:

| File | Role |
|------|------|
| `__init__.py` | Entry setup, creates the `AsyncAnthropic` client, forwards platforms. |
| `config_flow.py` | UI config flow: API key validation, conversation subentry (prompt, model, MCP). |
| `conversation.py` | `ClaudeConversationEntity` -- the HA conversation entity that wires everything together. |
| `agent.py` | `run_agent_loop()` -- async generator that streams Claude responses and dispatches tool calls. `ConversationStateManager` handles session TTL. |
| `mcp_manager.py` | `MCPManager` -- connects to MCP servers, discovers tools, executes tool calls with timeout. |
| `entity.py` | Base entity class with device info. |
| `const.py` | Domain name, defaults, limits. |

## Prerequisites

- **Home Assistant 2025.7.0** or newer
- **HACS** (Home Assistant Community Store) installed
- **Anthropic API key** from [console.anthropic.com](https://console.anthropic.com/)
- **MCP server** -- Home Assistant's built-in MCP server (available at `/api/mcp`) or any external MCP-compatible server
- A **long-lived access token** for the MCP server (generate one in your HA profile at `/profile/security`)

## Installation

1. Open HACS in your Home Assistant instance.
2. Go to **Integrations** and click the three-dot menu in the top right.
3. Select **Custom repositories**.
4. Enter the repository URL: `https://github.com/nickpwhite/claude-ha-conversation-agent`
5. Set the category to **Integration** and click **Add**.
6. Find "Claude Conversation Agent" in the HACS store and click **Download**.
7. Restart Home Assistant.

## Configuration

### Step 1: Add the integration

1. Go to **Settings > Devices & services > Add integration**.
2. Search for "Claude Conversation Agent".
3. Enter your Anthropic API key. The integration validates the key by listing models before saving.

### Step 2: Configure the conversation agent

After the integration is created, a default conversation agent subentry is automatically added. To reconfigure it (or add additional agents), open the integration and click **Configure** on the conversation agent.

**Basic settings:**

| Option | Description | Default |
|--------|-------------|---------|
| System prompt | Instructions for Claude. Supports Jinja2 templates. | A concise voice-assistant prompt. |
| Use recommended settings | When enabled, skips advanced model settings. | Enabled |

**Advanced settings** (when "Use recommended settings" is disabled):

| Option | Description | Default |
|--------|-------------|---------|
| Model | Claude model ID. | `claude-sonnet-4-5` |
| Maximum response tokens | Upper limit on tokens per response. | 1024 |
| Temperature | Controls randomness (0.0 = deterministic, 2.0 = creative). | 1.0 |

**MCP server settings:**

| Option | Description | Example |
|--------|-------------|---------|
| MCP server URL | The URL of your MCP server. | `http://homeassistant.local:8123/api/mcp` |
| MCP server access token | A long-lived access token for authentication. | *(generated in your HA profile)* |

### Step 3: Assign to a voice pipeline

1. Go to **Settings > Voice assistants**.
2. Select a voice pipeline (or create a new one).
3. Set the **Conversation agent** to "Claude Conversation Agent".

## Usage

Once configured, the agent responds to any voice pipeline that uses it:

- **Voice commands** via Wyoming satellites, HA Assist app, or browser.
- **Text input** via the Assist dialog in the HA dashboard (click the chat bubble in the top right).
- **Automations** that call the `conversation.process` service.

### Example interactions

> "Turn off the lights in the living room."

Claude calls the appropriate MCP tool (e.g., `ha__call_service`) and confirms the action.

> "What's the temperature in the bedroom?"

Claude reads sensor data through MCP and responds conversationally.

> "Set the thermostat to 72 and turn on the porch lights."

Claude handles multiple tool calls in sequence (up to 10 rounds per turn).

### System prompt tips

The system prompt supports Jinja2, so you can include dynamic HA data:

```yaml
# Example: inject the current time
You are a helpful home assistant. The current time is {{ now().strftime('%I:%M %p') }}.
Keep responses concise and conversational.
```

## Troubleshooting

### "Failed to connect to MCP server"

- Verify the MCP server URL is reachable from Home Assistant. If HA runs in a container, `localhost` may not resolve -- use `homeassistant.local` or the container's host IP.
- Check that the long-lived access token is valid and has not expired.
- Look for detailed errors in the Home Assistant log (`Settings > System > Logs`) and filter for `claude_conversation_agent`.

### "Authentication failed. Please update your API key."

- Your Anthropic API key is invalid or has been revoked. The integration will prompt you to re-authenticate.

### Agent responds but cannot control devices

- Confirm the MCP server URL and token are configured in the conversation agent settings.
- Check that the MCP server exposes the tools you expect. You can test with: `curl -H "Authorization: Bearer <token>" http://homeassistant.local:8123/api/mcp`
- Review the logs for lines like `Connected to MCP server 'ha' with N tools` -- if N is 0, tool discovery failed.

### Slow responses

- The agent streams text to TTS as soon as the first tokens arrive. If there is a delay, it is likely the initial Claude API call latency.
- Make sure `max_tokens` is not set unnecessarily high -- shorter limits mean faster responses for simple queries.
- Consider using `claude-haiku-4-5` for faster response times at the cost of some capability.

### Conversation context is lost

- Conversation state expires after 5 minutes of inactivity. This is intentional and matches HA's default session timeout.
- Reloading or reconfiguring the integration clears all conversation state.

### MCP connection drops

- The integration attempts to reconnect automatically. If the server is persistently unreachable, the agent will continue to work but without tool access.
- Check the MCP server's health independently to rule out server-side issues.

## Development

### Project structure

```
custom_components/claude_conversation_agent/
  __init__.py           # Integration setup
  config_flow.py        # Config flow UI
  conversation.py       # ConversationEntity
  agent.py              # Agent loop (async generator)
  mcp_manager.py        # MCP client connections
  entity.py             # Base entity
  const.py              # Constants and defaults
  manifest.json         # Integration metadata
  strings.json          # UI strings
  translations/en.json  # English translations
```

### Dependencies

From `manifest.json`:

- `anthropic==0.78.0` -- Anthropic Python SDK
- `mcp>=1.26.0` -- Model Context Protocol client library

### Key design decisions

1. **MCP-only tool access.** The integration passes `user_llm_hass_api=None` to `async_provide_llm_data`, which disables HA's native LLM tool system. All tool calls go through MCP, giving you full control over what Claude can do.

2. **Streaming via async generator.** `run_agent_loop()` is an async generator that yields `{"role": "assistant"}` and `{"content": "..."}` dicts. The conversation entity pipes these into HA's `ChatLog.async_add_delta_content_stream()`, which feeds the TTS delta listener.

3. **Tool call loop.** After each Claude response with `stop_reason == "tool_use"`, the agent executes the tool calls via MCP and feeds the results back as the next user message. This loop runs up to 10 iterations (`MAX_TOOL_ITERATIONS`).

4. **Namespaced tools.** MCP tools are namespaced as `{server_name}__{tool_name}` to avoid collisions when multiple MCP servers are connected.

### Running locally

For development, clone the repo into your HA custom_components directory:

```bash
cd /path/to/ha-config/custom_components
git clone https://github.com/nickpwhite/claude-ha-conversation-agent.git claude_conversation_agent
```

Or symlink:

```bash
ln -s /path/to/claude-ha-conversation-agent/custom_components/claude_conversation_agent \
      /path/to/ha-config/custom_components/claude_conversation_agent
```

Then restart Home Assistant. Changes to Python files require a restart or integration reload.

## License

MIT
