# DingTalk Bridge · 钉钉 AI 机器人桥

> Bridge any **DingTalk group @-mention** to a local **Claude Code** or **Codex CLI** session — no public IP, no webhook callback, just DingTalk's Stream WebSocket.

把钉钉群里 `@机器人` 的消息转发到本机的 **Claude Code / Codex CLI**(可选 Hermes),把 AI 回复发回群里。
**入站走钉钉官方 Stream 模式(WebSocket),不需要公网 IP / webhook 回调地址。**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green)]()
[![DingTalk Stream](https://img.shields.io/badge/dingtalk--stream-%E2%89%A50.24-orange)]()

---

## Features / 功能

- 🤖 **One bot per agent** — Claude, Codex, Hermes each get their own DingTalk robot (own AppKey/Secret). `@claude-bot` runs Claude, `@codex-bot` runs Codex.
- 🔁 **Session continuity** — per-(conversation, sender, agent) sessions are persisted as JSON and resumed across restarts (Claude `--resume`, Codex `exec resume <thread_id>`).
- 🧠 **Echo mode** — switch to `BRIDGE_BRAIN=echo` to validate the inbound pipe without invoking any agent.
- 😊 **Emoji progress** — `🤔Thinking` → `🥳Done` / `😖Failed` reactions mark task progress on the original @-message (best-effort, never blocks).
- 🔒 **Sender allowlist** — gate execution to specific DingTalk `staff_id`s; empty = open (not recommended).
- 🛡️ **No `dangerously-skip-permissions`** — the default Claude permission mode is the strictest one; expand capabilities only through `allowedTools`.

---

## Architecture / 架构

```
DingTalk group @bot ──① Stream WS ──▶ bridge.py ──② spawn agent CLI ──▶ Claude / Codex
        ▲                                                  │
        └──────────────── ③ reply via session_webhook ──────┘
                          + optional emoji reactions
```

| File | Role |
|---|---|
| `bridge.py` | Stream client per bot, message routing, command parsing |
| `config.py` | Immutable `.env` loading + multi-agent config |
| `brain.py` | `EchoBrain` / `ClaudeBrain` / `CodexBrain` / `HermesBrain` — one `Brain` per agent |
| `reactions.py` | Emoji progress via DingTalk OpenAPI (robot_1_0) |
| `session_store.py` | Thread-safe JSON persistence of `(agent, conv, sender) → session_id` |
| `task_io.py` | `TaskInput` / `TaskOutput` contract shared by every brain |
| `selftest.py` | Offline unit tests (run with `python selftest.py`) |
| `run.sh` | One-shot launcher (creates venv, installs deps, runs bridge) |

---

## Quickstart / 快速开始

### 1. DingTalk console (one-time)

1. [DingTalk Open Platform](https://open-dev.dingtalk.com/) → **App Development → Enterprise Internal App** → create one app per agent.
2. In each app → **Credentials** → copy `AppKey` / `AppSecret`.
3. **Robot** → enable Robot capability → **Message receiving mode: Stream** (NOT HTTP callback).
4. Add the robot to your target group (Group Settings → Smart Assistants → Add Robot).
5. Publish the app.

### 2. Configure

```bash
git clone https://github.com/trustchain-ai/dingtalk-bridge.git
cd dingtalk-bridge
cp .env.example .env
# fill in CLAUDE_APP_KEY / CLAUDE_APP_SECRET (and CODEX_*, HERMES_* as needed)
```

`.env` is git-ignored. Never commit AppKey/Secret.

### 3. Run

```bash
./run.sh
# first run: creates .venv and installs requirements.txt
```

In your DingTalk group:

```
@claude-bot 你好        # EchoBrain confirms the pipe (BRIDGE_BRAIN=echo)
                        # then switch BRIDGE_BRAIN=claude to actually execute
@codex-bot  /status     # Show session state for this (conv, sender)
@claude-bot /reset      # Wipe this bot's session for this (conv, sender)
@claude-bot /reset all  # Wipe sessions across all agents
```

---

## Agent setup / 接入 Claude Code 与 Codex

Each agent expects its CLI binary on `PATH`:

| Agent | Binary (default) | Headless flag | Resume flag |
|---|---|---|---|
| claude | `/opt/homebrew/bin/claude` | `-p <task> --output-format json` | `--resume <session_id>` |
| codex  | `/opt/homebrew/bin/codex`  | `exec --skip-git-repo-check --sandbox read-only --json` | `exec resume <thread_id>` |
| hermes | `/Users/lihu/.local/bin/hermes` | `-z <task>` | `--continue <session_name>` |

Override via `CLAUDE_BIN`, `CODEX_BIN`, `HERMES_BIN` in `.env`.

Claude honors `CLAUDE_PERMISSION_MODE` (`default` / `acceptEdits` / `plan`). **Do not** set `dangerously-skip-permissions` — the bridge intentionally refuses to bypass Claude's safety gate so a group member can't drive your machine unchecked.

---

## Security / 安全

- `BRIDGE_BRAIN=claude` means **any allowed sender can run Claude/Codex with your credentials on your machine**. Always set `ALLOWED_SENDERS=<your_staff_id>`.
- Sender staff IDs are printed in startup logs at first run — copy yours into `.env`.
- AppKey / AppSecret live only in `.env` (git-ignored). Rotate immediately if exposed.
- Bridge never echoes credentials into chat; replies are clipped to `MAX_REPLY_CHARS`.

---

## Commands / 命令

| Command | Effect |
|---|---|
| `<free text>` | Route the task to the agent of the @-mentioned bot |
| `/reset` | Wipe this bot's session for this (conv, sender) |
| `/reset <agent>` | If invoked on a different bot, tells you which bot to use |
| `/reset all` | Wipe every agent's session for this (conv, sender) |
| `/status` | Print session state for every agent in this (conv, sender) |

---

## Development / 开发

```bash
./.venv/bin/python selftest.py       # offline unit tests (default; SKIP_LIVE=1 skips live CLI tests)
./.venv/bin/python -m bridge         # run bridge directly (use ./run.sh in production)
```

The brain interface is intentionally narrow (`run(TaskInput) -> TaskOutput`) — adding a new agent is a single new class in `brain.py` plus a `build_brain` branch.

```python
# Adding "my-agent":
class MyAgentBrain:
    name = "my-agent"
    def run(self, ti: TaskInput) -> TaskOutput: ...
```

---

## License

MIT — see [LICENSE](./LICENSE).

## Maintainer

[trustchain-ai](https://github.com/trustchain-ai) · issues and PRs welcome.
