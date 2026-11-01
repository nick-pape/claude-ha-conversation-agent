/**
 * Long-lived Claude sessions, so a voice turn doesn't pay for a CLI start.
 *
 * Starting a session (spawn the Claude Code CLI, initialize, connect MCP
 * servers) cost ~1.5 s per turn when every request ran a fresh query().
 * Instead:
 *
 * - each conversation keeps one CLI process, fed one user message per turn
 *   through an input queue (the SDK's streaming-input mode), so follow-ups
 *   go straight to a running session;
 * - one spare process is always warming (startup()) with the options the
 *   last conversation used, so a new conversation starts on a ready process.
 *
 * Sessions close after IDLE_MS without a turn (matching the integration's
 * conversation TTL), or as soon as a turn is abandoned part-way, since its
 * unread output would otherwise leak into the next turn.
 */

import { query, startup } from "@anthropic-ai/claude-agent-sdk";

const IDLE_MS = 5 * 60 * 1000;

/** Async iterable of user messages that stays open between turns. */
class InputQueue {
  #items = [];
  #waiters = [];
  #closed = false;

  push(text) {
    const message = {
      type: "user",
      message: { role: "user", content: text },
      parent_tool_use_id: null,
    };
    const waiter = this.#waiters.shift();
    if (waiter) waiter({ value: message, done: false });
    else this.#items.push(message);
  }

  close() {
    this.#closed = true;
    for (const waiter of this.#waiters.splice(0)) waiter({ value: undefined, done: true });
  }

  [Symbol.asyncIterator]() {
    return {
      next: () => {
        if (this.#items.length) return Promise.resolve({ value: this.#items.shift(), done: false });
        if (this.#closed) return Promise.resolve({ value: undefined, done: true });
        return new Promise((resolve) => this.#waiters.push(resolve));
      },
    };
  }
}

/** One CLI process serving every turn of one conversation. */
class LiveSession {
  constructor(pool, q, input) {
    this.pool = pool;
    this.q = q;
    this.input = input;
    this.messages = q[Symbol.asyncIterator]();
    this.sessionId = null;
    this.busy = false;
    this.closed = false;
    this.timer = null;
  }

  /** Sends one user turn; yields SDK messages up to and including its result. */
  async *turn(text) {
    this.busy = true;
    clearTimeout(this.timer);
    let finished = false;
    try {
      this.input.push(text);
      while (true) {
        const { value, done } = await this.messages.next();
        if (done) throw new Error("Claude session ended unexpectedly");
        if (value.session_id && !this.sessionId) {
          this.sessionId = value.session_id;
          this.pool.track(this);
        }
        yield value;
        if (value.type === "result") {
          finished = true;
          // Out of turns: let the next turn start a fresh session.
          if (value.subtype === "error_max_turns") this.close();
          return;
        }
      }
    } finally {
      this.busy = false;
      if (!finished) this.close();
      else if (!this.closed) this.timer = setTimeout(() => this.close(), IDLE_MS);
    }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    clearTimeout(this.timer);
    this.input.close();
    this.q.close();
    this.pool.forget(this);
  }
}

export class SessionPool {
  #live = new Map(); // SDK session id -> LiveSession
  #spare = null; // { key, warm: Promise<WarmQuery | null> }

  /**
   * The session to run a turn on, and how it was obtained:
   *   "live"    the conversation's own running session
   *   "warm"    the pre-warmed spare (new conversation)
   *   "cold"    a freshly started process (no matching spare)
   *   "resumed" a fresh process resuming a session this pool no longer holds
   *
   * `key` identifies the options; a spare is only used for the same key.
   * `makeOptions` builds them afresh for each process, since an in-process
   * MCP server instance serves one process at a time.
   */
  async acquire({ sessionId, key, makeOptions }) {
    if (sessionId) {
      const live = this.#live.get(sessionId);
      if (live && !live.busy && !live.closed) return { session: live, how: "live" };
      const input = new InputQueue();
      const session = new LiveSession(this, query({ prompt: input, options: { ...makeOptions(), resume: sessionId } }), input);
      return { session, how: "resumed" };
    }

    let warm = null;
    if (this.#spare && this.#spare.key === key) {
      warm = await this.#spare.warm;
      this.#spare = null;
    }
    const input = new InputQueue();
    const q = warm ? warm.query(input) : query({ prompt: input, options: makeOptions() });
    const session = new LiveSession(this, q, input);
    this.warmSpare(key, makeOptions);
    return { session, how: warm ? "warm" : "cold" };
  }

  /** Starts warming a spare for `key`, replacing a spare for other options. */
  warmSpare(key, makeOptions) {
    if (this.#spare && this.#spare.key === key) return;
    this.#spare?.warm.then((w) => w?.close());
    const spare = {
      key,
      warm: startup({ options: makeOptions() }).catch((err) => {
        console.error("Pre-warming a Claude session failed:", err.message);
        if (this.#spare === spare) this.#spare = null;
        return null;
      }),
    };
    this.#spare = spare;
  }

  track(session) {
    this.#live.set(session.sessionId, session);
  }

  forget(session) {
    if (session.sessionId && this.#live.get(session.sessionId) === session) {
      this.#live.delete(session.sessionId);
    }
  }
}
