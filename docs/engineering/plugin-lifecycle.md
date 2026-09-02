# Plugin lifecycle

## The extension surface

Plugins are structural, not inherited. A tool is any object with the right attributes and an
`execute` coroutine - there is no base class to subclass and no registration decorator. That is
what lets a plugin replace a built-in without importing it.

```mermaid
classDiagram
    class Tool {
        <<protocol>>
        +str name
        +str description
        +dict parameters
        +execute(args, ctx) ToolResult
    }

    class Provider {
        <<protocol>>
        +str name
        +stream(system, messages, tools, model, max_tokens, thinking)
        +list_models() list~str~
    }

    class Frontend {
        <<protocol>>
        +emit(event, payload)
        +ask(kind, prompt)
        +read_input() str
        +run(agent)
    }

    class PluginAPI {
        +on(event, handler)
        +register_tool(tool)
        +register_provider(provider)
        +register_command(name, handler, description)
        +register_frontend(frontend)
        +register_system_prompt_section(name, render)
        +set_active_tools(names)
        +append_entry(type, data)
        +plugin_config() dict
    }

    Tool <|.. ReadTool
    Tool <|.. ShellTool
    Tool <|.. GuardedShellTool : replaces shell by name
    Provider <|.. OpenAICompatProvider
    Frontend <|.. PlainFrontend
    Frontend <|.. PrintFrontend
    PluginAPI ..> Tool : register_tool
    PluginAPI ..> Provider : register_provider
    PluginAPI ..> Frontend : register_frontend
```

`list_models` is deliberately optional on `Provider`: not every backend has an equivalent of
`GET /models`, and callers check with `hasattr` rather than forcing a provider to fake one.

## Discovery, trust, load

Plugin code runs with your privileges, so the trust check sits between discovery and import -
nothing is imported before it passes.

```mermaid
flowchart TD
    start([startup]) --> disc[discover roots<br/>config specs, plugin dirs, -e paths]
    disc --> git{git spec?}
    git -- yes --> clone[clone or fetch into<br/>the plugins dir]
    git -- no --> localdir[use the local path]
    clone --> mf[read plugin.toml]
    localdir --> mf
    mf --> valid{manifest valid?}
    valid -- no --> skipbad([skip, report as invalid])
    valid -- yes --> eflag{loaded with -e?}
    eflag -- yes --> imp[import entry module]
    eflag -- no --> known{recorded in trust.json?}
    known -- no --> isnew([skip, report as new])
    known -- yes --> fp{fingerprint matches?}
    fp -- no --> chg([skip, report as CHANGED])
    fp -- yes --> imp
    imp --> reg["call register(api)"]
    reg --> sk[add the plugin's SKILL.md dirs]
    sk --> ok([loaded])
```

`-e` bypasses the trust check for that run only. It exists so an edit-run loop while developing
a plugin doesn't demand re-approval on every save.

## Trust states

The fingerprint is a hash of `plugin.toml` plus the entry module. Any edit invalidates it -
including your own.

```mermaid
stateDiagram-v2
    [*] --> New: discovered, never approved
    New --> Trusted: plugin trust, after review
    Trusted --> Changed: any edit to plugin.toml or the entry module
    Changed --> Trusted: plugin trust, after seeing what moved
    Changed --> Changed: declined, stays unloaded
    New --> New: declined, stays unloaded

    note right of Changed
        The security-relevant state.
        Code that was reviewed has been
        replaced by code that has not.
        An upgrade and your own edit
        look identical here, so the CLI
        describes the change and asks
        rather than deciding.
    end note
```

`Changed` is why the trust record stores more than a bare hash: per-file hashes name the file
that moved, and the recorded commit turns an upgrade into a reviewable range. A hash alone can
only report *that* something changed, which leaves one blunt response - re-approve and hope.
