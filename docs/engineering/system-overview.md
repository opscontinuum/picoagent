# System overview

## Module dependencies

Dependencies point one way: down. Nothing in `core/` imports from `plugins/` or `frontends/`.
Plugins reach into core through `PluginAPI` and never the reverse, which is what stops a
plugin from being able to break the loop merely by existing - the loop has no knowledge that
any particular plugin is there.

```mermaid
graph TD
    cli["cli.py<br/>wiring, argument parsing"]

    subgraph core["core/"]
        loop["loop.py<br/>AgentLoop + Runtime"]
        provider["provider.py<br/>Provider protocol + OpenAI dialect"]
        vertex["vertex.py<br/>Gemini/Vertex dialect"]
        dialects["dialects.py<br/>a provider per [providers.n] table"]
        tools["tools.py<br/>Tool protocol + registry"]
        skills["skills.py<br/>SKILL.md discovery"]
        context["context.py<br/>system prompt sections"]
        session["session.py<br/>append-only JSONL"]
        commands["commands.py<br/>slash commands"]
        events["events.py<br/>event bus"]
        config["config.py<br/>layered TOML"]
        types["types.py<br/>shared dataclasses"]
    end

    subgraph plug["plugins/"]
        api["api.py<br/>PluginAPI"]
        loader["loader.py<br/>discovery, trust, import"]
        manifest["manifest.py<br/>plugin.toml"]
    end

    subgraph fe["frontends/"]
        plain["plain.py<br/>REPL"]
        printfe["print.py<br/>-p and --json"]
    end

    cli --> config
    cli --> dialects
    cli --> loop
    cli --> loader
    cli --> plain
    cli --> printfe

    loop --> events
    loop --> tools
    loop --> skills
    loop --> commands
    loop --> provider
    loop --> context
    loop --> session

    loader --> manifest
    loader --> api
    api --> loop

    dialects --> provider
    dialects --> vertex
    vertex --> provider
    provider --> types
    tools --> types
    session --> types
    loop --> types
```

`loop.py` is the only module that knows about every registry. Everything else knows about
`types.py` and whatever sits below it.

## How an override wins

Every registry replaces on name. Registering a tool called `shell` *is* the shell tool from
then on - there is no priority field, no merge, no plugin ordering API. The only thing that
decides a winner is who registers last, and load order decides that:

```mermaid
graph LR
    core["core built-ins<br/>read, write, edit, shell"] --> uc["user config<br/>~/.picoagent/config.toml"]
    uc --> pc["project config<br/>.picoagent/config.toml"]
    pc --> ud["user plugin dir<br/>~/.picoagent/plugins/"]
    ud --> pd["project plugin dir<br/>.picoagent/plugins/"]
    pd --> e["-e paths<br/>trusted for this run only"]
    e --> win["last registration wins"]
```

So a project can override what a user installed, and a one-off `-e` beats both. The same rule
covers providers, commands (`help`), system-prompt sections (`base`), and the frontend - one
mechanism instead of five. Providers reach that ladder one rung earlier than the others: the
user's config registers one per `[providers.<name>]` table before any plugin loads, so a plugin
registering the same name still wins, and a table naming a dialect core does not have registers
nothing at all rather than something that speaks the wrong format.

The cost of that simplicity is worth naming: two plugins that both register `shell` do not
conflict loudly, the later one just wins. `picoagent plugin list` and `/tools` are how you see
what actually ended up registered.
