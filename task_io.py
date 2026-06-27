"""Handoff contract for brains: every brain consumes TaskInput, returns TaskOutput.

This locks the interface so that bridge.py, ClaudeBrain, CodexBrain, HermesBrain
are interchangeable. Adding a new agent = implement a Brain with the same signature.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskInput:
    text: str
    conversation_id: str
    sender_id: str
    sender_nick: str = ""
    is_reset: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskOutput:
    text: str = ""
    session_id: str | None = None
    round: int = 0
    needs_human: bool = False
    approval_link: str | None = None
    error: str | None = None
    agent: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
