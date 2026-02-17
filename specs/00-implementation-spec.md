# Implementation Specification: Claude HA Conversation Agent

> Synthesized from research specs 01-05, dated 2026-02-17.
> This document defines the complete architecture and design for a Home Assistant
> custom conversation agent integration that uses Claude via the raw Anthropic API
> and connects to external services exclusively through MCP servers.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Key Architectural Decisions](#2-key-architectural-decisions)
3. [File Structure](#3-file-structure)
4. [Configuration Schema](#4-configuration-schema)
5. [Component Lifecycle](#5-component-lifecycle)
6. [Agent Loop & Streaming Design](#6-agent-loop--streaming-design)
7. [MCP Server Management](#7-mcp-server-management)
8. [Multi-Turn Conversation](#8-multi-turn-conversation)
9. [Error Handling Strategy](#9-error-handling-strategy)
10. [Interface Contracts](#10-interface-contracts)
11. [Open Questions](#11-open-questions)

---

## 1. Architecture Overview

### 1.1 Design Philosophy

- **HA owns the voice pipeline.** Wake word, STT, TTS, satellite management are all HA's
  responsibility. Our integration is a conversation agent: text in, text out.
- **Claude is the brain.** All reasoning, planning, and decision-making happen in Claude
  via the Anthropic Messages API.
- **MCP is the only tool interface.** Claude never calls HA intents or native services
  directly. All external actions go through MCP servers. HA's own MCP server at `/api/mcp`
  exposes entity control, state queries, etc.
- **Streaming is first-class.** Text deltas flow from Claude through HA's ChatLog to the
  TTS engine in real-time, enabling low-latency voice responses.

### 1.2 Architecture Diagram

```
                         ┌─────────────────────────────────────────┐
                         │          VOICE PIPELINE (HA)            │
                         │                                         │
   User speaks ──►  Wyoming Satellite ──► STT Engine               │
                         │                    │                    │
                         │              transcribed text            │
                         │                    │                    │
                         │                    ▼                    │
                         │         ┌─────────────────────┐        │
                         │         │  Pipeline: INTENT    │        │
                         │         │  async_converse()    │        │
                         │         └─────────┬───────────┘        │
                         │                   │                    │
                         └───────────────────┼────────────────────┘
                                             │
                    ┌────────────────────────┼────────────────────────┐
                    │   OUR INTEGRATION      │                        │
                    │                        ▼                        │
                    │  ┌──────────────────────────────────────────┐   │
                    │  │ ClaudeConversationEntity                 │   │
                    │  │ ._async_handle_message(user_input,       │   │
                    │  │                        chat_log)         │   │
                    │  └──────────────┬───────────────────────────┘   │
                    │                 │                                │
                    │                 ▼                                │
                    │  ┌──────────────────────────────────────────┐   │
                    │  │  chat_log.async_add_delta_content_stream │   │
                    │  │  ◄── consumes async generator ──►        │   │
                    │  └──────────┬───────────────────────────────┘   │
                    │             │                                    │
                    │     ┌───────┴───────┐                           │
                    │     │ delta_listener │ ── content deltas ──►    │
                    │     └───────────────┘     tts_input_stream      │
                    │                              │                  │
                    │                              ▼                  │
                    │                     TTS Engine ──► Satellite    │
                    │                                    Speaker      │
                    │                                                  │
                    │  ┌──────────────────────────────────────────┐   │
                    │  │           AGENT LOOP                     │   │
                    │  │  (async generator yielding text deltas)  │   │
                    │  │                                          │   │
                    │  │  ┌────────────────────────────────┐      │   │
                    │  │  │ Claude Messages API (streaming) │      │   │
                    │  │  │  - system prompt                │      │   │
                    │  │  │  - conversation history          │      │   │
                    │  │  │  - MCP tool definitions          │      │   │
                    │  │  └────────┬───────────────────────┘      │   │
                    │  │           │                               │   │
                    │  │     ┌─────┴─────┐                        │   │
                    │  │     │ text_delta │──► yield to chat_log   │   │
                    │  │     │ tool_use   │──► dispatch via MCP    │   │
                    │  │     └───────────┘                        │   │
                    │  └──────────────────────────────────────────┘   │
                    └─────────────────────────────────────────────────┘
                                             │
                    ┌────────────────────────┼────────────────────────┐
                    │     MCP SERVERS        │                        │
                    │                        ▼                        │
                    │  ┌─────────────────┐  ┌──────────────────┐     │
                    │  │ HA MCP Server   │  │ External MCP     │     │
                    │  │ /api/mcp        │  │ Servers           │     │
                    │  │ (entity control,│  │ (calendar, email, │     │
                    │  │  state queries) │  │  custom tools)    │     │
                    │  └─────────────────┘  └──────────────────┘     │
                    └─────────────────────────────────────────────────┘
```

### 1.3 Data Flow (Single Turn)

```
1. Pipeline calls async_converse(text, conversation_id, ...)
2. HA opens ChatSession + ChatLog (with delta_listener attached)
3. Our _async_handle_message is called
4. We call chat_log.async_provide_llm_data(..., user_llm_hass_api=None)
   → System prompt is rendered, no HA tools provided
5. We start chat_log.async_add_delta_content_stream(entity_id, agent_loop_generator)
6. Agent loop generator:
   a. Calls Claude Messages API with streaming
   b. Yields {"role": "assistant", "content": "Let me ..."} text deltas
      → delta_listener feeds these to tts_input_stream for TTS
   c. Detects tool_use blocks in Claude's response
   d. Executes tools via MCP: session.call_tool(name, args)
   e. Feeds tool results back to Claude messages
   f. Repeats from (a) until Claude's stop_reason == "end_turn"
   g. Final text deltas are yielded
7. chat_log.async_add_delta_content_stream finishes
   → Last AssistantContent is in chat_log
8. Return conversation.async_get_result_from_chat_log(user_input, chat_log)
```

---

## 2. Key Architectural Decisions

### 2.1 Raw Anthropic API + MCP Python SDK (not Claude Agent SDK)

**Decision:** Use the `anthropic` Python package for LLM calls and the `mcp` Python
package for MCP connections. Do NOT use the `claude-agent-sdk` package.

**Rationale:**
- The Agent SDK spawns a Node.js subprocess (Claude Code CLI) which adds significant
  overhead, especially on Raspberry Pi / low-power HA hosts.
- The raw API gives full control over the streaming pipeline and agent loop, which we
  need for precise integration with HA's ChatLog delta protocol.
- The `mcp` Python package handles the MCP protocol correctly without subprocess overhead.
- The Agent SDK includes dozens of built-in tools (Bash, Write, Edit, etc.) that we
  don't need and would have to explicitly disable.

**Trade-off:** More code to write for the agent loop and tool dispatch. This is
acceptable because the agent loop is straightforward and we need precise control.

### 2.2 Tool Handling Outside ChatLog

**Decision:** Handle all MCP tool calls in our agent loop generator. Only yield text
content deltas to `chat_log.async_add_delta_content_stream`. Never include `tool_calls`
in the deltas sent to the chat_log.

**Rationale:**
- We call `async_provide_llm_data` with `user_llm_hass_api=None`, so `chat_log.llm_api`
  is `None`.
- If we included `tool_calls` in deltas, the chat_log would try to dispatch them via
  `self.llm_api.async_call_tool(...)` which would crash.
- By handling tools ourselves, we maintain full control over MCP dispatch, error handling,
  and retry logic.
- The chat_log only sees text — clean and simple.

**Trade-off:** HA's pipeline has a streaming trigger that fires when tool_calls appear
after content (`start_streaming = delta_character_count > 0 and delta.get("tool_calls")`).
Since we don't pass tool_calls, this trigger never fires. Streaming for short pre-tool
text (<60 characters) relies solely on the 60-character threshold. This means responses
like "OK, checking..." (15 chars) won't trigger streaming until the post-tool response
pushes the total over 60 characters. Acceptable for v1; can be optimized later by
registering a custom `llm.API` wrapper.

### 2.3 No HA Intent/Service Integration

**Decision:** Claude accesses HA exclusively through MCP servers. We do NOT pass any
`user_llm_hass_api` to `async_provide_llm_data`.

**Rationale:** This is a core requirement from the user. MCP provides a clean, protocol-
standard interface that works identically for HA and non-HA services.

### 2.4 Dual State Management

**Decision:** Maintain two separate state stores:
1. **HA ChatLog:** Stores text-only conversation visible to HA. Used for TTS streaming
   and generating `ConversationResult`.
2. **Our ConversationState:** Stores the full Claude message history including tool
   calls and results. Used for Claude API calls.

**Rationale:** The ChatLog is HA's interface and must follow HA's protocols. Claude's
message history needs full tool call/result context for proper reasoning across turns.
These are different views of the same conversation.

### 2.5 Persistent MCP Connections

**Decision:** Maintain persistent MCP sessions for the lifetime of the integration,
with automatic reconnection on failure. Do not use HA's connect-per-operation pattern.

**Rationale:** Voice assistant latency is critical. Connect-per-operation adds ~100-500ms
per tool call (TCP handshake, MCP initialization). A persistent session eliminates this
overhead. The Agent SDK also uses persistent connections.

**Trade-off:** Must handle connection drops, server restarts, and cleanup on integration
unload. The `AsyncExitStack` pattern from the MCP SDK handles this cleanly.

---

## 3. File Structure

```
claude-ha-conversation-agent/                 # Repository root
├── hacs.json                                 # HACS metadata
├── README.md                                 # Documentation
├── LICENSE                                   # License
├── specs/                                    # Design documents (not deployed)
│   ├── 00-implementation-spec.md             # This file
│   ├── 01-ha-conversation-interface.md
│   ├── 02-claude-agent-sdk.md
│   ├── 03-mcp-connectivity.md
│   ├── 04-ha-streaming-pipeline.md
│   └── 05-integration-boilerplate.md
│
└── custom_components/
    └── claude_conversation_agent/            # Integration directory
        ├── __init__.py                       # Setup, teardown, entry management
        ├── manifest.json                     # HA integration manifest
        ├── config_flow.py                    # Config flow + subentry flows
        ├── conversation.py                   # ConversationEntity platform setup
        ├── entity.py                         # Base entity + agent loop
        ├── const.py                          # Constants, defaults, config keys
        ├── mcp_manager.py                    # MCP connection + tool management
        ├── agent.py                          # Claude agent loop (async generator)
        ├── strings.json                      # UI strings
        └── translations/
            └── en.json                       # Resolved English translations
```

### File Responsibilities

| File | Responsibility |
|------|---------------|
| `__init__.py` | Create Anthropic client, validate API key, forward to conversation platform, manage update listener |
| `manifest.json` | Integration metadata, dependencies (`conversation`), pip requirements (`anthropic`, `mcp`) |
| `config_flow.py` | Main flow: API key. Subentry flow: agent name, prompt, model, MCP server URL/token |
| `conversation.py` | `async_setup_entry`: iterate subentries, create `ClaudeConversationEntity` per subentry |
| `entity.py` | `ClaudeBaseLLMEntity`: holds config, device info, `_async_handle_chat_log` |
| `const.py` | `DOMAIN`, config key constants, default values |
| `mcp_manager.py` | `MCPManager`: persistent connections, tool discovery, tool execution, reconnection |
| `agent.py` | `run_agent_loop()`: async generator that calls Claude API, dispatches MCP tools, yields text deltas |
| `strings.json` | UI strings with `[%key:]` references (for `strings.json` only) |
| `translations/en.json` | Resolved English strings (copy of strings.json with resolved references) |

---

## 4. Configuration Schema

### 4.1 Main Config Entry (API Key)

Stored in `entry.data`:

```python
{
    "api_key": "sk-ant-api03-..."    # Anthropic API key
}
```

**Config flow step:** `async_step_user`
- Input: API key (TextSelector, password mode)
- Validation: `client.models.list(timeout=10.0)`
- On success: Create entry + default conversation subentry

### 4.2 Conversation Subentry

Stored in `subentry.data`:

```python
{
    # Basic settings (step: init)
    "prompt": "You are a helpful voice assistant...",   # System prompt (Jinja2 template)
    "recommended": True,                                 # Use defaults toggle

    # Advanced settings (step: advanced, shown when recommended=False)
    "chat_model": "claude-sonnet-4-5",                  # Claude model ID
    "max_tokens": 1024,                                  # Max response tokens
    "temperature": 1.0,                                  # Sampling temperature

    # MCP settings (step: mcp)
    "mcp_server_url": "http://localhost:8123/api/mcp",  # Primary MCP server URL
    "mcp_server_token": "eyJ...",                        # Long-lived access token
}
```

### 4.3 Subentry Flow Steps

```
async_step_user (new subentry):
  → async_step_init

async_step_reconfigure (edit existing):
  → async_step_init (with current values pre-filled)

async_step_init (Basic Settings):
  Fields: name, prompt (TemplateSelector), recommended (bool)
  If recommended=True → async_step_mcp
  If recommended=False → async_step_advanced

async_step_advanced (Advanced Settings):
  Fields: chat_model (SelectSelector), max_tokens (NumberSelector),
          temperature (NumberSelector 0.0-2.0)
  → async_step_mcp

async_step_mcp (MCP Server):
  Fields: mcp_server_url (TextSelector), mcp_server_token (TextSelector, password)
  last_step=True → create/update entry
```

### 4.4 Default Values

```python
DEFAULT_CONVERSATION_NAME = "Claude Conversation Agent"

DEFAULT_CONVERSATION_OPTIONS = {
    CONF_RECOMMENDED: True,
    CONF_PROMPT: "You are a helpful voice assistant for a smart home. "
                 "Keep responses concise and conversational. "
                 "When performing actions, briefly confirm what you did.",
}

DEFAULTS = {
    CONF_CHAT_MODEL: "claude-sonnet-4-5",
    CONF_MAX_TOKENS: 1024,
    CONF_TEMPERATURE: 1.0,
}
```

### 4.5 Model Selector Options

```python
RECOMMENDED_MODELS = [
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-opus-4-5",
]
```

SelectSelector with `custom_value=True` to allow typing any model ID.

### 4.6 manifest.json

```json
{
    "domain": "claude_conversation_agent",
    "name": "Claude Conversation Agent",
    "after_dependencies": ["assist_pipeline", "intent"],
    "codeowners": ["@nickpwhite"],
    "config_flow": true,
    "dependencies": ["conversation"],
    "documentation": "https://github.com/nickpwhite/claude-ha-conversation-agent",
    "integration_type": "service",
    "iot_class": "cloud_polling",
    "issue_tracker": "https://github.com/nickpwhite/claude-ha-conversation-agent/issues",
    "requirements": ["anthropic==0.78.0", "mcp>=1.26.0"],
    "version": "0.1.0"
}
```

### 4.7 hacs.json

```json
{
    "name": "Claude Conversation Agent",
    "render_readme": true,
    "homeassistant": "2025.7.0"
}
```

---

## 5. Component Lifecycle

### 5.1 Integration Setup (`__init__.py`)

```python
type ClaudeAgentConfigEntry = ConfigEntry[anthropic.AsyncAnthropic]

PLATFORMS = (Platform.CONVERSATION,)

async def async_setup_entry(hass, entry: ClaudeAgentConfigEntry) -> bool:
    # 1. Create Anthropic async client (uses HA's shared httpx client)
    client = anthropic.AsyncAnthropic(
        api_key=entry.data[CONF_API_KEY],
        http_client=get_async_client(hass),
    )

    # 2. Validate API key
    try:
        await client.models.list(timeout=10.0)
    except anthropic.AuthenticationError as err:
        raise ConfigEntryAuthFailed(err) from err
    except anthropic.AnthropicError as err:
        raise ConfigEntryNotReady(err) from err

    # 3. Store client as runtime data
    entry.runtime_data = client

    # 4. Forward to conversation platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 5. Register update listener (triggers reload on config change)
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True
```

### 5.2 Conversation Platform Setup (`conversation.py`)

```python
async def async_setup_entry(hass, config_entry, async_add_entities):
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "conversation":
            continue
        async_add_entities(
            [ClaudeConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )
```

### 5.3 Entity Lifecycle

```python
class ClaudeConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
    ClaudeBaseLLMEntity,
):
    _attr_supports_streaming = True

    def __init__(self, entry, subentry):
        super().__init__(entry, subentry)
        self._attr_supported_features = (
            conversation.ConversationEntityFeature.CONTROL
        )
        self._mcp_manager = MCPManager(hass=None)  # Set in async_added_to_hass
        self._conversation_states: dict[str, ConversationState] = {}

    async def async_added_to_hass(self):
        """Connect to MCP servers when entity is added."""
        await super().async_added_to_hass()
        self._mcp_manager = MCPManager(self.hass)
        mcp_url = self.subentry.data.get(CONF_MCP_SERVER_URL)
        mcp_token = self.subentry.data.get(CONF_MCP_SERVER_TOKEN)
        if mcp_url:
            await self._mcp_manager.connect(
                name="ha",
                url=mcp_url,
                token=mcp_token,
            )

    async def async_will_remove_from_hass(self):
        """Disconnect MCP servers when entity is removed."""
        await self._mcp_manager.disconnect_all()
        self._conversation_states.clear()
        await super().async_will_remove_from_hass()
```

### 5.4 Integration Teardown

```python
async def async_unload_entry(hass, entry) -> bool:
    # Entity's async_will_remove_from_hass handles MCP cleanup
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```

---

## 6. Agent Loop & Streaming Design

### 6.1 `_async_handle_message` (Entry Point)

This is the method called by HA for each conversation turn.

```python
async def _async_handle_message(
    self,
    user_input: conversation.ConversationInput,
    chat_log: conversation.ChatLog,
) -> conversation.ConversationResult:
    options = self.subentry.data

    # 1. Set up system prompt (no HA tools)
    try:
        await chat_log.async_provide_llm_data(
            user_input.as_llm_context(DOMAIN),
            user_llm_hass_api=None,           # No HA native tools
            user_llm_prompt=options.get(CONF_PROMPT),
            user_extra_system_prompt=user_input.extra_system_prompt,
        )
    except conversation.ConverseError as err:
        return err.as_conversation_result()

    # 2. Get or create conversation state
    state = self._get_or_create_state(chat_log.conversation_id)

    # 3. Get system prompt from chat_log
    system_prompt = chat_log.content[0].content

    # 4. Run agent loop through chat_log's streaming interface
    async for _ in chat_log.async_add_delta_content_stream(
        self.entity_id,
        run_agent_loop(
            client=self.entry.runtime_data,
            mcp_manager=self._mcp_manager,
            system_prompt=system_prompt,
            user_text=user_input.text,
            conversation_state=state,
            model=options.get(CONF_CHAT_MODEL, DEFAULTS[CONF_CHAT_MODEL]),
            max_tokens=options.get(CONF_MAX_TOKENS, DEFAULTS[CONF_MAX_TOKENS]),
            temperature=options.get(CONF_TEMPERATURE, DEFAULTS[CONF_TEMPERATURE]),
        ),
    ):
        pass  # Content is added to chat_log automatically

    # 5. Return result from chat_log
    return conversation.async_get_result_from_chat_log(user_input, chat_log)
```

### 6.2 Agent Loop (async generator in `agent.py`)

The agent loop is an async generator that yields `AssistantContentDeltaDict` dicts.
It handles the full Claude API interaction including multi-step tool use.

```python
async def run_agent_loop(
    client: anthropic.AsyncAnthropic,
    mcp_manager: MCPManager,
    system_prompt: str,
    user_text: str,
    conversation_state: ConversationState,
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[AssistantContentDeltaDict]:
    """
    Async generator that:
    1. Calls Claude API with streaming
    2. Yields text deltas (consumed by chat_log for TTS streaming)
    3. Handles tool_use blocks by dispatching to MCP
    4. Loops until Claude returns end_turn
    """

    # Get MCP tools formatted for Claude
    tools = mcp_manager.get_claude_tools()

    # Add user message to conversation history
    messages = conversation_state.messages
    messages.append({"role": "user", "content": user_text})

    for _iteration in range(MAX_TOOL_ITERATIONS):
        # Build API call args
        api_args = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
        if temperature != 1.0:
            api_args["temperature"] = temperature
        if tools:
            api_args["tools"] = tools

        # Call Claude with streaming
        async with client.messages.stream(**api_args) as stream:
            text_started = False

            async for event in stream:
                # Yield text deltas for TTS streaming
                if event.type == "content_block_start":
                    if event.content_block.type == "text":
                        if not text_started:
                            yield {"role": "assistant"}
                            text_started = True

                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield {"content": event.delta.text}

                # tool_use events are consumed but NOT yielded
                # (we handle them ourselves after the stream completes)

            final_message = await stream.get_final_message()

        # Add complete assistant response to our conversation history
        messages.append({
            "role": "assistant",
            "content": [block.model_dump() for block in final_message.content],
        })

        # If no tool calls, we're done
        if final_message.stop_reason == "end_turn":
            break

        # Execute tool calls via MCP
        if final_message.stop_reason == "tool_use":
            tool_results = []
            for block in final_message.content:
                if block.type == "tool_use":
                    result = await mcp_manager.call_tool(
                        block.name, block.input
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            # Add tool results to conversation history
            messages.append({"role": "user", "content": tool_results})

        # Next iteration will call Claude again with tool results
        text_started = False
```

### 6.3 Delta Protocol Compliance

Our generator yields deltas conforming to `AssistantContentDeltaDict`:

| Delta | When | Purpose |
|-------|------|---------|
| `{"role": "assistant"}` | First text of each Claude API call | Starts a new AssistantContent in chat_log |
| `{"content": "chunk"}` | Each text_delta from Claude stream | Appended to current AssistantContent; fed to TTS via delta_listener |

We intentionally **never** yield:
- `{"tool_calls": [...]}` — would crash because `chat_log.llm_api` is None
- `{"thinking_content": "..."}` — not needed for v1 (can add for extended thinking later)
- `{"native": ...}` — not needed for v1

### 6.4 Streaming to TTS

The pipeline attaches a `delta_listener` to the ChatLog before calling `async_converse()`.
This listener receives our text deltas and routes them:

```
Our generator yields:  {"role": "assistant", "content": "Let me "}
                       {"content": "check that."}
                       ...tool execution (no yields)...
                       {"role": "assistant", "content": "The "}
                       {"content": "temperature is 72°F."}

delta_listener receives each delta, extracts "content", puts it in tts_input_stream.

Pipeline streaming logic:
  - Accumulates character count
  - When count > 60 OR tool_calls appear: begins streaming to TTS
  - Short responses (<60 chars total): delivered non-streaming after async_converse returns
```

### 6.5 Streaming Timeline for Tool Use

```
Time ──────────────────────────────────────────────────────────────►

Claude API call 1:
  [text deltas: "Let me check the temperature..."]  ←── streamed to TTS
  [tool_use: get_temperature]                        ←── NOT in deltas

MCP tool execution:
  [...executing HassTurnOn via MCP...]               ←── silence (or TTS playing)

Claude API call 2:
  [text deltas: "It's currently 72°F."]              ←── streamed to TTS

Result returned to HA pipeline.
```

During the MCP tool execution gap, the user hears whatever text has already been
streamed to TTS. If Claude produced enough text before the tool call ("Let me check
the temperature in your living room for you..."), the user hears that while waiting.
If the pre-tool text was very short, the user experiences a brief silence.

### 6.6 Interaction with 60-Character Threshold

The pipeline starts streaming to TTS when accumulated content exceeds 60 characters.
Since our deltas don't include `tool_calls`, the alternate trigger
(`delta_character_count > 0 and delta.get("tool_calls")`) never fires.

**Practical impact:**
- Responses >60 chars: TTS streaming starts as expected
- "OK, checking..." (15 chars) followed by tool → silence until post-tool response
- Combined pre-tool + post-tool text >60 chars → streaming starts at threshold

**Mitigation (system prompt):** Instruct Claude to provide slightly longer
acknowledgments before tool calls to maximize streaming opportunities.

---

## 7. MCP Server Management

### 7.1 MCPManager Class (`mcp_manager.py`)

```python
class MCPServerConnection:
    """A single MCP server connection."""
    name: str                           # Server name (used for tool namespacing)
    url: str                            # Server URL
    session: ClientSession | None       # Active MCP session
    tools: list[MCPTool]                # Discovered tools
    _exit_stack: AsyncExitStack         # Resource cleanup

class MCPManager:
    """Manages connections to multiple MCP servers."""
    _connections: dict[str, MCPServerConnection]
    _tool_registry: dict[str, tuple[str, str]]  # namespaced_name → (server_name, original_name)

    async def connect(self, name: str, url: str, token: str | None = None) -> None
    async def disconnect(self, name: str) -> None
    async def disconnect_all(self) -> None
    async def refresh_tools(self, name: str) -> None
    async def call_tool(self, namespaced_name: str, arguments: dict) -> str
    def get_claude_tools(self) -> list[dict]
```

### 7.2 Connection Lifecycle

```python
async def connect(self, name: str, url: str, token: str | None = None):
    """Connect to an MCP server and discover tools."""
    conn = MCPServerConnection(name=name, url=url)
    conn._exit_stack = AsyncExitStack()

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Try Streamable HTTP first, fall back to SSE
    try:
        read, write, _ = await conn._exit_stack.enter_async_context(
            streamable_http_client(url=url, headers=headers)
        )
    except (ExceptionGroup, httpx.HTTPStatusError):
        read, write = await conn._exit_stack.enter_async_context(
            sse_client(url=url, headers=headers)
        )

    conn.session = await conn._exit_stack.enter_async_context(
        ClientSession(read, write)
    )
    await conn.session.initialize()

    # Discover tools
    result = await conn.session.list_tools()
    conn.tools = result.tools

    # Register tools with namespacing
    for tool in conn.tools:
        namespaced = f"{name}__{tool.name}"
        self._tool_registry[namespaced] = (name, tool.name)

    self._connections[name] = conn
```

### 7.3 Tool Namespacing

MCP tools are namespaced to prevent collisions across servers:

```
Server "ha" + Tool "HassTurnOn"  →  "ha__HassTurnOn"
Server "cal" + Tool "list_events" →  "cal__list_events"
```

Claude sees the namespaced names. When Claude calls `ha__HassTurnOn`, we:
1. Parse the prefix: server = `"ha"`, tool = `"HassTurnOn"`
2. Look up the connection for `"ha"`
3. Call `session.call_tool("HassTurnOn", arguments)`

### 7.4 Tool Format Conversion

MCP tool → Claude API tool definition:

```python
def get_claude_tools(self) -> list[dict]:
    """Get all MCP tools formatted for the Claude API."""
    tools = []
    for conn in self._connections.values():
        for tool in conn.tools:
            tools.append({
                "name": f"{conn.name}__{tool.name}",
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            })
    return tools
```

### 7.5 Tool Execution

```python
async def call_tool(self, namespaced_name: str, arguments: dict) -> str:
    """Execute a tool call via the appropriate MCP server."""
    if namespaced_name not in self._tool_registry:
        return json.dumps({"error": f"Unknown tool: {namespaced_name}"})

    server_name, tool_name = self._tool_registry[namespaced_name]
    conn = self._connections.get(server_name)

    if not conn or not conn.session:
        return json.dumps({"error": f"MCP server '{server_name}' not connected"})

    try:
        result = await asyncio.wait_for(
            conn.session.call_tool(tool_name, arguments),
            timeout=30.0,
        )
        # Convert MCP result to string for Claude
        content_parts = []
        for item in result.content:
            if hasattr(item, "text"):
                content_parts.append(item.text)
            else:
                content_parts.append(str(item))
        return "\n".join(content_parts) if content_parts else "OK"

    except TimeoutError:
        return json.dumps({"error": f"Tool '{tool_name}' timed out"})
    except Exception as err:
        return json.dumps({"error": f"Tool '{tool_name}' failed: {err}"})
```

### 7.6 Reconnection Strategy

```python
async def _ensure_connected(self, name: str) -> bool:
    """Ensure a server connection is active, reconnecting if needed."""
    conn = self._connections.get(name)
    if not conn:
        return False

    try:
        # Heartbeat: list_tools as a health check
        await asyncio.wait_for(conn.session.list_tools(), timeout=5.0)
        return True
    except Exception:
        _LOGGER.warning("MCP server '%s' connection lost, reconnecting", name)
        await conn._exit_stack.aclose()
        try:
            await self.connect(name, conn.url, conn.token)
            return True
        except Exception as err:
            _LOGGER.error("Failed to reconnect to MCP server '%s': %s", name, err)
            return False
```

### 7.7 Periodic Tool Refresh

Tools are refreshed every 30 minutes (matching HA's MCP client pattern) and on
reconnection. This catches tools added/removed on the MCP server.

```python
async def _schedule_refresh(self):
    """Schedule periodic tool refresh."""
    while True:
        await asyncio.sleep(1800)  # 30 minutes
        for name in list(self._connections):
            try:
                await self.refresh_tools(name)
            except Exception:
                _LOGGER.warning("Failed to refresh tools for '%s'", name)
```

---

## 8. Multi-Turn Conversation

### 8.1 Conversation State

```python
@dataclass
class ConversationState:
    """Stores Claude's conversation history for a session."""
    messages: list[dict]      # Claude API message format
    created: float            # time.monotonic() timestamp
    last_accessed: float      # time.monotonic() timestamp
```

### 8.2 State Management

```python
class ConversationStateManager:
    """TTL cache for conversation states."""

    TTL_SECONDS = 300  # 5 minutes (matches HA session timeout)

    def __init__(self):
        self._states: dict[str, ConversationState] = {}

    def get_or_create(self, conversation_id: str) -> ConversationState:
        """Get existing state or create a new one."""
        self._cleanup_expired()
        now = time.monotonic()

        if conversation_id in self._states:
            state = self._states[conversation_id]
            state.last_accessed = now
            return state

        state = ConversationState(
            messages=[],
            created=now,
            last_accessed=now,
        )
        self._states[conversation_id] = state
        return state

    def _cleanup_expired(self):
        """Remove states that haven't been accessed within TTL."""
        now = time.monotonic()
        expired = [
            cid for cid, state in self._states.items()
            if now - state.last_accessed > self.TTL_SECONDS
        ]
        for cid in expired:
            del self._states[cid]
```

### 8.3 Multi-Turn Flow

```
Turn 1: "What's the temperature?"
  conversation_id = new ULID
  state.messages = []
  → messages: [user: "What's the temperature?"]
  → Claude API call → tool call → response
  → messages: [user, assistant, user(tool_result), assistant(final)]
  → state saved under conversation_id

Turn 2: "Turn it up 2 degrees" (within 5 minutes, same conversation_id)
  state = lookup by conversation_id
  → messages: [user, assistant, user(tool_result), assistant(final),
               user: "Turn it up 2 degrees"]
  → Claude has full context of previous turn
  → Claude API call → tool call → response
```

### 8.4 Conversation ID Lifecycle

- HA generates a ULID for new conversations (`conversation_id=None` → `ulid_now()`)
- Same ULID is reused for multi-turn within 5-minute session
- After 5 minutes of inactivity, HA generates a new ULID (stale session)
- Our ConversationState also has a 5-minute TTL to match

### 8.5 `continue_conversation` Behavior

We do NOT override `continue_conversation`. It is automatically determined by
`chat_log.continue_conversation`:

```python
# Returns True if last assistant message ends with "?", ";", or "？"
chat_log.continue_conversation
```

This is read by `async_get_result_from_chat_log` and set on the `ConversationResult`.
When True, the voice pipeline keeps the mic open for the next turn. Claude can
naturally trigger this by asking follow-up questions.

---

## 9. Error Handling Strategy

### 9.1 Error Categories and Handling

| Error | Where | Handling |
|-------|-------|----------|
| Invalid API key | `async_setup_entry` | `raise ConfigEntryAuthFailed` → triggers reauth flow |
| API unreachable | `async_setup_entry` | `raise ConfigEntryNotReady` → HA retries |
| MCP connect failure | `async_added_to_hass` | Log warning, continue without MCP. Agent responds with "MCP servers unavailable" |
| Claude API error during conversation | `_async_handle_message` | `raise HomeAssistantError(msg)` → caught by HA, shown as error |
| MCP tool execution error | `run_agent_loop` | Return error string to Claude. Claude recovers gracefully |
| MCP connection drop during conversation | `call_tool` | Attempt reconnect. If fails, return error to Claude |
| Streaming error | `run_agent_loop` | Generator raises exception. `async_add_delta_content_stream` propagates it |
| Tool timeout | `call_tool` | 30-second timeout. Return timeout error to Claude |
| Max iterations reached | `run_agent_loop` | Generator ends. Whatever text has been yielded so far is the response |

### 9.2 Error Patterns

**API errors in _async_handle_message:**

```python
try:
    async for _ in chat_log.async_add_delta_content_stream(...):
        pass
except anthropic.AuthenticationError as err:
    # Trigger reauth flow for next attempt
    self.entry.async_start_reauth(self.hass)
    raise HomeAssistantError(
        "Authentication failed. Please update your API key."
    ) from err
except anthropic.AnthropicError as err:
    raise HomeAssistantError(
        f"Error communicating with Claude: {err}"
    ) from err
```

**Tool errors (returned to Claude, not raised):**

```python
# In MCPManager.call_tool:
try:
    result = await conn.session.call_tool(tool_name, arguments)
    return format_result(result)
except Exception as err:
    # Return error as tool result — Claude will handle gracefully
    return json.dumps({
        "error": type(err).__name__,
        "error_text": str(err),
    })
```

### 9.3 Graceful Degradation

- **No MCP servers configured:** Claude responds based on its knowledge alone. No tool
  calls are available.
- **MCP server down:** Claude receives tool errors and adapts its response
  ("I'm unable to check that right now, but...").
- **Slow tool call:** No explicit timeout on `async_converse()` from HA's side. The
  user hears silence (or pre-tool text if streaming started). Our tool timeout (30s)
  prevents infinite hangs.

---

## 10. Interface Contracts

### 10.1 ConversationEntity Contract

Our entity implements:

```python
class ClaudeConversationEntity(
    conversation.ConversationEntity,         # HA entity base
    conversation.AbstractConversationAgent,  # Agent interface
    ClaudeBaseLLMEntity,                     # Our base with config
):
    _attr_supports_streaming = True
    _attr_supported_features = ConversationEntityFeature.CONTROL

    @property
    def supported_languages(self) -> Literal["*"]:
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult: ...
```

### 10.2 Agent Loop Contract

```python
async def run_agent_loop(
    client: anthropic.AsyncAnthropic,
    mcp_manager: MCPManager,
    system_prompt: str,
    user_text: str,
    conversation_state: ConversationState,
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncGenerator[AssistantContentDeltaDict]:
    """
    Yields: AssistantContentDeltaDict dicts with keys:
      - {"role": "assistant"} — start of new assistant message
      - {"content": "text chunk"} — text delta for TTS

    Never yields: tool_calls, thinking_content, native

    Side effects:
      - Modifies conversation_state.messages in place
      - Calls mcp_manager.call_tool() for tool execution

    Terminates when:
      - Claude returns stop_reason == "end_turn"
      - MAX_TOOL_ITERATIONS (10) reached
      - An exception occurs (propagated to caller)
    """
```

### 10.3 MCPManager Contract

```python
class MCPManager:
    async def connect(self, name: str, url: str, token: str | None = None) -> None:
        """Connect to an MCP server. Discovers tools automatically."""

    async def disconnect(self, name: str) -> None:
        """Disconnect from a specific MCP server."""

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers. Called on entity removal."""

    async def refresh_tools(self, name: str) -> None:
        """Re-discover tools from a connected server."""

    def get_claude_tools(self) -> list[dict]:
        """Get all tools formatted for Claude API. Returns list of
        {"name": "server__tool", "description": ..., "input_schema": ...}"""

    async def call_tool(self, namespaced_name: str, arguments: dict) -> str:
        """Execute a tool. Returns result as string.
        On error, returns JSON with 'error' key."""
```

### 10.4 ConversationState Contract

```python
@dataclass
class ConversationState:
    messages: list[dict]    # Anthropic Messages API format
    created: float          # monotonic timestamp
    last_accessed: float    # monotonic timestamp
```

Messages format follows the Anthropic API:
```python
[
    {"role": "user", "content": "Turn on the lights"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "I'll turn on the lights for you."},
        {"type": "tool_use", "id": "toolu_xxx", "name": "ha__HassTurnOn",
         "input": {"entity_id": "light.living_room"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_xxx",
         "content": "OK"},
    ]},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Done! The living room lights are now on."},
    ]},
]
```

### 10.5 Delta Format Reference

Deltas yielded by our agent loop and consumed by `chat_log.async_add_delta_content_stream`:

```python
class AssistantContentDeltaDict(TypedDict, total=False):
    role: Literal["assistant"]   # Present = new message starts
    content: str | None          # Text chunk (concatenated across deltas)
    thinking_content: str | None # NOT USED in v1
    tool_calls: list[...]        # NOT USED (we handle tools ourselves)
    native: Any                  # NOT USED in v1
```

---

## 11. Open Questions

### 11.1 Multiple MCP Servers

**Question:** Should v1 support multiple MCP servers per agent, or just one?

**Recommendation:** Start with one MCP server per subentry (simplest config flow).
The `MCPManager` class already supports multiple connections, so expanding to multiple
servers in the config is a future enhancement.

### 11.2 Custom LLM API Wrapper

**Question:** Should we create a custom `llm.API` that wraps MCP tools and pass it
to `async_provide_llm_data`? This would enable the pipeline's tool_call streaming
trigger and put tool history in the ChatLog.

**Recommendation:** Defer to v2. The current approach (handling tools ourselves) is
simpler and works correctly. The only downside is the 60-char streaming threshold
for short pre-tool text, which is acceptable.

### 11.3 Extended Thinking

**Question:** Should we support Claude's extended thinking feature?

**Recommendation:** Defer to v2. When supported, add `thinking_budget` to config,
pass `thinking` parameter to API, and yield `{"thinking_content": ...}` deltas.
Note: the Agent SDK research found that streaming is NOT emitted during extended
thinking, so this may require special handling.

### 11.4 Token Usage Tracking

**Question:** Should we track and expose token usage/cost?

**Recommendation:** Log token usage at debug level. Consider adding a sensor entity
in v2 for usage tracking.

### 11.5 System Prompt Template Variables

**Question:** Should the system prompt support Jinja2 template variables (like the
Anthropic integration does)?

**Recommendation:** Yes. Use `TemplateSelector` in the config flow and let HA's
template engine render it. The `async_provide_llm_data` method handles this when
we pass the prompt as `user_llm_prompt`.

### 11.6 Reauth Flow

**Question:** Should we implement reauth for expired API keys?

**Recommendation:** Yes, for v1. Follow the Anthropic integration pattern:
- `async_step_reauth` + `async_step_reauth_confirm` in config flow
- `self.entry.async_start_reauth(self.hass)` on `AuthenticationError` during conversation

### 11.7 Rate Limiting

**Question:** How should we handle Anthropic API rate limits?

**Recommendation:** For v1, surface rate limit errors to the user via
`HomeAssistantError`. For v2, consider exponential backoff with retry.

### 11.8 MCP Server Health Monitoring

**Question:** Should we proactively monitor MCP server health?

**Recommendation:** For v1, reconnect on failure (reactive). For v2, consider
periodic health checks and HA repair/notification on persistent failure.

### 11.9 Message History Pruning

**Question:** Should we prune old messages to stay within context limits?

**Recommendation:** For v1, keep all messages within a conversation session (max 5
minutes). Claude's context window (200k tokens for Sonnet) is large enough for typical
voice conversations. For v2, implement token counting and pruning of old turns.

### 11.10 Stdio MCP Servers

**Question:** Should we support stdio (subprocess) MCP servers?

**Recommendation:** Defer to v2. HTTP/SSE covers the most common use cases (HA's own
server, remote services). Stdio adds subprocess management complexity.

---

## Appendix A: Dependency Matrix

| Package | Version | Already in HA Core | Purpose |
|---------|---------|-------------------|---------|
| `anthropic` | ==0.78.0 | Yes (anthropic component) | Claude API client |
| `mcp` | >=1.26.0 | Yes (mcp component) | MCP protocol client |
| `httpx` | (HA bundled) | Yes | HTTP client (transitive) |

Both required packages are already available in HA Core's environment. Our
`manifest.json` should pin the same versions to avoid conflicts.

## Appendix B: Constants Reference

```python
DOMAIN = "claude_conversation_agent"

# Config keys - main entry
CONF_API_KEY = "api_key"  # from homeassistant.const

# Config keys - subentry
CONF_RECOMMENDED = "recommended"
CONF_PROMPT = "prompt"
CONF_CHAT_MODEL = "chat_model"
CONF_MAX_TOKENS = "max_tokens"
CONF_TEMPERATURE = "temperature"
CONF_MCP_SERVER_URL = "mcp_server_url"
CONF_MCP_SERVER_TOKEN = "mcp_server_token"

# Defaults
DEFAULT_CHAT_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 1.0

# Limits
MAX_TOOL_ITERATIONS = 10
MCP_TOOL_TIMEOUT = 30.0  # seconds
MCP_CONNECT_TIMEOUT = 10.0  # seconds
CONVERSATION_STATE_TTL = 300  # 5 minutes
MCP_REFRESH_INTERVAL = 1800  # 30 minutes
```

## Appendix C: Test Plan Outline

| Test Area | What to Test |
|-----------|-------------|
| Config flow | API key validation, subentry creation, reauth |
| Entity lifecycle | Setup, MCP connect, teardown, MCP disconnect |
| Simple conversation | Text in → Claude API → text out |
| Tool use | Text in → Claude tool_use → MCP call → Claude response → text out |
| Multi-step tool use | Multiple sequential tool calls in one turn |
| Streaming | Verify deltas flow through chat_log to delta_listener |
| Multi-turn | Same conversation_id reuses state, different ID creates new state |
| Error: API failure | HomeAssistantError raised and caught by HA |
| Error: MCP failure | Error returned to Claude, graceful response |
| Error: tool timeout | Timeout error returned to Claude |
| State cleanup | Conversation state expires after 5 minutes |
| MCP reconnect | Connection drop → automatic reconnect on next call |
