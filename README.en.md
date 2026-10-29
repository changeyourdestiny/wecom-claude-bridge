<p align="center">
  <img src="docs/logo.svg" width="88" alt="logo">
</p>

<h1 align="center">WeCom Claude Bridge</h1>

<p align="center">
  <b>Chat with Claude Code from WeCom (企业微信) on your phone.</b><br>
  在企业微信里和 Claude Code 对话,手机上就能让它读代码、跑命令、改文件。
</p>

<p align="center">
  <a href="https://github.com/changeyourdestiny/wecom-claude-bridge/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/changeyourdestiny/wecom-claude-bridge?style=flat-square&logo=github&color=blue"></a>
  <a href="https://github.com/changeyourdestiny/wecom-claude-bridge/forks"><img alt="GitHub forks" src="https://img.shields.io/github/forks/changeyourdestiny/wecom-claude-bridge?style=flat-square&logo=github"></a>
  <a href="https://github.com/changeyourdestiny/wecom-claude-bridge/issues"><img alt="GitHub issues" src="https://img.shields.io/github/issues/changeyourdestiny/wecom-claude-bridge?style=flat-square&logo=github"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="Claude Code" src="https://img.shields.io/badge/Claude%20Code-CLI-D97757?style=flat-square">
  <img alt="WeCom" src="https://img.shields.io/badge/WeCom-企业微信-07C160?style=flat-square">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/changeyourdestiny/wecom-claude-bridge?style=flat-square&color=green"></a>
  <a href="https://github.com/changeyourdestiny/wecom-claude-bridge/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/changeyourdestiny/wecom-claude-bridge?style=flat-square"></a>
</p>

<p align="center">
  <a href="README.md">简体中文</a> ·
  <a href="README.en.md"><b>English</b></a>
</p>

---

## What is this

A bridge between [Claude Code](https://claude.com/claude-code) and a WeCom (企业微信) smart bot. You send a message from your phone; the bridge runs `claude -p` on your machine and streams the result back to WeCom.

```
WeCom App  ──►  WeCom smart bot (long connection)  ──►  bot.py  ──►  claude -p  ──►  local work dir
    ▲                                                                                    │
    └──────────────────────  streamed reply / chunked push  ◄──────────────────────────┘
```

## Features

- **Drive Claude Code from your phone**: plain text messages go straight to Claude Code. Long replies are split at 4096 bytes and pushed as chunks — nothing is truncated.
- **You own the session**: the bridge never starts a new session on its own. Claude Code auto-compacts when the context fills up; the bridge only warns once usage reaches 90%.
- **Switch sessions**: `resume` lists recent sessions, `resume 3` continues the third one.
- **Interrupt**: `停止` (stop) kills the running Claude process tree immediately.
- **Busy guard**: messages that arrive while a task is running get a notice instead of piling up.
- **Reconnect**: exponential backoff with ping/pong keepalive.

## Commands

| Message | Effect |
| --- | --- |
| Any text | Sent to Claude Code (continues the current session) |
| `resume` / `/resume` / `会话列表` | List the 10 most recent sessions |
| `resume 3` / `/resume 3` / `接续 3` | Continue the third session in the list |
| `开启新会话` / `新会话` / `/new` / `/NEW` | Start a brand-new session |
| `清理上下文` / `清空上下文` / `/clear` | Clear the current session's memory |
| `状态` / `/status` | Show current session, work dir, busy state |
| `停止` / `中断` / `取消` / `/stop` | Interrupt the running task |

## Prerequisites

1. **Claude Code CLI** installed and logged in (`claude` works, `claude -p "hi"` returns normally).
2. **A WeCom smart bot**: create one in the WeCom admin console, get its `bot_id` and `secret`, and enable long-connection (callback) mode. See the official WeCom docs for the exact entry point.
3. **Python 3.9+**, then install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Copy the example config and fill in your own values:

```bash
cp config.example.json config.json
```

| Field | Description |
| --- | --- |
| `bot_id` | Smart bot id |
| `secret` | Smart bot secret (**never commit it**) |
| `work_dir` | Claude Code working directory — the root it reads, writes and runs commands in |
| `claude_bin` | Path to the `claude` executable. On Windows usually `C:/Users/<you>/AppData/Roaming/npm/claude.cmd`; `claude` also works |
| `claude_timeout_sec` | Per-task timeout in seconds |
| `session_file` | Session state file; relative paths resolve against the script directory |
| `log_file` | Log file path |

`config.json`, `session.json` and logs are all covered by `.gitignore`.

## Run

```bash
python bot.py
```

On Windows you can also double-click `start.bat` (keep the window minimized; closing it stops the service).

The first message creates a Claude Code session under `work_dir`; later messages continue that session.

## How it works

- Subscribes to bot messages over a long connection to `wss://openws.work.weixin.qq.com` (`aibot_subscribe`); each `aibot_msg_callback` is handled in its own thread.
- Calls `claude -p "<message>" --output-format json`, adding `--resume <session_id>` when a session exists; reads `result` and the new `session_id` from the JSON.
- The child process gets `stdin=DEVNULL` so Claude can't block the handler thread waiting for permission prompts.
- Replies are streamed with `aibot_respond_msg`; oversized replies are pushed in chunks via `aibot_send_msg`.
- The session list is read from `~/.claude/projects/<escaped work dir>/` and matched by the real `cwd` recorded in each session.

### About context usage

The bridge reads the **last request's** `input + cache_read + cache_creation` from the session jsonl as the current context size. Do not use `modelUsage` from the `claude -p` output: it is cumulative for the whole session (including subagents) and will exceed the window. The window size comes from the `CONTEXT_WINDOW` constant — adjust it if you switch models.

## Security notes

This bridge exposes your local Claude Code to a chat bot. Please note:

- The bot only responds to messages from your WeCom account/groups, but **do not add it to untrusted groups**.
- What Claude Code can do is governed by your permissions in `~/.claude/settings.json`. Allow only the tools you need, or scope operations to the working directory.
- Never commit `config.json`, `session.json` or logs to any public repository.

## Contributing

Issues and pull requests are welcome.

## License

[MIT](LICENSE)
