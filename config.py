"""Configuration for the DingTalk <-> Claude/Codex/Hermes bridge.

Supports multiple robots, one per agent:
- CLAUDE_APP_KEY / CLAUDE_APP_SECRET -> claude-bot
- CODEX_APP_KEY  / CODEX_APP_SECRET  -> codex-bot
- HERMES_APP_KEY / HERMES_APP_SECRET -> hermes-bot (optional)

If only one pair is set (DINGTALK_APP_KEY / DINGTALK_APP_SECRET), the bridge
falls back to single-robot mode where that one bot controls `default_agent`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"

_VALID_BRAINS = ("echo", "claude")
_VALID_AGENTS = ("claude", "codex", "hermes")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _agent_creds(agent: str) -> tuple[str, str] | None:
    """Return (app_key, app_secret) for the agent, or None if not configured."""
    ak = os.environ.get(f"{agent.upper()}_APP_KEY", "").strip()
    sk = os.environ.get(f"{agent.upper()}_APP_SECRET", "").strip()
    if ak and sk:
        return ak, sk
    return None


@dataclass(frozen=True)
class AgentConfig:
    app_key: str
    app_secret: str
    bin: str
    cwd: str
    timeout: int = 600
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """Immutable bridge configuration."""

    # Multi-robot table: agent_name -> AgentConfig
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    # Single-robot fallback
    app_key: str = ""
    app_secret: str = ""

    # Default agent for the single-robot path
    default_agent: str = "claude"

    brain: str = "echo"  # echo | claude  (echo forces single-robot no-op mode)
    allowed_senders: tuple[str, ...] = ()
    max_reply_chars: int = 3500
    session_store_path: Path = field(default_factory=lambda: Path("./.sessions.json"))

    @staticmethod
    def load() -> "Config":
        _load_dotenv(ENV_PATH)

        senders = tuple(
            s.strip() for s in os.environ.get("ALLOWED_SENDERS", "").split(",") if s.strip()
        )

        brain = os.environ.get("BRIDGE_BRAIN", "echo").strip().lower()
        if brain not in _VALID_BRAINS:
            raise SystemExit(f"BRIDGE_BRAIN must be one of {_VALID_BRAINS}, got '{brain}'")

        cwd = os.environ.get("AGENT_CWD", "/Users/lihu/WorkSpace/ai").strip()

        agents: dict[str, AgentConfig] = {}

        # Claude
        if creds := _agent_creds("claude"):
            agents["claude"] = AgentConfig(
                app_key=creds[0], app_secret=creds[1],
                bin=os.environ.get("CLAUDE_BIN", "/opt/homebrew/bin/claude").strip(),
                cwd=cwd,
                timeout=int(os.environ.get("CLAUDE_TIMEOUT", "600")),
                extra_args=(),
            )
        elif os.environ.get("CLAUDE_BIN"):
            agents["claude"] = AgentConfig(
                app_key="", app_secret="",
                bin=os.environ.get("CLAUDE_BIN", "/opt/homebrew/bin/claude").strip(),
                cwd=cwd,
                timeout=int(os.environ.get("CLAUDE_TIMEOUT", "600")),
            )

        # Codex
        if creds := _agent_creds("codex"):
            agents["codex"] = AgentConfig(
                app_key=creds[0], app_secret=creds[1],
                bin=os.environ.get("CODEX_BIN", "/opt/homebrew/bin/codex").strip(),
                cwd=cwd,
                timeout=int(os.environ.get("CODEX_TIMEOUT", "600")),
            )

        # Hermes
        if creds := _agent_creds("hermes"):
            agents["hermes"] = AgentConfig(
                app_key=creds[0], app_secret=creds[1],
                bin=os.environ.get("HERMES_BIN", "/Users/lihu/.local/bin/hermes").strip(),
                cwd=cwd,
                timeout=int(os.environ.get("HERMES_TIMEOUT", "600")),
            )

        # Fallback: legacy DINGTALK_APP_KEY / DINGTALK_APP_SECRET pair -> claude
        legacy_ak = os.environ.get("DINGTALK_APP_KEY", "").strip()
        legacy_sk = os.environ.get("DINGTALK_APP_SECRET", "").strip()
        if legacy_ak and legacy_sk and "claude" not in agents:
            agents["claude"] = AgentConfig(
                app_key=legacy_ak, app_secret=legacy_sk,
                bin=os.environ.get("CLAUDE_BIN", "/opt/homebrew/bin/claude").strip(),
                cwd=cwd,
                timeout=int(os.environ.get("CLAUDE_TIMEOUT", "600")),
            )

        default_agent = os.environ.get("DEFAULT_AGENT", "claude").strip().lower()
        if default_agent not in _VALID_AGENTS:
            raise SystemExit(f"DEFAULT_AGENT must be one of {_VALID_AGENTS}, got '{default_agent}'")

        return Config(
            agents=agents,
            app_key=legacy_ak,
            app_secret=legacy_sk,
            default_agent=default_agent,
            brain=brain,
            allowed_senders=senders,
            max_reply_chars=int(os.environ.get("MAX_REPLY_CHARS", "3500")),
            session_store_path=Path(
                os.environ.get("SESSION_STORE_PATH", str(ENV_PATH.parent / ".sessions.json"))
            ).expanduser().resolve(),
        )

    # --- queries ---------------------------------------------------------

    def sender_allowed(self, staff_id: str | None) -> bool:
        if not self.allowed_senders:
            return True
        return (staff_id or "") in self.allowed_senders

    def active_agents(self) -> list[str]:
        """Agents that have a fully configured DingTalk credential pair."""
        return [a for a, c in self.agents.items() if c.app_key and c.app_secret]
