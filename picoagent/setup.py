"""``picoagent setup`` - ask what this install should point at, then write it down.

The failure this exists to end: a first run with nothing configured reached OpenAI, got a 401,
and printed the JSON body of it. That text names an ``api_key`` request parameter. It does not
name picoagent, or ``~/.picoagent/config.toml``, or ``PICOAGENT_BASE_URL``, or anything a person
could act on, because it was not written for them. The other half of the fix is in
:func:`~picoagent.core.provider.describe_model_failure`, which puts picoagent's own sentence in
front of that body and names this command; this module is what that sentence points at.

The flow is deliberately plain - pick a provider, answer what it needs, pick a model, prove it
works, save it - and it asks the *provider* what it needs rather than carrying a table of vendor
knowledge. A provider that declares
:class:`~picoagent.core.provider.SetupField` is asked for exactly its own fields, which is how a
plugin's dialect appears in this wizard the day it is installed, with its own vocabulary
(Vertex wants a project and a location, not a bearer token), without a line changing here. A
provider that declares nothing is asked for ``base_url`` and ``api_key``, which is what an
OpenAI-compatible endpoint wants and what most of them are.

"Pick a provider" includes picking one that does not exist yet. Core ships two wire dialects and
a provider is a name plus a table, so adding ``grok``, ``local`` or ``milgemini`` is a question
this wizard can ask and a table it can write - not a plugin somebody has to find, trust and
install. :data:`NEW_PROVIDER` is the last option in the list, always, and choosing it asks for
the name and the dialect before handing over to exactly the same field-by-field flow.

Two rules this holds to, because it is writing a credential to somebody's disk:

* **Never without a person.** No terminal, or ``--non-interactive``, refuses and says so. A
  wizard that blocks on ``input()`` inside a CI job is a hung build with no output.
* **Never clobber.** A value already in the file is shown - masked, if it is a secret - and
  replaced only on a yes. What is not being changed is not rewritten either: the file is edited
  in place by :mod:`picoagent.core.toml_write`, so comments, ordering and settings this wizard
  has never heard of come through untouched.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .core.config import provider_config
from .core.dialects import DEFAULT_DIALECT, DIALECT_KEY, DIALECTS, build_provider
from .core.loop import Runtime
from .core.provider import SetupField
from .core.session import restrict_to_owner
from .core.toml_write import TomlEditError, apply_edits
from .core.tools import is_windows
from .core.types import Message

#: What a provider that declares no :class:`SetupField` is asked for. The OpenAI-compatible
#: dialect is the one core ships and the one most gateways speak, so its two settings are the
#: honest guess for a provider that has not said otherwise - and a wrong guess costs a user two
#: keys in a config file rather than a failed setup, since the provider is asked to prove itself
#: before anything is written.
DEFAULT_FIELDS: tuple[SetupField, ...] = (
    SetupField("base_url", "Endpoint URL", "https://api.openai.com/v1"),
    SetupField("api_key", "API key", secret=True),
)

#: The option that turns "which of these?" into "a new one, please". Always offered, because a
#: list of what is already registered can only ever be an edit: the first provider somebody adds
#: beyond the built-in one has no entry to pick, and a wizard that can only edit sends them to
#: the config file to write the table by hand - which is the thing this command exists to avoid.
NEW_PROVIDER = "(a new provider)"

#: The option that lets somebody name a model the server did not list. A server's ``/models`` is
#: not always complete - a gateway that proxies several backends often lists none of them - and a
#: wizard that only offers what it was told about is one a user has to walk around.
TYPE_IT_IN = "(type a name)"

#: Enough of a stored credential to recognise it by, never enough to use. Shown when a key is
#: already in the file and the wizard is asking whether to replace it: the question "is this the
#: right key?" needs the last few characters and nothing else.
KEEP_TAIL = 4


def refusal_without_a_terminal(non_interactive: bool) -> str | None:
    """Why this cannot run, or ``None`` when it can.

    Two ways to arrive with nobody there: the flag, and a redirected stdin. Both get the same
    answer, because the difference matters to the caller and not to the outcome - there is no
    version of "ask the user which model" that works without a user. The message says what to do
    instead, since a script that reached here wanted settings written and there is a file it can
    write them to directly.
    """
    if non_interactive:
        return ("picoagent setup asks questions, so it cannot run with --non-interactive. Write "
                "the settings into your config.toml directly: model, provider, and a "
                "[providers.<name>] table holding that provider's endpoint and key.")
    if not sys.stdin.isatty():
        return ("picoagent setup asks questions and stdin is not a terminal, so there is nobody "
                "to answer them. Run it from a terminal, or write the settings into your "
                "config.toml directly: model, provider, and a [providers.<name>] table.")
    return None


def mask(value: str) -> str:
    """A stored secret as much as it may be shown."""
    if not value:
        return "(empty)"
    if len(value) <= KEEP_TAIL * 2:
        return "*" * len(value)
    return f"{value[:3]}...{value[-KEEP_TAIL:]}"


@dataclass(frozen=True)
class Chosen:
    """The provider this run is about, and the settings choosing it already decided.

    ``values`` is empty for a provider that already exists, and carries ``dialect`` for one the
    user has just invented, because that is the key which tells the next session how to rebuild
    what was registered here. It travels with the name rather than being looked up later: the
    provider object in the registry is the only thing that knows which dialect built it, and by
    the time :func:`save` runs it is a second runtime's object, built from the file this run has
    not written yet.
    """
    name: str
    values: dict[str, Any] = field(default_factory=dict)


async def choose_provider(rt: Runtime) -> Chosen | None:
    """Which provider to configure - an existing one or a new one - or ``None`` if neither.

    The list is the live registry, so every ``[providers.<name>]`` table in the config and every
    dialect a plugin registered is offered, and neither this function nor the wizard around it
    knows the difference between them.

    :data:`NEW_PROVIDER` is always the last option, including when exactly one provider exists,
    which is why there is no longer a shortcut for that case. The single-provider install is
    precisely the one where the answer to "which provider?" is most often "none of these": it is
    a fresh checkout whose only registration is the built-in client, and the person running setup
    has an xAI key or a Gemini project in front of them.
    """
    names = sorted(rt.providers.names())
    picked = await rt.frontend.ask("select", "Which provider?", options=names + [NEW_PROVIDER])
    if picked is None:
        return None
    if picked != NEW_PROVIDER:
        return Chosen(picked)
    return await create_provider(rt)


async def create_provider(rt: Runtime) -> Chosen | None:
    """Invent a provider: a name, a wire format, and a live object to ask the rest of.

    The object is built and registered here rather than at the end, because everything after
    this point - the fields to ask for, the model list, the verification call - comes from
    asking a provider, and a name with no object behind it would have to be special-cased in
    each of them.

    Only core's dialects can be created this way, and that is the honest limit rather than an
    oversight: a plugin's provider is constructed by that plugin's ``register()`` out of settings
    only it knows the shape of, so the way to add one is to install it, at which point it is in
    the list above like everything else.
    """
    name = ((await rt.frontend.ask("input", "Name for this provider (e.g. grok, local, milgemini)")
             ) or "").strip()
    refusal = why_not_a_new_provider(rt, name)
    if refusal:
        await rt.frontend.emit("error", {"text": refusal})
        return None
    dialect = await choose_dialect(rt)
    if dialect is None:
        return None
    rt.providers.register(build_provider(name, {DIALECT_KEY: dialect}))
    # Only when it is not the default. A table with no `dialect` key is the OpenAI-compatible
    # client by definition, so writing `dialect = "openai"` would add a line that says what the
    # file already said - and the config a user reads back should be the shortest true one.
    return Chosen(name, {DIALECT_KEY: dialect} if dialect != DEFAULT_DIALECT else {})


def why_not_a_new_provider(rt: Runtime, name: str) -> str | None:
    """Why ``name`` cannot be created, or ``None`` when it can.

    An existing name is refused rather than quietly treated as an edit. The two are different
    intentions and only one of them is what the user picked: somebody who typed ``openai`` at
    "name for this provider" has either forgotten it is in the list or means to replace it, and
    silently editing it would write over an endpoint and a key on the strength of a guess.
    """
    if not name:
        return "no name given, so there is no provider to create; nothing was written"
    if name in rt.providers.names():
        return (f"'{name}' is already a provider. Run setup again and pick it from the list to "
                f"change its settings; nothing was written")
    return None


async def choose_dialect(rt: Runtime) -> str | None:
    """Which wire format the new provider speaks, or ``None`` if the answer was not one.

    Asked rather than inferred from the endpoint, because the two guesses available are both
    bad: a URL says nothing reliable about the format behind it (every gateway proxies somebody
    else's), and defaulting silently is exactly the failure the dialect key exists to prevent.
    """
    options = sorted(DIALECTS)
    return await rt.frontend.ask(
        "select", f"Which wire format does it speak? ({DEFAULT_DIALECT} covers OpenAI, Ollama, "
                  f"vLLM, xAI and most gateways)", options=options)


def fields_of(provider: Any) -> tuple[SetupField, ...]:
    """What ``provider`` needs configured, or the OpenAI-compatible pair if it does not say.

    ``hasattr`` rather than a required member, the same way ``list_models`` is optional: a
    provider that predates this, or one whose settings are all environment variables, is still a
    provider and must not be unusable here because it declined to describe itself.
    """
    declared = getattr(provider, "setup_fields", None)
    return tuple(declared) if declared else DEFAULT_FIELDS


async def ask_field(rt: Runtime, field: SetupField, stored: dict) -> tuple[str, bool]:
    """Ask for one field. Returns its value and whether that value is a change worth writing.

    Empty input keeps what is already there, which is what makes a second run an edit rather
    than a retype: the person tabs through the settings they are happy with. A value that is
    already set and is about to become a different one is confirmed first, and a declined
    confirmation keeps the stored value rather than abandoning the run - the user answered "not
    that one", not "stop".
    """
    existing = stored.get(field.key)
    shown = None if existing is None else (mask(str(existing)) if field.secret else str(existing))
    hint = shown if shown is not None else (field.default or "empty")
    answer = await rt.frontend.ask("input", f"{field.prompt} [{hint}]", secret=field.secret)
    text = (answer or "").strip()
    if not text:
        return (str(existing) if existing is not None else field.default), existing is None
    if existing is not None and text != str(existing):
        question = f"{field.key} is already set to {shown}. Replace it?"
        if not await rt.frontend.ask("confirm", question):
            return str(existing), False
    return text, text != (str(existing) if existing is not None else None)


async def collect_fields(rt: Runtime, name: str) -> dict[str, Any]:
    """Every field the provider called ``name`` needs, asked for in its own words."""
    stored = provider_config(rt.cfg, name)
    provider = rt.providers.get(name)
    answers: dict[str, Any] = {}
    for field in fields_of(provider):
        value, changed = await ask_field(rt, field, stored)
        if changed:
            answers[field.key] = value
    return answers


async def offered_models(rt: Runtime, provider: Any) -> list[str]:
    """What the server says it has, or an empty list when it cannot or will not say.

    A provider without ``list_models`` and a server that refuses the request are the same answer
    here - nothing to choose from - but not the same event, so the second one is reported. It is
    usually the first sign that the key just typed is wrong, and hearing it now beats hearing it
    from the verification call with no idea which of the answers caused it.
    """
    if not hasattr(provider, "list_models"):
        return []
    try:
        return list(await provider.list_models())
    except Exception as exc:  # noqa: BLE001 - the provider already worded the reason
        await rt.frontend.emit("notice", {"text": f"could not list models: {exc}"})
        return []


async def choose_model(rt: Runtime, provider: Any) -> str | None:
    """Pick from what the server offers, or type one in. ``None`` if nothing was chosen."""
    names = await offered_models(rt, provider)
    current = rt.cfg["model"]
    if names:
        options = sorted(names) + [TYPE_IT_IN]
        picked = await rt.frontend.ask("select", f"Which model? (currently {current})",
                                       options=options)
        if picked is None:
            return None
        if picked != TYPE_IT_IN:
            return picked
    answer = await rt.frontend.ask("input", f"Model name [{current}]")
    return (answer or "").strip() or current


async def verify(provider: Any, model: str) -> str | None:
    """One real request. ``None`` when it answered, otherwise why it did not.

    A completion rather than a cheaper probe, because it is the call every turn of every session
    makes: it exercises the URL, the credential, the model name and the wire format together,
    and those are exactly the four things somebody just typed. ``/models`` answering proves the
    first two and says nothing about whether the model name is one this server has.

    The failure is returned as the provider worded it. That wording is already picoagent's own
    for the two cases a fresh install hits - see
    :func:`~picoagent.core.provider.describe_model_failure` - and re-describing it here would
    put a guess in front of the server's actual answer.
    """
    async for event in provider.stream(system="Reply with the single word OK.",
                                       messages=[Message(role="user", text="ping")],
                                       tools=[], model=model, max_tokens=16, thinking="off"):
        if event.type == "error":
            return event.error
    return None


async def confirm_top_level(rt: Runtime, current: dict, wanted: dict) -> dict:
    """The ``model`` / ``provider`` lines to write, once anything they would replace is agreed.

    Same rule as a provider field and asked separately because it is a separate file: these two
    are read straight off the top of config.toml, and a person who has been configuring an
    endpoint has not necessarily agreed to have their default model changed as well.
    """
    keep: dict[str, Any] = {}
    for key, value in wanted.items():
        existing = current.get(key)
        if existing == value:
            continue
        if existing is not None and not await rt.frontend.ask(
                "confirm", f"{key} is already {existing!r} in this file. Replace it with {value!r}?"):
            continue
        keep[key] = value
    return keep


def publish(path: Path, text: str) -> None:
    """Put ``text`` at ``path`` without a moment where the old file is gone and the new is not.

    The trust store's rule, for the same reason it has one: this file is the only record of where
    a key goes, and a truncating write interrupted halfway leaves a config that neither parses
    nor says what it used to. The replacement is written beside it, flushed to disk, and renamed
    over it - atomic on POSIX and on Windows, and on one filesystem because the temp file is in
    the same directory. Whatever fails, the previous config is still there.

    The published file carries ``mkstemp``'s owner-only mode, so there is no window in which the
    key sits in a world-readable file at all. That does mean a config somebody had deliberately
    left read-only comes back writable: this is picoagent creating the file it is publishing, and
    0600 is the mode a file it just wrote a credential into should have.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def report_written(path: Path) -> None:
    """Say which file changed and who can read it, in the mode the filesystem actually reports.

    Read back rather than asserted, because the claim is about the file on disk. On Windows it
    is not made at all: ``chmod`` there sets a read-only flag and no more, and reporting 0600 on
    a filesystem whose access control is ACLs would be a protection picoagent did not obtain.
    """
    if is_windows():
        print(f"picoagent: wrote {path}", file=sys.stderr)
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    print(f"picoagent: wrote {path} (mode {mode:04o}); it can hold an API key, so it is readable "
          "only by you", file=sys.stderr)


async def save(rt: Runtime, name: str, values: dict, model: str) -> int:
    """Write the answers into the user's config.toml, changing nothing else in it."""
    path = Path(rt.cfg["_user_dir"]) / "config.toml"
    text = path.read_text() if path.exists() else ""
    edits: dict[tuple[str, ...], dict[str, Any]] = {}
    try:
        top = await confirm_top_level(rt, _current_top_level(text), {"model": model, "provider": name})
    except TomlEditError as exc:
        await rt.frontend.emit("error", {"text": str(exc)})
        return 1
    if top:
        edits[()] = top
    if values:
        edits[("providers", name)] = values
    if not edits:
        await rt.frontend.emit("notice", {"text": f"nothing to change in {path}"})
        return 0
    try:
        updated = apply_edits(text, edits)
    except TomlEditError as exc:
        await rt.frontend.emit("error", {"text": f"{path} was not changed: {exc}"})
        return 1
    publish(path, updated)
    restrict_to_owner(path)
    report_written(path)
    return 0


def _current_top_level(text: str) -> dict:
    """``model`` and ``provider`` as this file already sets them, if it parses at all."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"this config.toml is not valid TOML, so nothing can be added to "
                            f"it: {exc}") from None
    return {key: document[key] for key in ("model", "provider") if key in document}


async def run(rt: Runtime, rebuild: Callable[[dict], Runtime]) -> int:
    """The whole wizard. ``rebuild`` makes a second runtime from config overrides.

    The second runtime is what makes the verification real rather than a rehearsal. The provider
    in ``rt`` was constructed at load time from the config as it stands, so it still points where
    it pointed before any of these questions were asked; asking *it* to prove the new endpoint
    would prove the old one. For a provider invented in this run it proves more than that: the
    second runtime builds it from the ``dialect`` key that is about to be written, so a wire
    format that does not survive the round trip through config.toml fails here rather than on
    the user's first prompt. Handing the answers back through ``load_config`` and loading the
    plugins again produces the provider a real session would produce, reading the same
    ``[providers.<name>]`` keys this wizard is about to write. If a plugin reads a key by a
    different name than it declared, the verification fails here rather than on the user's first
    prompt.

    Wiring is the caller's, not this module's: building a runtime is what :mod:`picoagent.cli`
    does, and a wizard that imported it back would be a cycle for the sake of six lines.
    """
    chosen = await choose_provider(rt)
    if chosen is None:
        await rt.frontend.emit("error", {"text": "no provider chosen; nothing was written"})
        return 1
    name = chosen.name
    values = {**chosen.values, **await collect_fields(rt, name)}
    merged = {**provider_config(rt.cfg, name), **values}
    second = rebuild({"provider": name, "providers": {name: merged}})
    provider = second.providers.get(name)
    model = await choose_model(second, provider)
    if not model:
        await rt.frontend.emit("error", {"text": "no model chosen; nothing was written"})
        return 1
    failure = await verify(provider, model)
    if failure is None:
        await rt.frontend.emit("notice", {"text": f"{name}/{model} answered."})
    else:
        await rt.frontend.emit("error", {"text": failure})
        if not await rt.frontend.ask("confirm", "Save these settings anyway?"):
            return 1
    return await save(second, name, values, model)
