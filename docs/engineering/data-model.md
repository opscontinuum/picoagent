# Data model

## Core types

Everything shared lives in `core/types.py` as plain dataclasses. Providers map these to their
own wire format; nothing else in the codebase defines a message shape.

```mermaid
classDiagram
    class Message {
        +Role role
        +str text
        +list~ToolCall~ tool_calls
        +list~ToolResult~ tool_results
        +list~dict~ images
        +dict meta
        +float ts
        +to_dict() dict
        +from_dict(d) Message
    }

    class ToolCall {
        +str id
        +str name
        +dict args
    }

    class ToolResult {
        +str tool_call_id
        +str content
        +bool is_error
        +dict details
    }

    class ToolSpec {
        +str name
        +str description
        +dict parameters
    }

    class StreamEvent {
        +str type
        +str text
        +ToolCall tool_call
        +dict usage
        +str error
    }

    Message "1" o-- "0..*" ToolCall
    Message "1" o-- "0..*" ToolResult
    StreamEvent ..> ToolCall : carries when type is tool_call
    ToolCall ..> ToolResult : id links the pair
```

Two conventions that the shapes encode:

`ToolResult.content` goes to the model; `details` does not. A tool that wants to tell a UI
something without spending context puts it in `details`.

An expected failure is a value, not an exception. A missing file or a non-zero exit returns
`ToolResult(is_error=True)` so the model can read it and recover. Only genuine bugs raise, and
the loop catches those and turns them into the same shape.

## Sessions are a tree, not a list

Each JSONL entry carries an `id` and a `parent`. `leaf` points at the newest entry, and
`branch()` walks parents back to the root to build the model's context. Nothing is ever
deleted or edited in place.

```mermaid
graph TD
    r["user: add a --verbose flag"] --> a1["assistant: tool_calls"]
    a1 --> t1["tool: read results"]
    t1 --> a2["assistant: answer"]
    a2 -.->|"leaf moved back,<br/>then appended"| b1["user: actually, use -v"]
    b1 --> b2["assistant: different answer"]

    a2 --> c1["custom: compaction summary<br/>replace everything before a1"]

    classDef branch stroke-dasharray: 4 3
    class b1,b2 branch
```

That shape buys three things a flat log cannot:

**Rewind and branch.** Move `leaf` to an older entry and append; the old path still exists and
is still reachable. Undo, alternate takes, and tree UIs are plugin work on top of this file
rather than core features.

**Compaction without loss.** A summary is just another entry saying "when building context,
replace everything before entry X with this". The original messages stay on disk, so the
compaction is auditable and reversible.

**Plugin state that survives restarts.** `api.append_entry(type, data)` writes a custom entry
that is persisted and replayed to the plugin, but never sent to the model.
