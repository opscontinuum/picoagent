# Events reference

Handlers are registered with `api.on(name, handler)` and called as `handler(payload, rt)`
where `rt` is the `Runtime`. They run in plugin load order. Returning a dict merges it into
the payload for later handlers and for the core.

| Event | When | Payload | You may return |
|---|---|---|---|
| `session_start` | after plugins load, before the first prompt | `resume: bool` | - |
| `session_end` | on exit | - | - |
| `input` | user submitted text, before command/skill handling | `text`, `images`, `action` | `{"text": ...}` to rewrite; `{"action": "handled"}` to consume it |
| `before_agent_start` | once per prompt, before the first model call | `prompt`, `system_prompt`, `message` | `{"system_prompt": ...}`; `{"message": "..."}` injects a user-role message |
| `turn_start` | each model call | `turn` | - |
| `context` | right before each model call | `messages` (deep copy), `system_prompt` | `{"messages": [...]}` to replace the history (compaction) |
| `message_update` | each streamed text delta | `delta` | - |
| `message_end` | assistant message complete | `message` | - |
| `tool_call` | before a tool runs (sequential, in model order) | `name`, `args`, `id`, `block` | `{"args": {...}}` to rewrite; `{"block": True, "reason": "..."}` to refuse |
| `tool_execution_start` | tool is about to execute | `call` | - |
| `tool_result` | after a tool ran | `name`, `args`, `content`, `is_error`, `details` | any subset of `content`/`is_error`/`details` to patch |
| `tool_execution_end` | after result patching | `call`, `result` | - |
| `turn_end` | after the tool batch (or the final answer) | `turn`, `message`, `tool_results?` | - |
| `provider_error` | the provider reported an error | `error`, `retry` | `{"retry": True}` to re-run the model call |
| `agent_end` | the model stopped calling tools | - | - |
| `agent_settled` | after follow-ups drained; the agent is idle | - | - |
| `user_bash` | user typed `!cmd` in the REPL | `command`, `result` | `{"result": "..."}` to supply the output yourself |
| `model_select` | `api.set_model` was called, or the user ran `/model <name>` | `model`, `previous`, `provider` | - |

Plugins can define their own events with `await api.emit("thing", {...})`; other plugins
subscribe to `"<plugin-name>:thing"`.

## Semantics worth knowing

* **Blocking is first-wins.** Once a handler returns `block: True`, later handlers don't run
  and `blocked_by` is set to that plugin's name.
* **Patches chain.** Two handlers rewriting `text` see each other's output in order.
* **Exceptions are contained.** A handler that raises is logged and skipped.
* **`tool_call` runs before parallel execution.** Sibling calls are preflighted one by one, so
  a handler can't see sibling results, but it can rely on a stable order.
* **`context` gets a copy.** Mutating `payload["messages"]` in place is safe; returning
  `{"messages": ...}` is the explicit way.
