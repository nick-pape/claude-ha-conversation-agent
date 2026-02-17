# 04 - Home Assistant Voice Pipeline Streaming Architecture

> Research spec: How HA's voice pipeline consumes streaming conversation agent output
> and routes it through TTS to satellite speakers.

---

## 1. Pipeline Overview

The voice pipeline lives in `homeassistant/components/assist_pipeline/pipeline.py`. It
sequences through ordered stages:

```
WAKE_WORD -> STT -> INTENT -> TTS -> END
```

Each stage is optional. A satellite typically runs `STT -> INTENT -> TTS`. The pipeline
can also be started at `INTENT` (text input) or `TTS` (pre-formed text).

### Key classes

| Class | Role |
|---|---|
| `Pipeline` | Frozen config: engine IDs, languages, voices |
| `PipelineRun` | Mutable state for one execution of a pipeline |
| `PipelineInput` | Input data (audio stream, text, metadata) |
| `PipelineEvent` | Typed event emitted at each stage transition |
| `PipelineConversationData` | Persists `continue_conversation_agent` across turns |

### Stage enum (`PipelineStage`)

```python
class PipelineStage(StrEnum):
    WAKE_WORD = "wake_word"
    STT = "stt"
    INTENT = "intent"
    TTS = "tts"
    END = "end"
```

---

## 2. Event System (`PipelineEventType`)

```python
class PipelineEventType(StrEnum):
    RUN_START = "run-start"
    RUN_END = "run-end"
    WAKE_WORD_START = "wake_word-start"
    WAKE_WORD_END = "wake_word-end"
    STT_START = "stt-start"
    STT_VAD_START = "stt-vad-start"
    STT_VAD_END = "stt-vad-end"
    STT_END = "stt-end"
    INTENT_START = "intent-start"
    INTENT_PROGRESS = "intent-progress"   # <-- streaming deltas
    INTENT_END = "intent-end"
    TTS_START = "tts-start"
    TTS_END = "tts-end"
    ERROR = "error"
```

**Critical event: `INTENT_PROGRESS`** -- This is how streaming deltas from the
conversation agent are surfaced to the pipeline consumer (frontend, satellite).

Each event is a frozen dataclass:

```python
@dataclass(frozen=True)
class PipelineEvent:
    type: PipelineEventType
    data: dict[str, Any] | None = None
    timestamp: str  # UTC ISO format
```

Events are dispatched via `PipelineRun.process_event()` which calls the
`event_callback` registered when the pipeline was created.

---

## 3. How the Pipeline Calls the Conversation Agent

### 3.1 Entry point: `recognize_intent()`

```python
async def recognize_intent(
    self,
    intent_input: str,
    conversation_id: str,
    conversation_extra_system_prompt: str | None,
) -> tuple[str, bool]:  # (tts_text, all_targets_in_satellite_area)
```

Inside, it calls:

```python
conversation_result = await conversation.async_converse(
    hass=self.hass,
    text=intent_input,
    conversation_id=conversation_id,
    device_id=self._device_id,
    satellite_id=self._satellite_id,
    context=self.context,
    language=self.language,
    agent_id=agent_id,
    extra_system_prompt=extra_system_prompt,
)
```

`conversation.async_converse()` is a blocking (awaited) call. It returns a
`ConversationResult` only after the agent has finished processing -- including all
tool calls and multi-turn LLM interactions within the ChatLog.

### 3.2 Delta listener (streaming within the blocking call)

Before calling `async_converse`, the pipeline attaches a **delta listener** to the
ChatLog. This listener fires synchronously (via `@callback`) each time the conversation
agent yields a streaming delta:

```python
@callback
def chat_log_delta_listener(
    chat_log: conversation.ChatLog, delta: dict
) -> None:
    # 1. Emit INTENT_PROGRESS event to pipeline consumers
    self.process_event(
        PipelineEvent(
            PipelineEventType.INTENT_PROGRESS,
            {"chat_log_delta": delta},
        )
    )

    # 2. Feed content deltas to TTS input stream
    if tts_input_stream is None:
        return

    if role := delta.get("role"):
        chat_log_role = role

    if chat_log_role != "assistant":
        return

    if content := delta.get("content"):
        tts_input_stream.put_nowait(content)
```

