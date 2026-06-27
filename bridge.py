"""DingTalk <-> Claude/Codex/Hermes bridge.

Each agent (claude / codex / hermes) has its own DingTalk bot with its own
AppKey/AppSecret. We spin up one DingTalkStreamClient per bot in a separate
thread, all sharing a single SessionStore and the same handler logic.

Commands (in any group, in any bot's @-mention):
  <free text>            -> routed to the bot that received it
  /reset                 -> wipe this (conv, sender) session for THIS bot's agent
  /reset all             -> wipe this (conv, sender) session for ALL agents
  /status                -> show session state for all agents in this (conv, sender)
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading

import dingtalk_stream
from dingtalk_stream import (
    AckMessage,
    ChatbotHandler,
    ChatbotMessage,
    Credential,
    DingTalkStreamClient,
)

from brain import build_brain
from config import AgentConfig, Config
from reactions import DONE, FAILED, THINKING, Reactor
from session_store import SessionStore
from task_io import TaskInput, TaskOutput

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("dingtalk-bridge")

# Commands ----------------------------------------------------------------

_RESET_RE = re.compile(r"^/reset(?:\s+(\S+))?\s*$", re.IGNORECASE)
_STATUS_RE = re.compile(r"^/status\s*$", re.IGNORECASE)


# Text extraction --------------------------------------------------------

def _extract_text(incoming: ChatbotMessage) -> str:
    content = incoming.text
    if content is None:
        return ""
    # dingtalk_stream wraps plain text in a TextContent object (has .content).
    inner = getattr(content, "content", None)
    if isinstance(inner, str):
        return inner.strip()
    if isinstance(content, dict):
        return (content.get("content") or "").strip()
    return str(content).strip()


# Command parsing --------------------------------------------------------

def _parse_command(text: str) -> tuple[str, str | None]:
    """Return (kind, arg). kind in {reset, status, free, empty}."""
    if not text:
        return ("empty", None)
    if (m := _RESET_RE.match(text)):
        return ("reset", (m.group(1) or "").lower() or None)
    if _STATUS_RE.match(text):
        return ("status", None)
    return ("free", None)


# Render -----------------------------------------------------------------

def _render(o: TaskOutput) -> str:
    if o.error:
        body = f"```\n{o.error[:1500]}\n```"
    else:
        body = o.text or "(空响应)"
    body = body[:3500]

    if o.session_id:
        header = f"🤖 **{o.agent}** · sid=…{o.session_id[-6:]} · 第 {o.round} 轮"
    else:
        header = f"🤖 **{o.agent}**"

    foot_lines = ["🔧 `/reset` · `/reset all` · `/status`"]
    if o.needs_human and o.approval_link:
        foot_lines.insert(0, f"⚠️ 需确认: {o.approval_link}")
    foot = "\n".join(foot_lines)

    return f"{header}\n\n---\n{body}\n\n---\n{foot}"


# AgentBot: one per DingTalk robot ----------------------------------------

class AgentBot(ChatbotHandler):
    """Handles inbound @-mentions for a single DingTalk robot (== single agent)."""

    def __init__(self, cfg: Config, store: SessionStore, agent_name: str):
        super().__init__()
        self._cfg = cfg
        self._store = store
        self._agent_name = agent_name
        self._brain = build_brain(cfg, store, agent_name)
        agent_cfg = cfg.agents.get(agent_name)
        self._reactor = (
            Reactor(agent_cfg.app_key, agent_cfg.app_secret, agent_cfg.app_key)
            if agent_cfg else None
        )

    @property
    def agent(self) -> str:
        return self._agent_name

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        incoming = ChatbotMessage.from_dict(callback.data)
        text = _extract_text(incoming)
        who = incoming.sender_nick or incoming.sender_staff_id or "unknown"
        logger.info(
            "agent=%s robot=%s from=%s staff=%s conv=%r text=%r",
            self._agent_name, incoming.robot_code, who, incoming.sender_staff_id,
            incoming.conversation_title, text,
        )

        if not text:
            self.reply_text("请在 @我 时附上要执行的任务文本。", incoming)
            return AckMessage.STATUS_OK, "OK"

        if not self._cfg.sender_allowed(incoming.sender_staff_id):
            logger.warning("拒绝未授权发送者 staff=%s agent=%s",
                           incoming.sender_staff_id, self._agent_name)
            self.reply_text("⛔ 你不在允许列表(ALLOWED_SENDERS)中,无法触发任务。", incoming)
            return AckMessage.STATUS_OK, "OK"

        kind, arg = _parse_command(text)

        # ---- /status: synchronous read, no offload ----
        if kind == "status":
            self._handle_status(incoming)
            return AckMessage.STATUS_OK, "OK"

        # ---- /reset: synchronous mutation ----
        if kind == "reset":
            self._handle_reset(incoming, arg)
            return AckMessage.STATUS_OK, "OK"

        # ---- free text: offload to brain ----
        # 进度由 🤔/🥳 表情体现;仅当表情不可用时才回退到文字提示。
        if not (self._reactor and self._reactor.enabled):
            self.reply_markdown(
                f"🤖 {self._agent_name} 开始执行",
                "已收到,正在处理,请稍等…", incoming,
            )

        ti = TaskInput(
            text=text,
            conversation_id=incoming.conversation_id or "",
            sender_id=incoming.sender_staff_id or "anon",
            sender_nick=incoming.sender_nick or "",
        )

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self._run_and_reply, ti, incoming)
        future.add_done_callback(_log_future_error)
        return AckMessage.STATUS_OK, "OK"

    # ---- sync handlers (run inside the asyncio thread, fast) ------------

    def _handle_status(self, incoming: ChatbotMessage) -> None:
        conv = incoming.conversation_id or ""
        sender = incoming.sender_staff_id or "anon"
        lines = []
        for name in ("claude", "codex", "hermes"):
            ref = self._store.get(name, conv, sender)
            if ref is None:
                lines.append(f"- **{name}**: (无 session)")
            else:
                lines.append(f"- **{name}**: sid=…{ref.session_id[-6:]} 第 {ref.round} 轮")
        self.reply_markdown("📊 当前会话状态", "\n".join(lines), incoming)

    def _handle_reset(self, incoming: ChatbotMessage, target: str | None) -> None:
        conv = incoming.conversation_id or ""
        sender = incoming.sender_staff_id or "anon"
        if target in (None, self._agent_name):
            existed = self._store.reset(self._agent_name, conv, sender)
            body = (
                f"🔄 **{self._agent_name}** 会话已重置"
                f"(原来{'有' if existed else '没有'}历史)。"
            )
        elif target == "all":
            killed = self._store.reset_all_for(conv, sender)
            body = "🔄 全部 agent 会话已重置。\n" + (
                "\n".join(f"- {a}" for a in killed) if killed else "_(本就没有历史会话)_"
            )
        else:
            body = (f"ℹ️ 当前是 **{self._agent_name}-bot**,无法重置 `{target}`。"
                    f" 请到对应 bot 发 `/reset`,或发 `/reset all` 重置全部。")
        self.reply_markdown("重置", body, incoming)

    # ---- async handler (offloaded to a worker thread) ------------------

    def _run_and_reply(self, ti: TaskInput, incoming: ChatbotMessage) -> None:
        msg_id = incoming.message_id or ""
        conv_id = incoming.conversation_id or ""
        if self._reactor:
            self._reactor.reply(THINKING, msg_id, conv_id)
        try:
            output = self._brain.run(ti)
        except Exception as exc:
            logger.exception("brain 执行异常 agent=%s", self._agent_name)
            output = TaskOutput(
                text=f"❌ 执行出错: {exc}",
                agent=self._agent_name,
                error=str(exc),
            )
        if self._reactor:
            self._reactor.recall(THINKING, msg_id, conv_id)
            self._reactor.reply(FAILED if output.error else DONE, msg_id, conv_id)
        body = _render(output)
        self.reply_markdown(f"✅ {self._agent_name}", body, incoming)


def _log_future_error(fut) -> None:
    if exc := fut.exception():
        logger.error("线程内未捕获异常: %s", exc, exc_info=exc)


# Multi-robot bootstrap ---------------------------------------------------

def _start_one_bot(agent_name: str, agent_cfg: AgentConfig,
                   global_cfg: Config, store: SessionStore,
                   ready_evt: threading.Event) -> threading.Thread:
    """Start a single DingTalkStreamClient in its own thread."""

    def runner() -> None:
        try:
            cred = Credential(agent_cfg.app_key, agent_cfg.app_secret)
            client = DingTalkStreamClient(cred)
            bot = AgentBot(global_cfg, store, agent_name)
            client.register_callback_handler(ChatbotMessage.TOPIC, bot)
            logger.info("[%s] DingTalk Stream 已连接", agent_name)
            ready_evt.set()
            client.start_forever()
        except Exception:
            logger.exception("[%s] DingTalk Stream 启动失败", agent_name)
            ready_evt.set()

    t = threading.Thread(target=runner, name=f"dingtalk-{agent_name}", daemon=True)
    t.start()
    return t


def main() -> None:
    cfg = Config.load()

    active = cfg.active_agents()
    if not active:
        raise SystemExit(
            "No agent has both APP_KEY and APP_SECRET set.\n"
            "Configure at least one of: CLAUDE_/CODEX_/HERMES_ APP_KEY + APP_SECRET."
        )
    logger.info("启用的 agent 机器人: %s", active)
    logger.info("默认 agent: %s", cfg.default_agent)
    logger.info("Brain 模式: %s", cfg.brain)
    logger.info("Session 存储: %s", cfg.session_store_path)

    store = SessionStore(cfg.session_store_path)

    threads: list[threading.Thread] = []
    for name in active:
        evt = threading.Event()
        t = _start_one_bot(name, cfg.agents[name], cfg, store, evt)
        threads.append(t)
        if not evt.wait(timeout=10):
            logger.error("[%s] 10 秒内未就绪,继续运行但可能连接失败", name)

    logger.info("主线程空转,Ctrl-C 退出。")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        logger.info("Bye")


if __name__ == "__main__":
    main()
