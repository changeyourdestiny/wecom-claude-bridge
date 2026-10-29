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
  <a href="README.md"><b>简体中文</b></a> ·
  <a href="README.en.md">English</a>
</p>

---

## 这是什么

把 [Claude Code](https://claude.com/claude-code) 接到企业微信智能机器人上。你在手机上发一条消息,桥接进程在本机调用 `claude -p` 执行,结果流式回传到企业微信。

```
企业微信 App  ──►  企业微信智能机器人(长连接)  ──►  bot.py  ──►  claude -p  ──►  本机工作目录
      ▲                                                                              │
      └──────────────────────────  流式回复 / 分片推送  ◄──────────────────────────┘
```

## 功能

- **手机下指令**:文字消息直接交给 Claude Code,长回复按 4096 字节自动分片推送,不截断。
- **会话由你控制**:不会擅自开新会话。上下文快满时 Claude Code 自己压缩,桥接只在用到 90% 时提示一句。
- **会话切换**:`resume` 列出最近的会话,`resume 3` 接续第 3 个。
- **中断**:`停止` 立即结束正在跑的 Claude 进程树。
- **忙碌保护**:任务执行中收到新消息会提示,不会排队堆积。
- **断线重连**:指数退避重连,ping/pong 保活。

## 支持的指令

| 消息 | 作用 |
| --- | --- |
| 任意文字 | 发给 Claude Code 执行(续接当前会话) |
| `resume` / `/resume` / `会话列表` | 列出最近 10 个会话 |
| `resume 3` / `/resume 3` / `接续 3` | 接续列表里的第 3 个会话 |
| `开启新会话` / `新会话` / `/new` / `/NEW` | 开启全新会话 |
| `清理上下文` / `清空上下文` / `/clear` | 清空当前会话记忆 |
| `状态` / `/status` | 查看当前会话、工作目录、是否忙碌 |
| `停止` / `中断` / `取消` / `/stop` | 中断正在执行的任务 |

## 前置条件

1. **Claude Code CLI** 已安装并完成登录(`claude` 命令可用,`claude -p "hi"` 能正常返回)。
2. **企业微信智能机器人**:在企业微信管理后台创建智能机器人,拿到 `bot_id` 和 `secret`,并开启长连接(回调)模式。具体入口以企业微信官方文档为准。
3. **Python 3.9+**,安装依赖:

```bash
pip install -r requirements.txt
```

## 配置

复制示例配置并填入自己的值:

```bash
cp config.example.json config.json
```

| 字段 | 说明 |
| --- | --- |
| `bot_id` | 智能机器人的 bot id |
| `secret` | 智能机器人的 secret(**不要提交到仓库**) |
| `work_dir` | Claude Code 的工作目录,即它读写文件、执行命令的根目录 |
| `claude_bin` | `claude` 可执行文件路径。Windows 下通常是 `C:/Users/<you>/AppData/Roaming/npm/claude.cmd`,也可直接写 `claude` |
| `claude_timeout_sec` | 单次任务超时秒数 |
| `session_file` | 会话状态文件路径,相对路径按脚本目录解析 |
| `log_file` | 日志文件路径 |

`config.json`、`session.json`、日志都已写进 `.gitignore`。

## 运行

```bash
python bot.py
```

Windows 也可以双击 `start.bat`(窗口最小化后保持后台运行,关掉窗口即停止)。

首次给机器人发消息时,它会在 `work_dir` 下新建一个 Claude Code 会话;之后的消息都续接这个会话。

## 工作原理

- 通过 `wss://openws.work.weixin.qq.com` 长连接订阅机器人消息(`aibot_subscribe`),收到 `aibot_msg_callback` 后在新线程里处理。
- 调用 `claude -p "<消息>" --output-format json`,有会话时加 `--resume <session_id>`;从返回 JSON 里取 `result` 和新的 `session_id`。
- 子进程的 stdin 设为 `DEVNULL`,避免 Claude 等待权限确认时把处理线程卡死。
- 回复用 `aibot_respond_msg` 流式发送,超长部分用 `aibot_send_msg` 主动推送分片。
- 会话列表直接读 `~/.claude/projects/<工作目录>/` 下的会话记录,按真实 `cwd` 匹配工作目录。

### 关于上下文用量

桥接读取会话 jsonl 里**最后一次请求**的 `input + cache_read + cache_creation` 作为当前上下文大小。不要用 `claude -p` 输出里的 `modelUsage`:那是整个会话的累计值(含子代理),会远超窗口。窗口大小由 `CONTEXT_WINDOW` 常量决定,换模型时改一下。

## 安全提示

这个桥接等于把本机的 Claude Code 暴露给企业微信里的机器人。请注意:

- 机器人只会响应你企业微信账号/群里的消息,但**不要把机器人拉进不可信的群**。
- Claude Code 的权限设置(`~/.claude/settings.json` 的 `permissions`)决定了它能做什么。建议只放行需要的工具,或在工作目录里限定操作范围。
- 不要把 `config.json`、`session.json`、日志提交到任何公开仓库。

## 贡献

欢迎 Issue 和 Pull Request。

## License

[MIT](LICENSE)