**Key point**: Deltas are `dict` objects with optional keys:
- `"role"` -- `"assistant"`, `"user"`, etc. Signals start of a new message.
- `"content"` -- Text fragment (the actual streaming text).
- `"tool_calls"` -- Tool call requests.
- `"thinking_content"` -- Chain-of-thought (filtered out of TTS).

Only `"assistant"` role content is fed to TTS. Tool call results, thinking content,
and user messages are ignored for TTS purposes.

---

## 4. Streaming Decision Logic

The pipeline does NOT immediately start streaming to TTS. It uses a **character
threshold** to decide:

```python
STREAM_RESPONSE_CHARS = 60
```

### Decision flow:

```python
delta_character_count = 0
_streamed_response_text = False

# On each delta:
start_streaming = delta_character_count > 0 and delta.get("tool_calls")

if not start_streaming and content:
    delta_character_count += len(content)
    start_streaming = delta_character_count > STREAM_RESPONSE_CHARS

if start_streaming and not self._streamed_response_text:
    self._streamed_response_text = True
    # Emit special event
    self.process_event(PipelineEvent(
        PipelineEventType.INTENT_PROGRESS,
        {"tts_start_streaming": True},
    ))
    # Drain queue, concatenate buffered content, re-enqueue as single chunk
    parts = []
    while not tts_input_stream.empty():
        parts.append(tts_input_stream.get_nowait())
    tts_input_stream.put_nowait("".join(parts))

    # Attach the stream to the TTS ResultStream
    self.tts_stream.async_set_message_stream(
        tts_input_stream_generator()
    )
```

### Streaming triggers:

1. **Character threshold**: When accumulated content exceeds 60 characters, streaming
   begins. This avoids streaming short responses like "OK" or "Done".
2. **Tool call after content**: If the agent emits content and then starts a tool call,
   streaming begins immediately (the existing text is sent to TTS while the tool executes).

### Before threshold is reached:

Content deltas are queued in `tts_input_stream` (an `asyncio.Queue[str | None]`) but
NOT yet connected to TTS. They accumulate silently.

### When threshold is crossed:

1. All queued chunks are drained and concatenated into a single string.
2. That concatenated string is re-enqueued.
3. The queue is connected to TTS via `async_set_message_stream()`.
4. All subsequent deltas flow directly through the queue to TTS.

### If threshold is never crossed (short response):

The response is handled non-streaming. After `async_converse()` returns, the full
response text is passed via `async_set_message()` (non-streaming path).

---

## 5. TTS Input Stream Architecture

### The queue

```python
tts_input_stream: asyncio.Queue[str | None] = asyncio.Queue()
```

- Content deltas are enqueued as `str` fragments.
- `None` sentinel terminates the stream.

### The generator

```python
async def tts_input_stream_generator() -> AsyncGenerator[str]:
    while (tts_input := await tts_input_stream.get()) is not None:
        yield tts_input
```

### Stream termination

After `async_converse()` returns:

```python
if tts_input_stream and self._streamed_response_text:
    tts_input_stream.put_nowait(None)  # terminate the generator
```

### Pre-condition for streaming

Streaming only activates if `self.tts_stream.supports_streaming_input` is `True`.
This is determined by the TTS engine:

```python
if self.tts_stream and self.tts_stream.supports_streaming_input:
    tts_input_stream = asyncio.Queue()
else:
    tts_input_stream = None  # no streaming, full text only
```

---

## 6. TTS Processing (`ResultStream` and `TTSCache`)

### ResultStream

Created during pipeline preparation:

```python
self.tts_stream = tts.async_create_stream(
    hass=self.hass,
    engine=engine,
    language=self.pipeline.tts_language,
    options=tts_options,
)
```

Two paths for setting content:

| Method | When used |
|---|---|
| `async_set_message(text)` | Short/non-streamed responses. Full text available. Uses disk cache. |
| `async_set_message_stream(gen)` | Streamed responses (>60 chars). Text arrives incrementally. No disk cache (uses ULID key). |

### TTSCache (buffering and fan-out)

```python
class TTSCache:
    _result_data: bytes | None      # final complete audio
    _partial_data: list[bytes]      # chunks received so far
    _consumers: list[asyncio.Queue] # active streaming consumers
    _data_gen: AsyncGenerator[bytes] # source of audio bytes
```

