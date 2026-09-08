"""The session log: an append-only JSONL file that is the single source of truth.

Every line is an *entry* with an ``id`` and a ``parent`` id, so the file is really a
tree. The *active branch* is the chain from ``leaf`` back to the root. That gives
plugins everything they need for undo/rewind/fork/tree UIs without core changes:
move ``leaf`` to an older entry and keep appending.

Entry kinds
-----------
* ``header``      - written once at creation (cwd, timestamp, format version)
* ``message``     - a :class:`~picoagent.core.types.Message` (user / assistant / tool)
* ``custom``      - plugin state that must survive restarts but is *not* sent to the model
* ``compaction``  - a summary that replaces everything before ``keep_from`` when
                    building the model context (the original entries stay on disk)
* ``shutdown``    - written last when the process leaves by its own exit path; see
                    :meth:`Session.append_shutdown` for what its *absence* means

Unknown kinds are ignored by every reader here - ``messages()``, ``custom()`` and the
compaction lookup all select the kind they want - so a log written by a newer version opens
in an older one rather than breaking it.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any, Iterator

from .types import Message, new_id

log = logging.getLogger("picoagent.session")

FORMAT_VERSION = 1

#: Session logs, and the directories made to hold them, are owner-only. The file is the entire
#: conversation - every prompt, every command the model ran, every tool result, and whatever
#: those results contained - and under the umask it was 0644 inside 0755 directories, which on
#: any host with a second account means every account could read every session. The rest of this
#: codebase already draws the line (credentials opened at 0600, the trust store republished with
#: ``mkstemp``'s owner-only mode, spilled tool output at 0600); the log was the one missed.
#: DISA ASD V6R4 records that as V-222500 and V-222587. It also records V-222444, sensitive data
#: reaching the log at all, which this does not fix: a secret the shell tool hands a command is
#: still written here, to a file only its owner can read.
#:
#: POSIX only. Mode bits are not access control on Windows - NTFS uses ACLs - so the calls below
#: achieve nothing there and this module does not pretend otherwise: see T23 in
#: ``docs/security/threat-model.md`` for the residual and what to do about it by hand.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: The bits :func:`restrict_to_owner` keeps. Group and world go; the owner's own bits are left
#: exactly as they were, so a file somebody deliberately made read-only stays read-only.
OWNER_BITS = 0o700


def _owner_only(path: str, flags: int) -> int:
    """``open`` opener that gives a new session file its mode in the call that creates it.

    Creating under the umask and narrowing afterwards would leave a window - short, but the
    header is written inside it - in which another account can open the file and keep that
    descriptor across the ``chmod``. A mode passed to ``open`` has no window.
    """
    return os.open(path, flags, FILE_MODE)


def make_owner_only_dir(directory: Path) -> None:
    """``mkdir -p`` in which *every* directory created is owner-only, not just the last one.

    ``Path.mkdir(parents=True, mode=...)`` applies ``mode`` to the leaf and lets the parents it
    creates take the umask's, which would leave ``~/.picoagent/sessions`` at 0755 around an
    owner-only project directory. The names in that directory are the project paths this user
    has run the agent in, so the parent's mode is part of the same answer, not a detail.

    Directories that already exist are left alone: this creates, it does not retrofit. Narrowing
    a directory picoagent wrote earlier is a decision about a user's existing files, and it is
    taken where the code knows the directory is picoagent's - :func:`picoagent.cli.harden_session_dir`.
    """
    missing: list[Path] = []
    probe = directory
    while not probe.exists() and probe.parent != probe:
        missing.append(probe)
        probe = probe.parent
    for parent in reversed(missing):
        parent.mkdir(mode=DIR_MODE, exist_ok=True)


def restrict_to_owner(path: Path) -> bool:
    """Take group and world access off an existing file or directory. True if that changed it.

    A log written before this rule existed is still the file the next turn is appended to, so a
    resume narrows it rather than going on writing the conversation into a world-readable file.

    Only the group and other bits are cleared. Widening nothing and preserving the owner's own
    bits keeps this from being a mode rewrite - it removes exactly the access the finding is
    about and touches nothing else.

    A path this user cannot ``chmod`` (someone else's file reached through ``-r``) is reported
    rather than raised: refusing to open a session over a permissions error would be a worse
    answer than saying which file could not be protected.
    """
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current == current & OWNER_BITS:
            return False
        os.chmod(path, current & OWNER_BITS)
    except OSError as exc:
        log.warning("could not restrict %s to its owner (%s); other accounts may be able to "
                    "read it", path, exc)
        return False
    return True


class Session:
    def __init__(self, path: Path, cwd: Path, resume: bool = False):
        """Open (``resume=True``) or create a session file at ``path``."""
        self.path, self.cwd = path, cwd
        self.entries: list[dict] = []
        self.leaf: str | None = None       # id of the newest entry on the active branch
        self.name: str | None = None
        make_owner_only_dir(path.parent)
        if path.exists():
            # Before anything is read or appended: from here on this file grows a conversation,
            # and one written under an older umask is still 0644 until something narrows it.
            restrict_to_owner(path)
        if resume and path.exists():
            self._load()
        if not self.entries:
            # Also the answer for a resume that found nothing: a file whose only line was the
            # one an interrupted write tore, or an empty one. Every session log opens with a
            # header - it is what says the file is a session at all - so a resume that recovered
            # no entries starts one rather than leaving a file nothing will recognise later.
            self._write({"kind": "header", "id": new_id(), "parent": None, "cwd": str(cwd),
                         "created": time.time(), "version": FORMAT_VERSION})

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        """Read the entries back, dropping a last line an interrupted append left behind.

        ``-r`` is wanted most after a crash, which is exactly when the file ends mid-line: the
        log is appended to one entry at a time, so an interrupted write leaves a prefix of the
        last one and nothing else wrong. Letting ``json`` raise there ended the resume with a
        traceback at the moment the user was trying to get their conversation back.

        The torn line is *removed* rather than kept and ignored. The next append would otherwise
        write onto the end of it, gluing a good entry to a broken one and turning the ordinary
        crash shape into the fatal one below - a line that will not parse in the middle of the
        file. Those bytes can never become an entry, so nothing is lost by dropping them, and
        the user is told which file lost how much.

        A line that will not parse anywhere but the end is not that failure. Every entry names
        its parent, so a hole in the middle silently shortens the branch: the session would open
        on a history missing everything before the hole and go on to send it to the model as if
        it were whole. That is somebody's conversation being quietly rewritten, so it stops the
        session with a sentence instead - the same answer this codebase gives for the user's own
        config file, and for the same reason. Starting without ``-r`` still opens a new session.
        """
        lines = self.path.read_text().splitlines()
        # The last line with anything on it, not the last line: a blank one at the end is not an
        # entry, and counting it would read the torn line as a hole in the middle.
        last = max((number for number, line in enumerate(lines, start=1) if line.strip()), default=0)
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                self.entries.append(json.loads(line))
            except ValueError as exc:
                if number != last:
                    raise SystemExit(
                        f"{self.path} is damaged: line {number} of {len(lines)} is not a session "
                        f"entry ({exc}). Entries name their parents, so continuing would resume a "
                        "conversation with a hole in it and send that to the model. Repair or "
                        "move the file aside; a run without -r starts a new session.") from None
                log.warning("%s ends in a partial entry (%d bytes), which is what an interrupted "
                            "write leaves; dropping it and resuming from the entry before it.",
                            self.path, len(line))
                self._truncate_to(lines[:number - 1])
        if self.entries:
            self.leaf = self.entries[-1]["id"]

    def _truncate_to(self, lines: list[str]) -> None:
        """Rewrite the file as ``lines``, so the next append starts on a line of its own."""
        with open(self.path, "w", opener=_owner_only) as fh:
            fh.write("".join(f"{line}\n" for line in lines))

    def _write(self, entry: dict) -> dict:
        """Append ``entry`` to memory and disk; advance ``leaf`` for non-header entries."""
        self.entries.append(entry)
        # ``open`` rather than ``Path.open``: pathlib's does not take an opener, and the opener
        # is what puts the mode on the file that creates it. Every append goes through here, so
        # there is no second path that could create the log under the umask instead.
        with open(self.path, "a", opener=_owner_only) as fh:
            fh.write(json.dumps(entry) + "\n")
        if entry["kind"] != "header":
            self.leaf = entry["id"]
        return entry

    def _entry(self, kind: str, **fields: Any) -> dict:
        return self._write({"kind": kind, "id": new_id(), "parent": self.leaf, **fields})

    # ------------------------------------------------------------------ appending
    def append_message(self, message: Message) -> dict:
        return self._entry("message", message=message.to_dict())

    def append_custom(self, custom_type: str, data: Any) -> dict:
        """Persist plugin state. Never reaches the model."""
        return self._entry("custom", custom_type=custom_type, data=data)

    def append_shutdown(self, reason: str = "completed") -> dict:
        """Record that the process left by its own exit path, and when.

        **What this guarantees is narrower than it looks, and reading it as a guarantee gets
        every crashed session wrong.** The entry is appended from ``run_agent``'s exit path, so
        it says one thing: control reached that path and the write succeeded. A ``SIGKILL``, a
        power loss, or an interpreter that died cannot write anything by definition, and a
        session still running has not written it yet. So:

        * entry present  -> the session ended, and ``reason`` says how *control left*, not
          whether the work went well: ``completed`` for a prompt or a REPL that returned
          normally - a run whose model call failed returned normally and says ``completed``,
          because the exit code is where that verdict lives - and ``interrupted`` for an exit
          an exception carried out, Ctrl-C included;
        * entry absent   -> the session is still running, or it ended in a way that could not
          be recorded. The record cannot tell those two apart, and neither can a reader.

        That absence is the whole forensic value: before this entry existed, a clean exit and a
        kill left identical files. Resuming changes nothing about how a session opens - entries
        are appended after this one either way - but it does leave the difference visible in the
        record, because conversation after a shutdown entry is a session that was reopened and
        conversation with no shutdown entry behind it is one that never got to write one.
        """
        return self._entry("shutdown", reason=reason, ended=time.time())

    def append_compaction(self, summary: str, keep_from: str | None, tokens_before: int = 0) -> dict:
        """Record a summary; :meth:`messages` will use it in place of entries before ``keep_from``."""
        return self._entry("compaction", summary=summary, keep_from=keep_from, tokens_before=tokens_before)

    # ------------------------------------------------------------------ reading
    def branch(self) -> list[dict]:
        """Entries on the active branch, root first."""
        by_id = {e["id"]: e for e in self.entries}
        chain, current = [], self.leaf
        while current and current in by_id:
            chain.append(by_id[current])
            current = by_id[current]["parent"]
        return list(reversed(chain))

    def messages(self) -> list[Message]:
        """Model-facing history: active branch with the newest compaction applied."""
        branch = self.branch()
        compaction = next((e for e in reversed(branch) if e["kind"] == "compaction"), None)
        history: list[Message] = []
        if compaction:
            history.append(Message(role="user", text=f"[Conversation summary]\n{compaction['summary']}",
                                   meta={"compaction": True}))
            start = next((i for i, e in enumerate(branch) if e["id"] == compaction["keep_from"]), len(branch))
            branch = branch[start:]
        history.extend(Message.from_dict(e["message"]) for e in branch if e["kind"] == "message")
        return history

    def custom(self, custom_type: str) -> Iterator[dict]:
        """Plugin entries of one type on the active branch."""
        return (e for e in self.branch() if e["kind"] == "custom" and e["custom_type"] == custom_type)

    def set_leaf(self, entry_id: str) -> None:
        """Move the branch pointer (rewind / fork). New entries will hang off ``entry_id``."""
        self.leaf = entry_id

    @staticmethod
    def list(directory: Path) -> list[Path]:
        """Session files in ``directory``, newest first."""
        if not directory.exists():
            return []
        return sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
