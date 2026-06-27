"""Persist per-(conversation, sender, agent) session ids across restarts.

Stored as a single JSON file with a process-wide lock. Each (agent, conv, sender)
has its own session_id and round count. /reset wipes a key.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionRef:
    session_id: str
    round: int


class SessionStore:
    """(agent, conv, sender) -> SessionRef. JSON-file backed, thread-safe."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    @staticmethod
    def _key(agent: str, conv: str, sender: str) -> str:
        return f"{agent}::{conv}::{sender}"

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def get(self, agent: str, conv: str, sender: str) -> SessionRef | None:
        with self._lock:
            v = self._data.get(self._key(agent, conv, sender))
        if not v or not v.get("sid"):
            return None
        return SessionRef(session_id=v["sid"], round=int(v.get("round", 0)))

    def update(self, agent: str, conv: str, sender: str, sid: str) -> int:
        """Save the new session id, bump round, return the new round count."""
        with self._lock:
            k = self._key(agent, conv, sender)
            cur = self._data.get(k, {})
            cur["sid"] = sid
            cur["round"] = int(cur.get("round", 0)) + 1
            self._data[k] = cur
            new_round = cur["round"]
            self._flush()
            return new_round

    def reset(self, agent: str, conv: str, sender: str) -> bool:
        with self._lock:
            k = self._key(agent, conv, sender)
            existed = self._data.pop(k, None) is not None
            if existed:
                self._flush()
            return existed

    def reset_all_for(self, conv: str, sender: str) -> list[str]:
        """Drop every agent's session for this (conv, sender). Return list of agents wiped."""
        with self._lock:
            killed: list[str] = []
            prefix1 = f"::::{conv}::{sender}"   # defensive
            prefix2 = f"::{conv}::{sender}"     # matches _key(agent, conv, sender) suffix
            for k in list(self._data.keys()):
                if k.endswith(prefix2) and not k.endswith(prefix1):
                    agent = k[: -len(prefix2)]
                    self._data.pop(k, None)
                    killed.append(agent)
            if killed:
                self._flush()
            return killed