**Loading flow** (`async_load_data`):
1. Iterates `_data_gen` (audio bytes from TTS engine).
2. Appends each chunk to `_partial_data`.
3. Pushes each chunk to all registered consumer queues.
4. On completion, joins all chunks into `_result_data`.
5. Sends `None` sentinel to all consumers.

**Streaming flow** (`async_stream_data`):
1. If fully loaded, yields `_result_data` in one shot.
2. Otherwise, yields all existing `_partial_data` chunks.
3. Then registers a consumer queue and awaits future chunks.
4. This enables **concurrent production and consumption** of audio bytes.

### Audio generation path

```python
async def _async_generate_tts_audio(
    self, engine_instance, message_or_stream, language, options
) -> AsyncGenerator[bytes]:
```

Two sub-paths:

**Non-streaming TTS engine** (e.g., cloud TTS):
- If `message_or_stream` is an `AsyncGenerator[str]`, collects all text first:
  `message = "".join([chunk async for chunk in message_or_stream])`
- Calls `engine.async_internal_get_tts_audio(message, language, options)`
- Returns complete audio as a single chunk.

**Streaming TTS engine** (e.g., Wyoming with streaming support):
- Passes the text `AsyncGenerator[str]` directly to the engine:
  `engine.internal_async_stream_tts_audio(TTSAudioRequest(language, options, stream))`
- Returns `TTSAudioResponse.data_gen` (an `AsyncGenerator[bytes]`).

Audio conversion (format, sample rate) via ffmpeg is applied as a post-processing
`AsyncGenerator` wrapper if needed.

---

## 7. TTS Entity Streaming Interface

### TTSAudioRequest / TTSAudioResponse

```python
@dataclass
class TTSAudioRequest:
    language: str
    options: dict[str, Any]
    message_gen: AsyncGenerator[str]  # streaming text input

@dataclass
class TTSAudioResponse:
    extension: str                    # e.g., "wav"
    data_gen: AsyncGenerator[bytes]   # streaming audio output
```

### Engine capability detection

```python
def async_supports_streaming_input(self) -> bool:
    """Return if the TTS engine supports streaming input."""
    return (
        self.__class__.async_stream_tts_audio
        is not TextToSpeechEntity.async_stream_tts_audio
    )
```

An engine supports streaming if it overrides `async_stream_tts_audio`. Otherwise the
pipeline falls back to collecting all text then calling `async_get_tts_audio`.

---

## 8. Wyoming TTS Streaming Implementation

Wyoming TTS entities implement true streaming via TCP:

### Text transmission (`_write_tts_message`):
1. Send `SynthesizeStart` event with voice configuration.
2. For each text chunk from `message_gen`: send `SynthesizeChunk` event.
3. Send `SynthesizeStop` event to signal end of text.

### Audio reception (`_read_tts_audio`):
1. Yield a WAV header with zero frame count (streaming indicator).
2. For each `AudioChunk` event received: yield raw PCM bytes.
3. Terminate on `SynthesizeStopped` event.

### Key implication
Wyoming TTS can start producing audio **before the full text is available**. The text
chunks flow in as the LLM generates them, and audio chunks flow out as the TTS engine
synthesizes them. This is a true streaming pipeline:

```
LLM deltas -> asyncio.Queue -> tts_input_stream_generator() ->
TTSAudioRequest.message_gen -> Wyoming SynthesizeChunk events ->
Wyoming AudioChunk events -> TTSAudioResponse.data_gen ->
TTSCache.async_stream_data() -> HTTP StreamResponse / satellite
```

---

## 9. Pipeline to Satellite Audio Path

### 9.1 TTS output in pipeline events

The `text_to_speech()` method emits:

```python
# TTS_START event
PipelineEvent(PipelineEventType.TTS_START, {
    "engine": self.tts_stream.engine,
    "language": self.pipeline.tts_language,
    "voice": self.pipeline.tts_voice,
    "tts_input": tts_input,
    "acknowledge_override": override_media_path is not None,
})

# TTS_END event
PipelineEvent(PipelineEventType.TTS_END, {
    "tts_output": {
        "media_id": self.tts_stream.media_source_id,
        "token": self.tts_stream.token,
        "url": self.tts_stream.url,          # /api/tts_proxy/{token}
        "mime_type": self.tts_stream.content_type,
    }
})
```

**Important**: `TTS_END` is emitted as soon as the stream is *set up*, NOT when audio
generation is complete. The actual audio bytes may still be generating. The `url` and
`token` point to a streaming endpoint that will block until data is available.

