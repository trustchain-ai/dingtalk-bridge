"""Task brains: turn a TaskInput into a TaskOutput.

- EchoBrain     : proves inbound pipe without invoking any agent
- ClaudeBrain   : `claude -p` headless; --resume on persisted session_id
- CodexBrain    : `codex exec` headless; --resume on thread_id from --json
- HermesBrain   : `hermes -z` headless; per-(conv,sender) session name

Every brain implements the same `run(TaskInput) -> TaskOutput` signature
and persists its own session state via the shared SessionStore.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Protocol

from config import Config
from session_store import SessionStore
from task_io import TaskInput, TaskOutput


class Brain(Protocol):
    name: str

    def run(self, ti: TaskInput) -> TaskOutput: ...


# ---------------------------------------------------------------------------
# EchoBrain
# ---------------------------------------------------------------------------

class EchoBrain:
    """Milestone brain: echoes the task so the inbound pipe is verified."""

    name = "echo"

    def run(self, ti: TaskInput) -> TaskOutput:
        return TaskOutput(
            text=(
                "✅ 入站已打通,收到任务:\n\n"
                f"> {ti.text}\n\n"
                "_(当前 echo 模式;在 .env 设 `BRIDGE_BRAIN=claude` 接入真实执行)_"
            ),
            agent="echo",
        )


# ---------------------------------------------------------------------------
# ClaudeBrain
# ---------------------------------------------------------------------------

class ClaudeBrain:
    """Run the task through `claude -p` headless mode with --resume session reuse."""

    name = "claude"

    def __init__(self, cfg: Config, store: SessionStore, agent_name: str = "claude"):
        self._cfg = cfg
        self._store = store
        agent_cfg = cfg.agents.get(agent_name)
        if agent_cfg is None:
            raise ValueError(f"Config has no agent '{agent_name}'")
        self._bin = agent_cfg.bin
        self._cwd = agent_cfg.cwd
        self._timeout = agent_cfg.timeout
        self._permission_mode = os.environ.get("CLAUDE_PERMISSION_MODE", "default").strip()

    def run(self, ti: TaskInput) -> TaskOutput:
        if ti.is_reset:
            return self._do_reset(ti)
        return self._do_run(ti)

    def _do_reset(self, ti: TaskInput) -> TaskOutput:
        existed = self._store.reset(self.name, ti.conversation_id, ti.sender_id)
        return TaskOutput(
            text=(
                f"🔄 **{self.name}** 会话已重置(原来{'有' if existed else '没有'}历史)。"
                f" 下次提问将开启全新上下文。"
            ),
            agent=self.name,
        )

    def _do_run(self, ti: TaskInput) -> TaskOutput:
        ref = self._store.get(self.name, ti.conversation_id, ti.sender_id)
        cmd = [self._bin]
        if ref is not None:
            cmd += ["--resume", ref.session_id]
        cmd += [
            "-p", ti.text,
            "--output-format", "json",
            "--permission-mode", self._permission_mode,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=self._cwd, capture_output=True, text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return TaskOutput(text=f"⏱️ **{self.name}** 任务超时 (>{self._timeout}s)。",
                              agent=self.name, error="timeout")
        except FileNotFoundError:
            return TaskOutput(text=f"❌ 找不到 {self.name} 可执行文件: {self._bin}",
                              agent=self.name, error="not_found")

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            # If --resume failed because the session expired, drop it and retry once.
            if ref is not None and "session" in err.lower():
                self._store.reset(self.name, ti.conversation_id, ti.sender_id)
                return self._do_run(ti)
            return TaskOutput(
                text=f"❌ **{self.name}** 执行失败 (exit {proc.returncode}):\n{err[:1000]}",
                agent=self.name, error=err[:1000],
            )

        out, sid = self._parse_result(proc.stdout)
        if sid:
            new_round = self._store.update(self.name, ti.conversation_id, ti.sender_id, sid)
        else:
            new_round = (ref.round if ref else 0) + 1
        return TaskOutput(text=out, session_id=sid, round=new_round, agent=self.name)

    @staticmethod
    def _parse_result(stdout: str) -> tuple[str, str | None]:
        stdout = stdout.strip()
        if not stdout:
            return "(Claude 返回空结果)", None
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout, None
        if isinstance(data, dict):
            sid = data.get("session_id")
            text = data.get("result") or data.get("text") or stdout
            return str(text), sid
        return stdout, None


# ---------------------------------------------------------------------------
# CodexBrain
# ---------------------------------------------------------------------------

class CodexBrain:
    """Run via `codex exec` headless. Reuses sessions by `codex exec resume <thread_id>`.

    The thread_id is the very first line of `codex exec --json` output:
        {"type":"thread.started","thread_id":"019ef85c-..."}
    It is also the filename stem under ~/.codex/sessions/YYYY/MM/DD/rollout-...jsonl
    so we have two independent ways to recover it.
    """

    name = "codex"

    def __init__(self, cfg: Config, store: SessionStore, agent_name: str = "codex"):
        self._cfg = cfg
        self._store = store
        agent_cfg = cfg.agents.get(agent_name)
        if agent_cfg is None:
            raise ValueError(f"Config has no agent '{agent_name}'")
        self._bin = agent_cfg.bin
        self._cwd = agent_cfg.cwd
        self._timeout = agent_cfg.timeout

    def run(self, ti: TaskInput) -> TaskOutput:
        if ti.is_reset:
            return self._do_reset(ti)
        return self._do_run(ti)

    def _do_reset(self, ti: TaskInput) -> TaskOutput:
        existed = self._store.reset(self.name, ti.conversation_id, ti.sender_id)
        return TaskOutput(
            text=(
                f"🔄 **{self.name}** 会话已重置(原来{'有' if existed else '没有'}历史)。"
                f" 下次提问将开启全新上下文。"
            ),
            agent=self.name,
        )

    def _do_run(self, ti: TaskInput) -> TaskOutput:
        ref = self._store.get(self.name, ti.conversation_id, ti.sender_id)

        base = [self._bin, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--json"]
        if ref is not None:
            cmd = base + ["resume", ref.session_id, ti.text]
        else:
            cmd = base + [ti.text]

        try:
            proc = subprocess.run(
                cmd, cwd=self._cwd, capture_output=True, text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return TaskOutput(text=f"⏱️ **{self.name}** 任务超时 (>{self._timeout}s)。",
                              agent=self.name, error="timeout")
        except FileNotFoundError:
            return TaskOutput(text=f"❌ 找不到 {self.name} 可执行文件: {self._bin}",
                              agent=self.name, error="not_found")

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            # Resume may fail if the original session was deleted; recover by dropping.
            if ref is not None and ("session" in err.lower() or "not found" in err.lower()):
                self._store.reset(self.name, ti.conversation_id, ti.sender_id)
                return self._do_run(ti)
            return TaskOutput(
                text=f"❌ **{self.name}** 执行失败 (exit {proc.returncode}):\n{err[:1000]}",
                agent=self.name, error=err[:1000],
            )

        text, sid = self._extract_thread(proc.stdout, fallback_cwd=self._cwd)
        if sid:
            new_round = self._store.update(self.name, ti.conversation_id, ti.sender_id, sid)
        else:
            new_round = (ref.round if ref else 0) + 1
        return TaskOutput(text=text, session_id=sid, round=new_round, agent=self.name)

    @staticmethod
    def _extract_thread(stdout: str, fallback_cwd: str) -> tuple[str, str | None]:
        """Parse --json JSONL stream. First `thread.started` line has thread_id;
        last `item.completed` agent_message carries the final reply."""
        if not stdout.strip():
            return "(Codex 返回空结果)", None

        thread_id: str | None = None
        last_text: str = ""

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "thread.started":
                thread_id = evt.get("thread_id") or thread_id
            elif etype == "item.completed":
                item = evt.get("item") or {}
                if item.get("type") == "agent_message":
                    t = item.get("text")
                    if isinstance(t, str) and t.strip():
                        last_text = t
            elif etype == "turn.completed":
                pass

        if not last_text and not thread_id:
            return stdout, None
        return (last_text or "(Codex 没说啥)"), thread_id


# ---------------------------------------------------------------------------
# HermesBrain (placeholder; hermes session resume is not reliable yet)
# ---------------------------------------------------------------------------

class HermesBrain:
    """Run via `hermes -z`. Session name is derived from (conv, sender).

    NOTE: hermes's --continue/--resume continuity is currently unstable
    (verified 2026-06-24). Until that is fixed, this brain runs stateless:
    every invocation starts a fresh hermes session named after the user.
    """

    name = "hermes"

    def __init__(self, cfg: Config, store: SessionStore, agent_name: str = "hermes"):
        self._cfg = cfg
        self._store = store
        agent_cfg = cfg.agents.get(agent_name)
        if agent_cfg is None:
            raise ValueError(f"Config has no agent '{agent_name}'")
        self._bin = agent_cfg.bin
        self._cwd = agent_cfg.cwd
        self._timeout = agent_cfg.timeout

    def run(self, ti: TaskInput) -> TaskOutput:
        if ti.is_reset:
            return self._do_reset(ti)
        return self._do_run(ti)

    def _do_reset(self, ti: TaskInput) -> TaskOutput:
        existed = self._store.reset(self.name, ti.conversation_id, ti.sender_id)
        return TaskOutput(
            text=(
                f"🔄 **{self.name}** 会话已重置(原来{'有' if existed else '没有'}历史)。"
                f" 下次提问将开启全新上下文。"
            ),
            agent=self.name,
        )

    def _do_run(self, ti: TaskInput) -> TaskOutput:
        ref = self._store.get(self.name, ti.conversation_id, ti.sender_id)
        if ref is None:
            session_name = f"dingtalk_{_safe(ti.conversation_id)}_{_safe(ti.sender_id)}"
            self._store.update(self.name, ti.conversation_id, ti.sender_id, session_name)
            ref = self._store.get(self.name, ti.conversation_id, ti.sender_id)
        assert ref is not None
        cmd = [self._bin, "-z", ti.text, "--continue", ref.session_id]
        try:
            proc = subprocess.run(
                cmd, cwd=self._cwd, capture_output=True, text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return TaskOutput(text=f"⏱️ **{self.name}** 任务超时 (>{self._timeout}s)。",
                              agent=self.name, error="timeout")
        except FileNotFoundError:
            return TaskOutput(text=f"❌ 找不到 {self.name} 可执行文件: {self._bin}",
                              agent=self.name, error="not_found")

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return TaskOutput(
                text=f"❌ **{self.name}** 执行失败 (exit {proc.returncode}):\n{err[:1000]}",
                agent=self.name, error=err[:1000],
            )
        return TaskOutput(text=proc.stdout.strip(),
                          session_id=ref.session_id,
                          round=ref.round + 1,
                          agent=self.name)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:32] or "anon"


# ---------------------------------------------------------------------------
# Brain factory
# ---------------------------------------------------------------------------

def build_brain(cfg: Config, store: SessionStore, agent_name: str) -> Brain:
    if cfg.brain == "echo":
        return EchoBrain()
    if agent_name == "claude":
        return ClaudeBrain(cfg, store)
    if agent_name == "codex":
        return CodexBrain(cfg, store)
    if agent_name == "hermes":
        return HermesBrain(cfg, store)
    raise ValueError(f"Unknown agent: {agent_name}")
