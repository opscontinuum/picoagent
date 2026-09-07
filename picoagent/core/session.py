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
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterator

from .types import Message, new_id

log = logging.getLogger("picoagent.session")

FORMAT_VERSION = 1


class Session:
    def __init__(self, path: Path, cwd: Path, resume: bool = False):
        """Open (``resume=True``) or create a session file at ``path``."""
        self.path, self.cwd = path, cwd
        self.entries: list[dict] = []
        self.leaf: str | None = None       # id of the newest entry on the active branch
        self.name: str | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
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
        self.path.write_text("".join(f"{line}\n" for line in lines))

    def _write(self, entry: dict) -> dict:
        """Append ``entry`` to memory and disk; advance ``leaf`` for non-header entries."""
        self.entries.append(entry)
        with self.path.open("a") as fh:
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