### 9.2 RUN_START announces TTS stream token

```python
# In PipelineRun.start():
data = {
    "pipeline": self.pipeline.id,
    "language": self.language,
    "conversation_id": conversation_id,
}
if self.tts_stream:
    data["tts_output"] = {
        "token": self.tts_stream.token,
        # ... other fields
    }
```

The Wyoming satellite captures this token in `on_pipeline_event()`:

```python
if event.type == PipelineEventType.RUN_START:
    if event.data and (tts_output := event.data["tts_output"]):
        self._tts_stream_token = tts_output["token"]
```

### 9.3 Wyoming satellite audio streaming

The Wyoming satellite's `_stream_tts()` method:

1. Gets the `ResultStream` by token.
2. Calls `async_stream_result()` which yields audio bytes as they become available.
3. Parses the WAV header (first 44 bytes) for sample rate/width/channels.
4. Sends `AudioStart` event to the Wyoming satellite client with audio format info.
5. Sends audio in `AudioChunk` events of 2048 bytes each.
6. Sends `AudioStop` event when complete.
7. Waits for `Played` event from satellite confirming playback finished.
8. Calls `tts_response_finished()`.

```
ResultStream.async_stream_result()
  -> WAV header parse
  -> AudioStart (to satellite)
  -> AudioChunk * N (2048 byte chunks, to satellite)
  -> AudioStop (to satellite)
  <- Played (from satellite)
  -> tts_response_finished()
```

### 9.4 HTTP proxy path (for frontend/media players)

The `/api/tts_proxy/{token}` endpoint:
1. Looks up the `ResultStream` by token.
2. Creates an `aiohttp.web.StreamResponse`.
3. Iterates `async_stream_result()`.
4. Writes each chunk with `response.write(data)`.
5. Calls `response.write_eof()`.

Both paths (Wyoming TCP and HTTP proxy) consume the same `async_stream_result()` API.

---

## 10. Acknowledge Beep Logic

When all intent targets are in the satellite's area (e.g., "turn off the bedroom
light" spoken from the bedroom), the pipeline plays a short beep instead of the full
TTS response:

```python
all_targets_in_satellite_area = self._get_all_targets_in_satellite_area(
    conversation_result.response,
    self._satellite_id,
    self._device_id,
)

if all_targets_in_satellite_area:
    await self.run.text_to_speech(
        tts_input or "",
        override_media_path=ACKNOWLEDGE_PATH  # short beep WAV
    )
else:
    await self.run.text_to_speech(tts_input)
```

In the TTS method, `override_media_path` bypasses the TTS engine entirely:

```python
if override_media_path:
    self.tts_stream.async_override_result(override_media_path)
elif not self._streamed_response_text:
    self.tts_stream.async_set_message(tts_input)
# If _streamed_response_text is True, message was already set via streaming
```

---

## 11. `continue_conversation` Flow

### 11.1 What it means

When a conversation agent returns `ConversationResult(continue_conversation=True)`,
it signals that the agent expects the user to respond again (e.g., a follow-up question,
clarification, or multi-step dialog).

### 11.2 How the pipeline handles it

```python
@dataclass
class PipelineConversationData:
    continue_conversation_agent: str | None = None

# In recognize_intent(), after async_converse returns:
if conversation_result.continue_conversation:
    self._conversation_data.continue_conversation_agent = agent_id
```

On the **next** pipeline run with the same `conversation_id`:

```python
if self._conversation_data.continue_conversation_agent is not None:
    agent_info = conversation.async_get_agent_info(
        self.hass,
        self._conversation_data.continue_conversation_agent
    )
    self._conversation_data.continue_conversation_agent = None
    self._intent_agent_only = True
```

This forces the next turn to use the same agent (overriding default routing) and
sets `_intent_agent_only = True` to skip sentence triggers / built-in intents.

### 11.3 How satellites handle it

The `RUN_END` event carries **no data** about `continue_conversation`. The satellite
does not receive explicit notification to keep listening.

Instead, the mechanism is indirect:
1. The satellite preserves `_conversation_id` across calls to
   `async_accept_pipeline_from_satellite()`.
2. The `PipelineConversationData` is stored server-side, keyed by conversation ID.
3. When the satellite calls the pipeline again (because the user spoke again), the
   stored `continue_conversation_agent` takes effect.

