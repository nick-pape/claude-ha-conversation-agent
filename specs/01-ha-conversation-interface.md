# Home Assistant Conversation Agent Interface -- Complete Contract

> Generated from HA Core `dev` branch source code, fetched 2026-02-17.
> Source files analyzed:
> - `homeassistant/components/conversation/entity.py`
> - `homeassistant/components/conversation/chat_log.py`
> - `homeassistant/components/conversation/models.py`
> - `homeassistant/components/conversation/__init__.py`
> - `homeassistant/components/conversation/agent_manager.py`
> - `homeassistant/components/conversation/util.py`
> - `homeassistant/components/conversation/const.py`
> - `homeassistant/helpers/chat_session.py`
> - `homeassistant/components/anthropic/conversation.py`
> - `homeassistant/components/anthropic/entity.py`
> - `homeassistant/components/anthropic/__init__.py`
> - `homeassistant/components/anthropic/config_flow.py`
> - `homeassistant/components/anthropic/const.py`

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [ConversationEntity Base Class](#2-conversationentity-base-class)
3. [ConversationInput and ConversationResult](#3-conversationinput-and-conversationresult)
4. [ChatLog -- The Central Conversation State](#4-chatlog--the-central-conversation-state)
5. [Content Types](#5-content-types)
6. [Streaming Deltas](#6-streaming-deltas)
7. [ChatLog Event Subscription System](#7-chatlog-event-subscription-system)
8. [Tool Call Lifecycle](#8-tool-call-lifecycle)
9. [continue_conversation and Voice Pipeline](#9-continue_conversation-and-voice-pipeline)
10. [Error Handling](#10-error-handling)
11. [Conversation ID and Session Management](#11-conversation-id-and-session-management)
12. [Service Registration and Pipeline Dispatch](#12-service-registration-and-pipeline-dispatch)
13. [Reference Implementation: Anthropic Integration](#13-reference-implementation-anthropic-integration)
14. [Config Flow Patterns](#14-config-flow-patterns)
15. [Setup and Teardown Patterns](#15-setup-and-teardown-patterns)

---

## 1. Architecture Overview

The conversation system in Home Assistant follows this dispatch chain:

```
User Input (voice/text/service call)
    |
    v
conversation.async_converse()          # agent_manager.py
    |
    v
async_get_agent(hass, agent_id)        # Resolves to ConversationEntity or AbstractConversationAgent
    |
    v
entity.internal_async_process()        # Updates last_activity timestamp
    |
    v
entity.async_process()                 # Opens ChatSession + ChatLog context managers
    |
    v
entity._async_handle_message()         # YOUR CODE -- the method to override
    |
    v
chat_log.async_provide_llm_data()      # Sets system prompt, LLM tools, etc.
    |
    v
[Call your LLM API, stream results through chat_log]
    |
    v
async_get_result_from_chat_log()       # util.py -- extracts ConversationResult from ChatLog
```

Key abstraction layers:
- **ChatSession** (`homeassistant/helpers/chat_session.py`): Manages conversation_id lifecycle, session expiry (5-minute timeout), ULID generation.
- **ChatLog** (`conversation/chat_log.py`): Stores full message history, handles streaming deltas, dispatches tool calls, notifies subscribers.
- **ConversationEntity** (`conversation/entity.py`): Base class for conversation agent entities. Extends `RestoreEntity`.

---

## 2. ConversationEntity Base Class

**File:** `homeassistant/components/conversation/entity.py`

```python
class ConversationEntity(RestoreEntity):
    """Entity that supports conversations."""

    _attr_should_poll = False
    _attr_supported_features = ConversationEntityFeature(0)
    _attr_supports_streaming = False
    __last_activity: str | None = None
```

### Properties

| Property | Type | Description |
|---|---|---|
| `supports_streaming` | `bool` | Whether the agent supports streaming responses. Set via `_attr_supports_streaming`. |
| `state` | `str \| None` | ISO timestamp of the last activity. Marked `@final` -- cannot be overridden. |
| `supported_languages` | `list[str] \| Literal["*"]` | **Abstract property -- MUST override.** Return `MATCH_ALL` for all languages. |

### Methods to Override

#### `_async_handle_message` (the primary method to implement)

```python
async def _async_handle_message(
    self,
    user_input: ConversationInput,
    chat_log: ChatLog,
) -> ConversationResult:
    """Call the API."""
    raise NotImplementedError
```

This is the **only method you must implement** (along with the `supported_languages` property). The base class default raises `NotImplementedError`.

#### `async_prepare` (optional)

```python
async def async_prepare(self, language: str | None = None) -> None:
    """Load intents for a language."""
```

Called before conversation processing if the pipeline wants to warm up the agent. Default is a no-op.

### Lifecycle of a Conversation Turn

The full call chain for a single turn is:

```python
# 1. Framework calls internal_async_process (marked @final)
@final
async def internal_async_process(self, user_input: ConversationInput) -> ConversationResult:
    self.__last_activity = dt_util.utcnow().isoformat()
    self.async_write_ha_state()
    return await self.async_process(user_input)

# 2. async_process opens session + chat_log context managers
async def async_process(self, user_input: ConversationInput) -> ConversationResult:
    with (
        async_get_chat_session(self.hass, user_input.conversation_id) as session,
        async_get_chat_log(self.hass, session, user_input) as chat_log,
    ):
        return await self._async_handle_message(user_input, chat_log)

# 3. Your implementation of _async_handle_message runs
#    - Call chat_log.async_provide_llm_data(...)
#    - Call your LLM
#    - Stream results through chat_log.async_add_delta_content_stream(...)
#    - Return async_get_result_from_chat_log(user_input, chat_log)
```

**Important:** You typically do NOT override `async_process` -- just `_async_handle_message`. The base class handles session/chatlog setup.

### ConversationEntityFeature

```python
class ConversationEntityFeature(IntFlag):
    CONTROL = 1  # Set this when LLM has access to HA control APIs
```

---

## 3. ConversationInput and ConversationResult

**File:** `homeassistant/components/conversation/models.py`

### ConversationInput

```python
@dataclass(slots=True)
class ConversationInput:
    text: str                              # User spoken/typed text
    context: Context                       # HA request context (user_id, etc.)
    conversation_id: str | None            # Unique conversation identifier (ULID or custom)
    device_id: str | None                  # Device that initiated the conversation
    satellite_id: str | None               # Voice satellite identifier
    language: str                          # Language of the request
    agent_id: str                          # Agent entity_id to use for processing
    extra_system_prompt: str | None = None # Extra prompt for LLMs (e.g., from voice pipeline)

    def as_llm_context(self, conversing_domain: str) -> llm.LLMContext:
        """Return input as an LLM context."""
        return llm.LLMContext(
            platform=conversing_domain,
            context=self.context,
            language=self.language,
            assistant=DOMAIN,
            device_id=self.device_id,
        )
```

### ConversationResult

```python
@dataclass(slots=True)
class ConversationResult:
    response: intent.IntentResponse     # The response to send back
    conversation_id: str | None = None  # Must be set for multi-turn
    continue_conversation: bool = False # If True, voice pipeline keeps mic open
```

### AbstractConversationAgent

```python
class AbstractConversationAgent(ABC):
    @property
    @abstractmethod
    def supported_languages(self) -> list[str] | Literal["*"]: ...

    @abstractmethod
    async def async_process(self, user_input: ConversationInput) -> ConversationResult: ...

    async def async_reload(self, language: str | None = None) -> None: ...
    async def async_prepare(self, language: str | None = None) -> None: ...
```

Note: `ConversationEntity` (the entity-based approach) is the **modern** way. `AbstractConversationAgent` is the older non-entity approach (still supported but not preferred).

---

## 4. ChatLog -- The Central Conversation State

**File:** `homeassistant/components/conversation/chat_log.py`

```python
@dataclass
class ChatLog:
    hass: HomeAssistant
    conversation_id: str
    content: list[Content] = field(default_factory=lambda: [SystemContent(content="")])
    extra_system_prompt: str | None = None
    llm_api: llm.APIInstance | None = None
    delta_listener: Callable[[ChatLog, dict], None] | None = None
    llm_input_provided_index = 0
    created: datetime = field(init=False, default_factory=utcnow)
```

### Key Properties

| Property | Type | Description |
|---|---|---|
| `content` | `list[Content]` | Full message history. Starts with `[SystemContent(content="")]`. |
| `conversation_id` | `str` | The conversation identifier. |
| `llm_api` | `llm.APIInstance \| None` | Set by `async_provide_llm_data`. Provides tools and tool execution. |
| `delta_listener` | `Callable[[ChatLog, dict], None] \| None` | Streaming callback, set by the pipeline for real-time streaming. |
| `llm_input_provided_index` | `int` | Index in `content` where `async_provide_llm_data` was called. Used by `async_get_result_from_chat_log` to find tool results. |
| `continue_conversation` | `bool` (property) | Returns `True` if the last assistant message ends with `?`, `;` (Greek), or `?` (Chinese). |
| `unresponded_tool_results` | `bool` (property) | Returns `True` if the last content item has role `"tool_result"`. Used to determine if another LLM iteration is needed. |

### Key Methods

#### `async_provide_llm_data`

```python
async def async_provide_llm_data(
    self,
    llm_context: llm.LLMContext,
    user_llm_hass_api: str | list[str] | llm.API | None = None,
    user_llm_prompt: str | None = None,
    user_extra_system_prompt: str | None = None,
) -> None:
```

This method:
1. Resolves the LLM API instance (provides tools like `HassTurnOn`, `HassTurnOff`, etc.)
2. Renders the system prompt template (supports Jinja2 with `ha_name`, `user_name`, `llm_context`)
3. Appends the API prompt (tool descriptions), date/time info, and extra system prompt
4. Sets `self.content[0]` to the final `SystemContent`
5. Sets `self.llm_input_provided_index` to current content length
6. Fires a `ChatLogEventType.UPDATED` subscriber notification

#### `async_add_delta_content_stream`

```python
async def async_add_delta_content_stream(
    self,
    agent_id: str,
    stream: AsyncIterable[AssistantContentDeltaDict | ToolResultContentDeltaDict],
) -> AsyncGenerator[AssistantContent | ToolResultContent]:
```

This is the **primary streaming interface**. It:
1. Consumes an `AsyncIterable` of delta dicts from your LLM
2. Accumulates deltas into complete `AssistantContent` messages
3. When a new `role` key appears in a delta, the previous message is finalized
4. Automatically dispatches tool calls (via `self.llm_api.async_call_tool`) as soon as they appear
5. Yields back each completed `AssistantContent` or `ToolResultContent`
6. Calls `delta_listener` for each delta (used for real-time streaming to voice pipeline/TTS)
7. Filters out `native` key from deltas sent to `delta_listener` (not JSON serializable)

**The streaming protocol:**
- A delta without a `role` key is an **update** to the current message
- A delta with `role: "assistant"` starts a **new assistant message** (finalizing the previous one)
- A delta with `role: "tool_result"` adds a **tool result** (from external tools like web search)
- `content` strings are concatenated
- `thinking_content` strings are concatenated
- `tool_calls` lists are appended
- `native` must be set only once per message

#### `async_add_assistant_content`

```python
async def async_add_assistant_content(
    self,
    content: AssistantContent | ToolResultContent,
    /,
    tool_call_tasks: dict[str, asyncio.Task] | None = None,
) -> AsyncGenerator[ToolResultContent]:
```

Lower-level method that adds content and executes tool calls. Yields `ToolResultContent` for each tool call result. Used internally by `async_add_delta_content_stream`.

#### `async_add_user_content`

```python
@callback
def async_add_user_content(self, content: UserContent) -> None:
```

Appends user content and fires `CONTENT_ADDED` event.

#### `async_add_assistant_content_without_tools`

```python
@callback
def async_add_assistant_content_without_tools(
    self, content: AssistantContent | ToolResultContent
) -> None:
```

Adds assistant content that has no non-external tool calls. Used for external tool results (like web search).

---

## 5. Content Types

All content types are frozen dataclasses in `chat_log.py`.

### SystemContent

```python
@dataclass(frozen=True)
class SystemContent:
    role: Literal["system"] = field(init=False, default="system")
    content: str
    created: datetime = field(init=False, default_factory=utcnow)
```

### UserContent

```python
@dataclass(frozen=True)
class UserContent:
    role: Literal["user"] = field(init=False, default="user")
    content: str
    attachments: list[Attachment] | None = field(default=None)
    created: datetime = field(init=False, default_factory=utcnow)
```

### AssistantContent

```python
@dataclass(frozen=True)
class AssistantContent:
    role: Literal["assistant"] = field(init=False, default="assistant")
    agent_id: str                              # Your entity_id
    content: str | None = None                 # Text response
    thinking_content: str | None = None        # Extended thinking (if supported)
    tool_calls: list[llm.ToolInput] | None = None  # Tool calls made
    native: Any = None                         # Provider-specific data (e.g., ThinkingBlock)
    created: datetime = field(init=False, default_factory=utcnow)
```

### ToolResultContent

```python
@dataclass(frozen=True)
class ToolResultContent:
    role: Literal["tool_result"] = field(init=False, default="tool_result")
    agent_id: str
    tool_call_id: str
    tool_name: str
    tool_result: JsonObjectType
    created: datetime = field(init=False, default_factory=utcnow)
```

### Attachment

```python
@dataclass(frozen=True)
class Attachment:
    media_content_id: str
    mime_type: str
    path: Path
```

### Content Union Type

```python
type Content = SystemContent | UserContent | AssistantContent | ToolResultContent
```

---

## 6. Streaming Deltas

### AssistantContentDeltaDict

```python
class AssistantContentDeltaDict(TypedDict, total=False):
    role: Literal["assistant"]        # Present = start new message
    content: str | None               # Text chunk (concatenated)
    thinking_content: str | None      # Thinking chunk (concatenated)
    tool_calls: list[llm.ToolInput] | None  # Tool calls (appended)
    native: Any                       # Provider-specific (set once per message)
```

**Key rules:**
- If `role` is absent, the delta is an **update** to the current in-progress message.
- If `role` is present, a **new message** is started (and the previous one is finalized and yielded).
- `content` strings are concatenated across multiple deltas.
- `thinking_content` strings are concatenated across multiple deltas.
- `tool_calls` lists are appended (not replaced).
- `native` may only be set once per message (raises `RuntimeError` if set twice).
- The `native` key is **filtered out** before being passed to `delta_listener` (not JSON serializable).

### ToolResultContentDeltaDict

```python
class ToolResultContentDeltaDict(TypedDict, total=False):
    role: Literal["tool_result"]      # Always present
    tool_call_id: str
    tool_name: str
    tool_result: JsonObjectType
```

Used for external tool results (e.g., web search results that come from the API itself, not from HA tools).

### How Streaming Flows to Voice Pipeline / TTS

The voice pipeline attaches a `delta_listener` callback via the `chat_log_delta_listener` parameter of `async_get_chat_log`:

```python
@contextmanager
def async_get_chat_log(
    hass: HomeAssistant,
    session: chat_session.ChatSession,
    user_input: ConversationInput | None = None,
    *,
    chat_log_delta_listener: Callable[[ChatLog, dict], None] | None = None,
) -> Generator[ChatLog]:
```

Inside `async_add_delta_content_stream`, for each delta:

```python
if self.delta_listener:
    if filtered_delta := {
        k: v for k, v in assistant_delta.items() if k != "native"
    }:
        self.delta_listener(self, filtered_delta)
```

The delta_listener receives filtered dicts (no `native` key). The voice pipeline uses these `content` deltas to progressively feed text to the TTS engine for low-latency speech synthesis.

The `delta_listener` is set only by the **initial caller** of `async_get_chat_log`. If a nested context tries to attach one, it raises `RuntimeError("Cannot attach chat log delta listener unless initial caller")`.

---

## 7. ChatLog Event Subscription System

### Event Types

```python
class ChatLogEventType(StrEnum):
    INITIAL_STATE = "initial_state"
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    CONTENT_ADDED = "content_added"
```

### Subscribing

```python
@callback
def async_subscribe_chat_logs(
    hass: HomeAssistant,
    callback_func: Callable[[str, ChatLogEventType, dict[str, Any]], None],
) -> Callable[[], None]:
```

The callback receives `(conversation_id, event_type, data)`. Returns an unsubscribe function.

### Event Flow

1. **New conversation log created:** `CREATED` with `{"chat_log": chat_log.as_dict()}`
2. **System prompt set (via `async_provide_llm_data`):** `UPDATED` with `{"chat_log": chat_log.as_dict()}`
3. **User message added:** `CONTENT_ADDED` with `{"content": content.as_dict()}`
4. **Tool result added:** `CONTENT_ADDED` with `{"content": content.as_dict()}`
5. **Context manager exits (existing log):** `UPDATED` with full chat_log
6. **Session cleanup:** `DELETED` with `{}`

### Notification Function

```python
@callback
def _async_notify_subscribers(
    hass: HomeAssistant,
    conversation_id: str,
    event_type: ChatLogEventType,
    data: dict[str, Any],
) -> None:
```

---

## 8. Tool Call Lifecycle

### How Tools Are Provided

1. Integration config specifies `CONF_LLM_HASS_API` (e.g., `["llm.assist"]`)
2. `chat_log.async_provide_llm_data()` resolves this to an `llm.APIInstance`
3. `chat_log.llm_api.tools` provides the list of `llm.Tool` objects
4. Your integration formats these tools for your LLM provider

### Tool Execution Flow

When `async_add_delta_content_stream` encounters tool calls in deltas:

```python
# Tool calls are started EAGERLY as soon as they appear in the stream
if delta_tool_calls := assistant_delta.get("tool_calls"):
    current_tool_calls += delta_tool_calls
    for tool_call in delta_tool_calls:
        if not tool_call.external:
            tool_call_tasks[tool_call.id] = self.hass.async_create_task(
                self.llm_api.async_call_tool(tool_call),
                name=f"llm_tool_{tool_call.id}",
            )
```

When the message is finalized, tool results are awaited:

```python
async for tool_result in self.async_add_assistant_content(content, tool_call_tasks=tool_call_tasks):
    yield tool_result
```

### External vs Internal Tools

- **Internal tools** (`tool_call.external = False`): Executed by HA's LLM API (e.g., `HassTurnOn`). Results are added to the chat log automatically.
- **External tools** (`tool_call.external = True`): Managed by the LLM provider itself (e.g., Anthropic's web search). Results come back through the stream as `ToolResultContentDeltaDict`.

### Tool Error Handling

```python
try:
    tool_result = await tool_call_tasks[tool_input.id]
except (HomeAssistantError, vol.Invalid) as e:
    tool_result = {"error": type(e).__name__}
    if str(e):
        tool_result["error_text"] = str(e)
```

Tool errors are returned to the LLM as structured error objects, not raised as exceptions.

### Multi-Turn Tool Loop

The Anthropic integration demonstrates the standard pattern for iterating:

```python
for _iteration in range(MAX_TOOL_ITERATIONS):  # MAX_TOOL_ITERATIONS = 10
    stream = await client.messages.create(**model_args)

    messages.extend(
        _convert_content([
            content
            async for content in chat_log.async_add_delta_content_stream(
                self.entity_id,
                _transform_stream(chat_log, stream),
            )
        ])
    )

    if not chat_log.unresponded_tool_results:
        break  # No more tool calls pending -- done
```

---

## 9. continue_conversation and Voice Pipeline

### How It Is Determined

```python
@property
def continue_conversation(self) -> bool:
    if not self.content:
        return False
    last_msg = self.content[-1]
    return (
        last_msg.role == "assistant"
        and last_msg.content is not None
        and last_msg.content.strip().endswith((
            "?",     # Latin question mark
            ";",     # Greek question mark (U+037E)
            "\uff1f",  # Chinese/fullwidth question mark
        ))
    )
```

### How It Flows Through

`async_get_result_from_chat_log` (in `util.py`) reads this property:

```python
@callback
def async_get_result_from_chat_log(
    user_input: ConversationInput, chat_log: ChatLog
) -> ConversationResult:
    # ... extracts tool results and last assistant content ...

    return ConversationResult(
        response=intent_response,
        conversation_id=chat_log.conversation_id,
        continue_conversation=chat_log.continue_conversation,  # <-- HERE
    )
```

### What the Pipeline Does With It

When `continue_conversation=True` in the `ConversationResult`:
- The voice pipeline keeps the microphone open after TTS playback
- The user can continue speaking without a wake word
- The same `conversation_id` is passed back for the next turn

This means if your LLM's response ends with a question mark, the voice pipeline will automatically keep the conversation going.

---

## 10. Error Handling

### ConverseError

```python
class ConverseError(HomeAssistantError):
    def __init__(
        self, message: str, conversation_id: str, response: intent.IntentResponse
    ) -> None:
        super().__init__(message)
        self.conversation_id = conversation_id
        self.response = response

    def as_conversation_result(self) -> ConversationResult:
        return ConversationResult(
            response=self.response,
            conversation_id=self.conversation_id,
        )
```

**Pattern for handling ConverseError** (from the Anthropic integration):

```python
try:
    await chat_log.async_provide_llm_data(
        user_input.as_llm_context(DOMAIN),
        options.get(CONF_LLM_HASS_API),
        options.get(CONF_PROMPT),
        user_input.extra_system_prompt,
    )
except conversation.ConverseError as err:
    return err.as_conversation_result()
```

### HomeAssistantError in async_converse

The `async_converse` function in `agent_manager.py` catches `HomeAssistantError`:

```python
try:
    result = await method(conversation_input)
except HomeAssistantError as err:
    intent_response = intent.IntentResponse(language=language)
    intent_response.async_set_error(
        intent.IntentResponseErrorCode.UNKNOWN,
        str(err),
    )
    result = ConversationResult(
        response=intent_response,
        conversation_id=conversation_id,
    )
```

So raising `HomeAssistantError` from `_async_handle_message` is safe -- it will be caught and converted to an error response. The Anthropic integration raises it for API errors:

```python
except anthropic.AnthropicError as err:
    raise HomeAssistantError(
        f"Sorry, I had a problem talking to Anthropic: {err}"
    ) from err
```

### Error in async_get_result_from_chat_log

If the last content in the chat log is not an `AssistantContent`:

```python
if not isinstance((last_content := chat_log.content[-1]), AssistantContent):
    _LOGGER.error(
        "Last content in chat log is not an AssistantContent: %s. "
        "This could be due to the model not returning a valid response",
        last_content,
    )
    raise HomeAssistantError("Unable to get response")
```

---

## 11. Conversation ID and Session Management

**File:** `homeassistant/helpers/chat_session.py`

### ChatSession

```python
@dataclass
class ChatSession:
    conversation_id: str
    last_updated: datetime = field(default_factory=dt_util.utcnow)
    _cleanup_callbacks: list[CALLBACK_TYPE] = field(default_factory=list)
```

### Session ID Rules (from `async_get_chat_session`)

1. If `conversation_id` is `None`: A new ULID is generated (`ulid_now()`).
2. If `conversation_id` exists in active sessions: That session is returned.
3. If `conversation_id` is a valid ULID but not in active sessions: A **new** ULID is generated (stale session = new conversation).
4. If `conversation_id` is NOT a valid ULID: It is kept as-is (custom ID -- user wants to track it).

### Session Timeout

```python
CONVERSATION_TIMEOUT = timedelta(minutes=5)
```

Sessions expire after 5 minutes of inactivity. On expiry:
1. The session is removed from `DATA_CHAT_SESSION`
2. `session.async_cleanup()` is called, which triggers all registered cleanup callbacks
3. The ChatLog cleanup callback removes the chat log from `DATA_CHAT_LOGS` and fires `ChatLogEventType.DELETED`

### Context Variable Nesting

Both `ChatSession` and `ChatLog` use `ContextVar` to support nesting (e.g., a conversation agent calling a tool that talks to another LLM):

```python
current_session: ContextVar[ChatSession | None] = ContextVar("current_session", default=None)
current_chat_log: ContextVar[ChatLog | None] = ContextVar("current_chat_log", default=None)
```

If a nested call has the same `conversation_id`, the existing session/chat_log is reused.

---

## 12. Service Registration and Pipeline Dispatch

**File:** `homeassistant/components/conversation/__init__.py`

### Service Registration

```python
async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    entity_component = EntityComponent[ConversationEntity](_LOGGER, DOMAIN, hass)
    hass.data[DATA_COMPONENT] = entity_component

    # ... setup default agent, config intents ...

    hass.services.async_register(
        DOMAIN,
        SERVICE_PROCESS,           # "process"
        handle_process,
        schema=SERVICE_PROCESS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RELOAD, handle_reload, schema=SERVICE_RELOAD_SCHEMA
    )
    async_setup_conversation_http(hass)
    return True
```

### The `handle_process` Service Handler

```python
async def handle_process(service: ServiceCall) -> ServiceResponse:
    text = service.data[ATTR_TEXT]
    result = await async_converse(
        hass=hass,
        text=text,
        conversation_id=service.data.get(ATTR_CONVERSATION_ID),
        context=service.context,
        language=service.data.get(ATTR_LANGUAGE),
        agent_id=service.data.get(ATTR_AGENT_ID),
    )
    if service.return_response:
        return result.as_dict()
    return None
```

### Agent Resolution in `async_converse`

```python
async def async_converse(
    hass, text, conversation_id, context,
    language=None, agent_id=None, device_id=None,
    satellite_id=None, extra_system_prompt=None,
) -> ConversationResult:
    if agent_id is None:
        agent_id = HOME_ASSISTANT_AGENT  # "conversation.home_assistant"

    agent = async_get_agent(hass, agent_id)

    if isinstance(agent, ConversationEntity):
        agent.async_set_context(context)
        method = agent.internal_async_process
    else:
        method = agent.async_process
```

### Agent Lookup (`async_get_agent`)

```python
@callback
def async_get_agent(hass, agent_id=None):
    manager = get_agent_manager(hass)

    if agent_id is None or agent_id == HOME_ASSISTANT_AGENT:
        return manager.default_agent

    if "." in agent_id:  # Entity ID format: "conversation.my_agent"
        return hass.data[DATA_COMPONENT].get_entity(agent_id)

    # Otherwise look up in the agent manager (non-entity agents)
    return manager.async_get_agent(agent_id)
```

---

## 13. Reference Implementation: Anthropic Integration

### Conversation Entity (conversation.py)

```python
class AnthropicConversationEntity(
    conversation.ConversationEntity,        # Primary base
    conversation.AbstractConversationAgent, # Also implements agent interface
    AnthropicBaseLLMEntity,                # Shared base with AI Task entity
):
    _attr_supports_streaming = True

    def __init__(self, entry: AnthropicConfigEntry, subentry: ConfigSubentry) -> None:
        super().__init__(entry, subentry)
        if self.subentry.data.get(CONF_LLM_HASS_API):
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        options = self.subentry.data

        # Step 1: Set up LLM data (system prompt, tools)
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                options.get(CONF_LLM_HASS_API),
                options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        # Step 2: Call LLM with streaming + tool loop
        await self._async_handle_chat_log(chat_log)

        # Step 3: Extract result from chat log
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
```

### The Core LLM Loop (entity.py -- `_async_handle_chat_log`)

This is the heart of the Anthropic implementation. Simplified:

```python
async def _async_handle_chat_log(self, chat_log, ...):
    # 1. Extract system prompt
    system = chat_log.content[0]  # SystemContent
    system_prompt = [TextBlockParam(type="text", text=system.content,
                                     cache_control={"type": "ephemeral"})]

    # 2. Convert chat history to Anthropic format
    messages = _convert_content(chat_log.content[1:])

    # 3. Build model args
    model_args = MessageCreateParamsStreaming(
        model=model, messages=messages, max_tokens=max_tokens,
        system=system_prompt, stream=True,
    )

    # 4. Format HA tools for Anthropic
    tools = [_format_tool(tool, ...) for tool in chat_log.llm_api.tools]
    if tools:
        model_args["tools"] = tools

    # 5. Tool iteration loop
    for _iteration in range(MAX_TOOL_ITERATIONS):  # 10
        stream = await client.messages.create(**model_args)

        # Stream through chat_log, which handles tool execution
        messages.extend(
            _convert_content([
                content
                async for content in chat_log.async_add_delta_content_stream(
                    self.entity_id,
                    _transform_stream(chat_log, stream),
                )
            ])
        )

        # If no pending tool results, we're done
        if not chat_log.unresponded_tool_results:
            break
```

### The Stream Transformer (`_transform_stream`)

This generator converts Anthropic's raw `MessageStreamEvent` objects into HA's delta dict format:

```python
async def _transform_stream(
    chat_log: conversation.ChatLog,
    stream: AsyncStream[MessageStreamEvent],
    output_tool: str | None = None,
) -> AsyncGenerator[AssistantContentDeltaDict | ToolResultContentDeltaDict]:
```

Key mappings:
| Anthropic Event | HA Delta |
|---|---|
| `RawMessageStartEvent` | (validates role is "assistant") |
| `RawContentBlockStartEvent(TextBlock)` | `{"role": "assistant"}` (new message) |
| `RawContentBlockStartEvent(ThinkingBlock)` | `{"role": "assistant"}` (new message) |
| `RawContentBlockStartEvent(ToolUseBlock)` | (stores tool block state) |
| `RawContentBlockStartEvent(WebSearchToolResultBlock)` | `{"role": "tool_result", ...}` |
| `RawContentBlockDeltaEvent(TextDelta)` | `{"content": text}` |
| `RawContentBlockDeltaEvent(ThinkingDelta)` | `{"thinking_content": thinking}` |
| `RawContentBlockDeltaEvent(SignatureDelta)` | `{"native": ThinkingBlock(...)}` |
| `RawContentBlockDeltaEvent(InputJSONDelta)` | (accumulates tool args) |
| `RawContentBlockStopEvent` (for tool) | `{"tool_calls": [llm.ToolInput(...)]}` |
| `RawMessageDeltaEvent` | (token stats, refusal check) |

### Platform Setup (conversation.py)

```python
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AnthropicConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "conversation":
            continue
        async_add_entities(
            [AnthropicConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )
```

---

## 14. Config Flow Patterns

**File:** `homeassistant/components/anthropic/config_flow.py`

### Main Config Flow

```python
class AnthropicConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2
    MINOR_VERSION = 3

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            self._async_abort_entries_match(user_input)
            try:
                await validate_input(self.hass, user_input)
            except anthropic.APITimeoutError:
                errors["base"] = "timeout_connect"
            except anthropic.APIConnectionError:
                errors["base"] = "cannot_connect"
            except anthropic.APIStatusError as e:
                # ... check for authentication_error ...
            else:
                return self.async_create_entry(
                    title="Claude",
                    data=user_input,
                    subentries=[
                        {
                            "subentry_type": "conversation",
                            "data": DEFAULT_CONVERSATION_OPTIONS,
                            "title": DEFAULT_CONVERSATION_NAME,
                            "unique_id": None,
                        },
                        {
                            "subentry_type": "ai_task_data",
                            "data": DEFAULT_AI_TASK_OPTIONS,
                            "title": DEFAULT_AI_TASK_NAME,
                            "unique_id": None,
                        },
                    ],
                )
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors or None,
        )
```

### Key Pattern: Subentries

The Anthropic integration uses **config subentries** to separate:
- The parent config entry: stores `CONF_API_KEY`
- Conversation subentry: stores `CONF_PROMPT`, `CONF_LLM_HASS_API`, `CONF_CHAT_MODEL`, etc.
- AI Task subentry: stores task-specific options

This allows multiple conversation agents per API key.

### Subentry Flow

```python
@classmethod
@callback
def async_get_supported_subentry_types(cls, config_entry):
    return {
        "conversation": ConversationSubentryFlowHandler,
        "ai_task_data": ConversationSubentryFlowHandler,
    }
```

The `ConversationSubentryFlowHandler` implements a multi-step flow:
1. `async_step_init`: Basic options (prompt, LLM API selection, recommended toggle)
2. `async_step_advanced`: Model selection, max tokens, temperature
3. `async_step_model`: Model-specific options (thinking budget, web search)

### Default Options

```python
DEFAULT_CONVERSATION_OPTIONS = {
    CONF_RECOMMENDED: True,
    CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
    CONF_PROMPT: llm.DEFAULT_INSTRUCTIONS_PROMPT,
}
```

---

## 15. Setup and Teardown Patterns

**File:** `homeassistant/components/anthropic/__init__.py`

### Type Alias for Config Entry

```python
type AnthropicConfigEntry = ConfigEntry[anthropic.AsyncClient]
```

The `runtime_data` on the config entry stores the API client.

### Entry Setup

```python
PLATFORMS = (Platform.AI_TASK, Platform.CONVERSATION)

async def async_setup_entry(hass, entry: AnthropicConfigEntry) -> bool:
    # 1. Create API client
    client = anthropic.AsyncAnthropic(
        api_key=entry.data[CONF_API_KEY],
        http_client=get_async_client(hass)  # Reuse HA's httpx client
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

    # 4. Forward setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 5. Register update listener
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True
```

### Entry Teardown

```python
async def async_unload_entry(hass, entry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```

### Options Update Handler

```python
async def async_update_options(hass, entry) -> None:
    defer_reload_entries = hass.data.setdefault(DOMAIN, {}).setdefault(
        DATA_REPAIR_DEFER_RELOAD, set()
    )
    if entry.entry_id in defer_reload_entries:
        return
    await hass.config_entries.async_reload(entry.entry_id)
```

### Global Setup

```python
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)  # No YAML config

async def async_setup(hass, config) -> bool:
    hass.data.setdefault(DOMAIN, {}).setdefault(DATA_REPAIR_DEFER_RELOAD, set())
    await async_migrate_integration(hass)
    return True
```

---

## Appendix A: Minimal Custom Conversation Agent Template

Based on the Anthropic reference implementation, here is the minimal contract:

```python
"""conversation.py"""
from typing import Literal
from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL

class MyConversationEntity(conversation.ConversationEntity):
    _attr_supports_streaming = True  # or False

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        # 1. Set up LLM data
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context("my_domain"),
                user_llm_hass_api=["llm.assist"],  # or from config
                user_llm_prompt=None,               # or custom prompt
                user_extra_system_prompt=user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        # 2. Call your LLM and stream results
        # Option A: Streaming
        async for content in chat_log.async_add_delta_content_stream(
            self.entity_id,
            my_llm_stream_generator(chat_log),
        ):
            pass  # content is added to chat_log automatically

        # Option B: Non-streaming (use async_add_assistant_content)
        # chat_log.async_add_assistant_content_without_tools(
        #     conversation.AssistantContent(agent_id=self.entity_id, content="Hello!")
        # )

        # 3. Extract and return result
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
```

## Appendix B: Key Imports from `conversation` Package

The `__all__` list in `conversation/__init__.py`:

```python
__all__ = [
    "DOMAIN",
    "HOME_ASSISTANT_AGENT",
    "AssistantContent",
    "AssistantContentDeltaDict",
    "Attachment",
    "ChatLog",
    "Content",
    "ConversationEntity",
    "ConversationEntityFeature",
    "ConversationInput",
    "ConversationResult",
    "ConversationTraceEventType",
    "ConverseError",
    "SystemContent",
    "ToolResultContent",
    "ToolResultContentDeltaDict",
    "UserContent",
    "async_conversation_trace_append",
    "async_converse",
    "async_get_agent_info",
    "async_get_chat_log",
    "async_get_result_from_chat_log",
    "async_set_agent",
    "async_unset_agent",
]
```

## Appendix C: Anthropic Default Configuration Values

```python
DEFAULT = {
    CONF_CHAT_MODEL: "claude-haiku-4-5",
    CONF_MAX_TOKENS: 3000,
    CONF_TEMPERATURE: 1.0,
    CONF_THINKING_BUDGET: 0,
    CONF_THINKING_EFFORT: "low",
    CONF_WEB_SEARCH: False,
    CONF_WEB_SEARCH_USER_LOCATION: False,
    CONF_WEB_SEARCH_MAX_USES: 5,
}
MIN_THINKING_BUDGET = 1024
MAX_TOOL_ITERATIONS = 10
```
