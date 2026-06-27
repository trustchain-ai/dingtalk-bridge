"""离线自检:不连钉钉,验证配置/brain/文本提取/session_store。

跑法:  ./.venv/bin/python selftest.py
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# 在 import config 之前注入假凭据,否则 Config.load() 会因没钉钉 key 退出
os.environ.setdefault("CLAUDE_APP_KEY", "test-claude-key")
os.environ.setdefault("CLAUDE_APP_SECRET", "test-claude-secret")
os.environ.setdefault("CODEX_APP_KEY", "test-codex-key")
os.environ.setdefault("CODEX_APP_SECRET", "test-codex-secret")
os.environ.setdefault("HERMES_APP_KEY", "test-hermes-key")
os.environ.setdefault("HERMES_APP_SECRET", "test-hermes-secret")

from brain import (  # noqa: E402
    ClaudeBrain, CodexBrain, EchoBrain, HermesBrain, build_brain,
)
from bridge import _extract_text, _parse_command, _render  # noqa: E402
from config import Config  # noqa: E402
from session_store import SessionStore  # noqa: E402
from task_io import TaskInput, TaskOutput  # noqa: E402


class _FakeMsg:
    def __init__(self, text):
        self.text = text


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_config_parses_multi_robot():
    cfg = Config.load()
    assert "claude" in cfg.agents
    assert "codex" in cfg.agents
    assert cfg.agents["claude"].app_key == "test-claude-key"
    assert cfg.agents["codex"].app_key == "test-codex-key"
    assert "claude" in cfg.active_agents()
    assert "codex" in cfg.active_agents()
    try:
        cfg.brain = "x"  # type: ignore[misc]
        raise AssertionError("Config 应为不可变")
    except Exception:
        pass


def test_allowlist():
    open_cfg = Config(allowed_senders=())
    assert open_cfg.sender_allowed("anyone") is True
    gated = Config(allowed_senders=("u1",))
    assert gated.sender_allowed("u1") is True
    assert gated.sender_allowed("u2") is False
    assert gated.sender_allowed(None) is False


# ---------------------------------------------------------------------------
# brain factory
# ---------------------------------------------------------------------------

def test_echo_brain():
    ti = TaskInput(text="查日志", conversation_id="c1", sender_id="u1")
    out = EchoBrain().run(ti)
    assert "查日志" in out.text
    assert out.agent == "echo"


def test_build_brain_returns_correct_types():
    cfg = Config.load()
    with tempfile.TemporaryDirectory() as td:
        store = SessionStore(Path(td) / "s.json")
        for name in ("claude", "codex", "hermes"):
            b = build_brain(cfg, store, name)
            assert b.name == name


# ---------------------------------------------------------------------------
# text / command parsing
# ---------------------------------------------------------------------------

def test_extract_text():
    assert _extract_text(_FakeMsg({"content": "  hi  "})) == "hi"
    assert _extract_text(_FakeMsg(None)) == ""
    assert _extract_text(_FakeMsg("plain")) == "plain"


def test_parse_command():
    assert _parse_command("") == ("empty", None)
    assert _parse_command("/reset") == ("reset", None)
    assert _parse_command("/reset codex") == ("reset", "codex")
    assert _parse_command("/reset all") == ("reset", "all")
    assert _parse_command("/status") == ("status", None)
    assert _parse_command("hello world") == ("free", None)
    assert _parse_command("/unknown") == ("free", None)


# ---------------------------------------------------------------------------
# session store
# ---------------------------------------------------------------------------

def test_session_store_get_set_reset():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "s.json"
        s = SessionStore(path)
        assert s.get("claude", "c1", "u1") is None
        s.update("claude", "c1", "u1", "sid-A")
        s.update("claude", "c1", "u1", "sid-B")
        ref = s.get("claude", "c1", "u1")
        assert ref is not None
        assert ref.session_id == "sid-B"
        assert ref.round == 2
        # 重置单个
        assert s.reset("claude", "c1", "u1") is True
        assert s.get("claude", "c1", "u1") is None
        # 重置不存在的 key 返回 False
        assert s.reset("claude", "c1", "u1") is False


def test_session_store_reset_all_for():
    with tempfile.TemporaryDirectory() as td:
        s = SessionStore(Path(td) / "s.json")
        s.update("claude", "c1", "u1", "s1")
        s.update("codex", "c1", "u1", "s2")
        s.update("hermes", "c1", "u1", "s3")
        s.update("claude", "c2", "u1", "s4")  # 不同群,不该被清
        killed = s.reset_all_for("c1", "u1")
        assert sorted(killed) == ["claude", "codex", "hermes"]
        assert s.get("claude", "c1", "u1") is None
        assert s.get("codex", "c1", "u1") is None
        assert s.get("claude", "c2", "u1") is not None  # 另一群保留


def test_session_store_persists_across_instances():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "s.json"
        s1 = SessionStore(path)
        s1.update("claude", "c1", "u1", "sid-X")
        s2 = SessionStore(path)
        assert s2.get("claude", "c1", "u1").session_id == "sid-X"


# ---------------------------------------------------------------------------
# reset brain path (不调子进程,直接验证 TaskInput.is_reset 分支)
# ---------------------------------------------------------------------------

def test_reset_via_brain():
    cfg = Config.load()
    with tempfile.TemporaryDirectory() as td:
        store = SessionStore(Path(td) / "s.json")
        # 先有 session
        store.update("claude", "c1", "u1", "old-sid")
        brain = build_brain(cfg, store, "claude")
        ti_reset = TaskInput(text="/reset", conversation_id="c1", sender_id="u1", is_reset=True)
        out = brain.run(ti_reset)
        assert "重置" in out.text
        assert store.get("claude", "c1", "u1") is None


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def test_render_includes_session_and_round():
    o = TaskOutput(text="hi", session_id="abcdef-1234", round=3, agent="claude")
    rendered = _render(o)
    assert "claude" in rendered
    assert "第 3 轮" in rendered
    # 渲染时取 session_id 后 6 位
    assert "1234" in rendered


# ---------------------------------------------------------------------------
# 端到端冒烟(真实调 CLI,可被 SKIP_LIVE=1 跳过)
# ---------------------------------------------------------------------------

def test_codex_brain_live_smoke():
    """真实跑一次 codex exec,验证 --json 解析 + session_id 提取。"""
    if os.environ.get("SKIP_LIVE") == "1":
        print("    (SKIP_LIVE=1,跳过)")
        return
    cfg = Config.load()
    with tempfile.TemporaryDirectory() as td:
        store = SessionStore(Path(td) / "s.json")
        brain = CodexBrain(cfg, store)
        ti = TaskInput(text="回我一个字: 顺", conversation_id="c1", sender_id="u1")
        out = brain.run(ti)
        assert out.error is None, f"codex 跑挂了: {out.error}"
        assert out.session_id is not None, "应该拿到 thread_id"
        # 再发一次,验证 session 续接
        out2 = brain.run(TaskInput(
            text="我刚才让你回什么字?只回这一个字",
            conversation_id="c1", sender_id="u1"))
        assert out2.error is None
        assert out2.session_id == out.session_id, "resume 应返回同 thread_id"
        assert "顺" in out2.text, f"应记得上轮内容,实际: {out2.text!r}"


def test_claude_brain_live_smoke():
    if os.environ.get("SKIP_LIVE") == "1":
        print("    (SKIP_LIVE=1,跳过)")
        return
    cfg = Config.load()
    with tempfile.TemporaryDirectory() as td:
        store = SessionStore(Path(td) / "s.json")
        brain = ClaudeBrain(cfg, store)
        ti = TaskInput(text="回我一个字: 昌", conversation_id="c1", sender_id="u1")
        out = brain.run(ti)
        assert out.error is None, f"claude 跑挂了: {out.error}"
        assert out.session_id is not None
        out2 = brain.run(TaskInput(
            text="我刚才让你回什么字?只回这一个字",
            conversation_id="c1", sender_id="u1"))
        assert out2.error is None
        assert "昌" in out2.text, f"claude 应记得上轮,实际: {out2.text!r}"


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {e}")
            raise
    print(f"\n{passed}/{len(tests)} passed — 离线逻辑 OK。")


if __name__ == "__main__":
    main()