For Wyoming satellites specifically, `restart_on_end` in the `RunPipeline` message
controls whether the satellite automatically starts listening again:

```python
if run_pipeline.restart_on_end:
    # Automatically restart pipeline (always-on streaming satellites)
    self._run_pipeline_once(run_pipeline)
```

This is a satellite-level setting, not driven by `continue_conversation`.

### 11.4 Implications for our design

- `continue_conversation` does NOT cause the satellite to automatically re-listen.
- It only ensures the same agent handles the next turn.
- For multi-turn voice interactions, the satellite must independently decide to
  keep listening (via `restart_on_end` or manual re-invocation).

---

## 12. Error Handling

### 12.1 Conversation agent errors

```python
try:
    conversation_result = await conversation.async_converse(...)
except Exception as src_error:
    _LOGGER.exception("Unexpected error during intent recognition")
    raise IntentRecognitionError(
        code="intent-failed",
        message="Unexpected error during intent recognition",
    ) from src_error
```

### 12.2 TTS errors

If TTS fails, it raises `TextToSpeechError`. The pipeline catches all `PipelineError`
subclasses:

```python
except PipelineError as err:
    self.run.process_event(
        PipelineEvent(
            PipelineEventType.ERROR,
            {"code": err.code, "message": err.message},
        )
    )
```

### 12.3 Error event propagation

Error events are emitted to the same `event_callback`. Wyoming satellites and the
frontend receive these and can display/announce them.

### 12.4 What the user hears

When an error occurs:
- If it happens before TTS, the user hears nothing (or a timeout silence).
- The satellite transitions to `IDLE` state.
- The frontend displays the error code and message.
- There is no built-in "sorry, something went wrong" TTS fallback in the pipeline
  itself -- that would need to be implemented by the satellite or agent.

### 12.5 Timeout handling

- `TimeoutError` and `asyncio.CancelledError` are explicitly re-raised (not caught).
- Wake word detection has an optional `timeout` in `WakeWordSettings`.
- VAD-based silence detection uses `AudioSettings.silence_seconds` to determine when
  the user has stopped speaking.
- There is no explicit timeout on the conversation agent call itself within the
  pipeline. If the agent hangs, the pipeline hangs.

---

## 13. Complete Data Flow: LLM Delta to Satellite Speaker

```
                    CONVERSATION AGENT (LLM)
                              |
                    yields text deltas via ChatLog
                              |
                              v
                    chat_log_delta_listener (@callback)
                         /          \
                        /            \
              INTENT_PROGRESS     tts_input_stream.put_nowait(content)
              event emitted       (asyncio.Queue[str | None])
                   |                        |
                   v                        |
             pipeline consumers             |
             (frontend, satellite)          |
                                            |
                              [wait for 60 char threshold]
                                            |
                                    threshold crossed?
                                     /            \
                                   NO              YES
                                   |                |
                          async_converse()    drain queue,
                          returns full text   concatenate,
                                |             re-enqueue
                                v                |
                        async_set_message()      v
                        (non-streaming)    async_set_message_stream()
                                |             (streaming)
                                v                |
                          TTS engine             v
                          full text         tts_input_stream_generator()
                                |               |
                                v               v
                          audio bytes     TTSAudioRequest.message_gen
                          (single chunk)        |
                                |               v
                                |         TTS engine streaming
                                |         (e.g., Wyoming: SynthesizeChunk)
                                |               |
                                v               v
                            TTSCache        TTSAudioResponse.data_gen
                            (loaded once)   (streaming audio bytes)
                                |               |
                                v               v
                            TTSCache.async_stream_data()
                            (yields bytes as available)
                                        |
                                        v
                              ResultStream.async_stream_result()
                                   /              \
                                  /                \
                    HTTP proxy endpoint     Wyoming satellite
                    StreamResponse          _stream_tts()
                    response.write()              |
                         |                        v
                         v                  AudioStart event
                    browser/media           AudioChunk events (2048B)
                    player                  AudioStop event
                                                  |
                                                  v
                                           satellite speaker
                                                  |
                                                  v
                                           Played event (confirmation)
                                                  |
                                                  v
                                           tts_response_finished()
                                           state -> IDLE
```

---

## 14. Timing and Ordering Constraints

### 14.1 Delta ordering

