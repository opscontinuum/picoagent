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
import time
from pathlib import Path
from typing import Any, Iterator

from .types import Message, new_id

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
        else:
            self._write({"kind": "header", "id": new_id(), "parent": None, "cwd": str(cwd),
                         "created": time.time(), "version": FORMAT_VERSION})

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        for line in self.path.read_text().splitlines():
            if line.strip():
                self.entries.append(json.loads(line))
        if self.entries:
            self.leaf = self.entries[-1]["id"]

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