- Deltas arrive in order within the `chat_log_delta_listener` callback.
- The callback is `@callback`-decorated, meaning it runs synchronously on the HA
  event loop. No concurrent execution of multiple deltas.
- `put_nowait()` on the asyncio.Queue preserves ordering.

### 14.2 Stream setup timing

- `async_set_message_stream()` must be called **before** `text_to_speech()` returns.
- The TTS stream URL/token is allocated at pipeline preparation time (before any
  stage runs), so consumers can start waiting on the stream before TTS content is ready.
- The `RUN_START` event includes the TTS token, allowing satellites to prepare for
  audio before TTS actually begins.

### 14.3 TTS_END event timing

`TTS_END` fires when the stream is *configured*, not when audio is *complete*. The
satellite uses the token from `RUN_START` to begin streaming audio from the
`ResultStream`. Audio bytes may still be generating when `TTS_END` fires.

### 14.4 Concurrent pipeline stages

The INTENT and TTS stages overlap when streaming is active:
- `recognize_intent()` is still running (awaiting `async_converse()`).
- Meanwhile, text deltas flow through the queue to the TTS engine.
- The TTS engine may already be producing audio bytes.
- `text_to_speech()` is called only after `recognize_intent()` completes.
- But by then, `async_set_message_stream()` has already been called and the TTS
  engine is already processing text.

### 14.5 Queue termination

The `None` sentinel is sent to `tts_input_stream` only after `async_converse()`
returns. This means the TTS engine receives the complete text before the pipeline
formally enters the TTS stage.

---

## 15. Satellite State Machine

```
IDLE -> LISTENING -> PROCESSING -> RESPONDING -> IDLE
         (STT)       (INTENT)       (TTS)
```

State transitions are driven by pipeline events:

| Event | State transition |
|---|---|
| `STT_START` | -> `LISTENING` |
| `INTENT_START` | -> `PROCESSING` |
| `TTS_START` | -> `RESPONDING` |
| `RUN_END` (no TTS) | -> `IDLE` |
| `tts_response_finished()` | -> `IDLE` |

---

## 16. Constraints Affecting Our Streaming Design

### 16.1 We must implement `async_converse()` contract

Our conversation agent must work within the `async_converse()` call. This is a single
awaited coroutine that must return a `ConversationResult`. Streaming happens via the
ChatLog delta listener attached before the call.

### 16.2 Delta format

Deltas must be `dict` objects with these optional keys:
- `"role"`: `str` -- signals a new message. Required on first delta.
- `"content"`: `str` -- text fragment for TTS.
- `"tool_calls"`: tool invocation requests.
- `"thinking_content"`: chain-of-thought (not sent to TTS).

### 16.3 The 60-character threshold

Responses under 60 characters are NOT streamed to TTS. They are delivered as a
complete string after `async_converse()` returns. This means:
- Short confirmations ("OK", "Done", "Light turned on") use the non-streaming path.
- Only longer responses (explanations, lists, stories) trigger streaming TTS.
- We do NOT control this threshold; it is in the pipeline code.

### 16.4 TTS engine must support streaming input

If the configured TTS engine does not override `async_stream_tts_audio`, all streaming
is negated -- text is collected and sent as a complete string regardless of delta
delivery. Wyoming TTS supports streaming. Cloud TTS providers generally do not.

### 16.5 No explicit timeout on agent

The pipeline has no timeout on the `async_converse()` call. If our agent takes a long
time (e.g., slow LLM, complex tool chains), the pipeline will wait indefinitely. The
user experiences silence during this time. The satellite stays in `PROCESSING` state.

### 16.6 Sentence segmentation is NOT done by the pipeline

The pipeline does NOT split text into sentences for TTS. It passes raw delta fragments
directly. Sentence segmentation, if any, must be done by either:
- The conversation agent (our code), by emitting sentence-aligned deltas.
- The TTS engine, by processing incoming text chunks intelligently.

### 16.7 Tool calls interrupt streaming

If the agent emits content and then a tool call, streaming starts immediately (the
existing text goes to TTS while the tool executes). After the tool returns, the agent
may emit more content which continues through the stream.

### 16.8 `continue_conversation` is server-side only

The satellite does not receive `continue_conversation` in any event. It is stored in
`PipelineConversationData` and affects agent routing on the next turn. The satellite
decides independently whether to keep listening.
