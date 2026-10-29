#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
企业微信智能机器人 <-> Claude Code 双向桥。

长连接接收手机消息 -> 调 claude -p 执行 -> 流式回复到企业微信。
会话由用户手动控制:发"开启新会话"开新的,发"清理上下文"重置,
发"resume"列出最近会话、再发"resume 序号"接续其中某个会话。
"""
import json
import logging
import logging.handlers
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler

import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))

WS_URL = "wss://openws.work.weixin.qq.com"
PING_INTERVAL = 30
STREAM_TIMEOUT = 600  # 服务端要求 10 分钟内 finish
MAX_BYTES = 3800  # 企业微信 markdown 单条上限 4096 字节,留余量
# 上下文是否"快满"以模型真实用量为准(见 _context_usage),不再按 jsonl 文件大小判断:
# jsonl 里含工具输出、附件等,体积与 token 数不成比例,1.7MB 的文件可能只占 12 万 token。
# 这里只保留一个很高的文件大小兜底(远超 1M token 的合理体积),防止异常膨胀文件拖死 resume。
SESSION_HARD_BYTES = 40 * 1024 * 1024
CONTEXT_WARN_RATIO = 0.90  # 用量达到窗口 90% 时提示用户
CONTEXT_WINDOW = 1000000   # 模型上下文窗口(1M)

log = logging.getLogger("wecom-bot")


def disable_quickedit():
    """关闭 Windows 控制台 QuickEdit 模式。

    否则用户在窗口里点一下/选中文字,控制台输出会被冻结,所有写 stdout
    的线程(包括处理消息的线程)全部阻塞,表现为"回车后才恢复"。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_QUICK_EDIT = 0x0040
        new = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT
        k32.SetConsoleMode(h, new)
    except Exception:
        pass


def setup_log():
    disable_quickedit()
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    h = RotatingFileHandler(CFG["log_file"], maxBytes=2 * 1024 * 1024,
                            backupCount=3, encoding="utf-8")
    h.setFormatter(fmt)
    log.addHandler(h)
    # 控制台输出走独立线程 + 有界队列:控制台被冻结时丢弃输出,绝不阻塞业务线程
    q = queue.Queue(maxsize=2000)
    qh = logging.handlers.QueueHandler(q)
    log.addHandler(qh)

    def drain():
        while True:
            try:
                rec = q.get()
                if rec is None:
                    return
                msg = fmt.format(rec)
                try:
                    sys.stdout.write(msg + "\n")
                    sys.stdout.flush()
                except Exception:
                    pass
            except Exception:
                pass

    threading.Thread(target=drain, daemon=True).start()


# ---------------- 会话状态 ----------------

def load_session():
    try:
        return json.load(open(CFG["session_file"], encoding="utf-8"))
    except Exception:
        return {}


def save_session(d):
    tmp = CFG["session_file"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, CFG["session_file"])


session_lock = threading.Lock()


def session_path(sid):
    """返回 session 的 jsonl 路径,找不到返回 None。"""
    if not sid:
        return None
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    try:
        for d in os.listdir(base):
            p = os.path.join(base, d, sid + ".jsonl")
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return None


def session_size(sid):
    """返回 session 的 jsonl 文件大小(字节),找不到返回 0。"""
    p = session_path(sid)
    return os.path.getsize(p) if p else 0


def _context_usage(sid):
    """从会话 jsonl 里取当前上下文用量与窗口,取不到返回 (None, None)。

    以最后一次带 usage 的请求为准:上下文 = input + cache_read + cache_creation。
    注意不能用 claude -p 输出的 modelUsage——那是整个会话的累计值(含子代理),
    会远超窗口。窗口这里按 1M 算(jsonl 里的 model 名是代理别名,看不出窗口)。
    """
    path = session_path(sid)
    if not path:
        return None, None
    used = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                m = r.get("message") or {}
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                n = ((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                     + (u.get("cache_creation_input_tokens") or 0))
                if n:
                    used = n
    except Exception:
        pass
    return used, (CONTEXT_WINDOW if used else None)


def project_dir_for(work_dir):
    """claude 把会话存在 ~/.claude/projects/<工作目录转义>/ 下。"""
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    want = os.path.normcase(os.path.abspath(work_dir))
    try:
        for d in os.listdir(base):
            p = os.path.join(base, d)
            if not os.path.isdir(p):
                continue
            # 目录名是路径转义,无法直接还原;看目录里会话记录的 cwd 更可靠
            for f in os.listdir(p):
                if not f.endswith(".jsonl"):
                    continue
                cwd = _session_cwd(os.path.join(p, f))
                if cwd and os.path.normcase(os.path.abspath(cwd)) == want:
                    return p
                break
    except Exception:
        pass
    return None


def _session_cwd(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r.get("cwd"):
                    return r["cwd"]
    except Exception:
        pass
    return None


def _user_text(r):
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c
                        if isinstance(x, dict) and x.get("type") == "text").strip()
    return ""


def list_sessions(work_dir, limit=10):
    """列出工作目录下最近的会话,按最后活动时间倒序。

    返回 [{"id", "mtime", "turns", "first", "last"}],跳过只有系统提示的空会话。
    """
    pdir = project_dir_for(work_dir)
    if not pdir:
        return []
    items = []
    for f in os.listdir(pdir):
        if not f.endswith(".jsonl"):
            continue
        path = os.path.join(pdir, f)
        first = last = ""
        turns = 0
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("type") != "user" or r.get("isMeta"):
                        continue
                    t = _user_text(r)
                    if not t:
                        continue
                    turns += 1
                    first = first or t
                    last = t
        except Exception:
            continue
        if not turns:
            continue
        items.append({"id": f[:-6], "mtime": os.path.getmtime(path),
                      "turns": turns, "first": first, "last": last})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def _snippet(t, n=40):
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[:n] + "…"


PROGRESS_QUERY_RE = re.compile(
    r"(做到|进展|进度|什么程度|到哪了|好了吗|修好了|完成了吗|做完|跑完了|结果呢|还在跑)")


def _is_progress_query(text):
    """「做到什么程度了」这类短查询:忙碌时直接回答进度,不排队也不转发给模型。"""
    t = text.strip()
    return len(t) <= 30 and bool(PROGRESS_QUERY_RE.search(t))


# ---------------- Claude 调用 ----------------

def split_markdown(text, max_bytes):
    """按 UTF-8 字节数切分,优先在换行处断开,不切断字符和 markdown 语法。"""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    parts, buf, buf_len = [], [], 0

    def flush():
        nonlocal buf, buf_len
        if buf:
            parts.append("".join(buf).rstrip("\n"))
            buf, buf_len = [], 0

    for line in text.splitlines(keepends=True):
        lb = len(line.encode("utf-8"))
        if lb > max_bytes:
            # 单行就超长:按字符硬切
            flush()
            cur = ""
            for ch in line:
                if len((cur + ch).encode("utf-8")) > max_bytes:
                    parts.append(cur)
                    cur = ch
                else:
                    cur += ch
            if cur:
                buf, buf_len = [cur], len(cur.encode("utf-8"))
            continue
        if buf_len + lb > max_bytes:
            flush()
        buf.append(line)
        buf_len += lb
    flush()
    return [p for p in parts if p.strip()]

def kill_proc(p):
    """强制结束进程树(Windows 下 claude 是 .cmd,会派生子 node 进程)。"""
    if p is None or p.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=10)
        else:
            p.kill()
    except Exception as e:
        log.warning("kill 失败: %r", e)
    try:
        p.wait(timeout=5)
    except Exception:
        pass


def run_claude(prompt, session_id=None, on_tick=None, register=None):
    """调用 claude -p,返回 (result_text, new_session_id, is_error)。
    on_tick(elapsed) 每 5 秒回调一次,用于发进度提示。
    register(proc) 用于把子进程登记到 Bot,以便中断。
    """
    cmd = [CFG["claude_bin"], "-p", prompt, "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    # 关键:不要让子进程继承控制台 stdin。否则 claude 一旦等待输入(权限确认等),
    # 就会卡住整个处理线程,表现为"长时间无对话后收不到消息,按回车才恢复"。
    env["CLAUDE_CODE_NONINTERACTIVE"] = "1"
    try:
        p = subprocess.Popen(
            cmd, cwd=CFG["work_dir"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", env=env,
        )
    except Exception as e:
        return ("❌ 启动 Claude 失败:%s" % e, session_id, True)

    if register:
        register(p)

    t0 = time.time()
    exited_at = None
    while True:
        try:
            out, err = p.communicate(timeout=5)
            break
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            # 进程已退出但管道没关:通常是 claude 派生的后台子进程仍持有 stdout。
            # 给它 10 秒收尾,之后强制关管道,避免永远等下去。
            if p.poll() is not None:
                if exited_at is None:
                    exited_at = time.time()
                    log.warning("claude 已退出但输出管道未关闭,等待收尾…")
                elif time.time() - exited_at > 10:
                    log.warning("管道 10 秒未关闭,强制读取已有输出")
                    try:
                        out = p.stdout.read() if p.stdout else ""
                        err = p.stderr.read() if p.stderr else ""
                    except Exception:
                        out, err = "", ""
                    break
            if elapsed > CFG["claude_timeout_sec"]:
                kill_proc(p)
                return ("⏱ 执行超时(%d 秒),已终止。" % CFG["claude_timeout_sec"],
                        session_id, True)
            if on_tick:
                try:
                    on_tick(elapsed)
                except Exception as e:
                    log.warning("on_tick 异常: %r", e)
    if register:
        register(None)

    out = (out or "").strip()
    if not out:
        err = (err or "").strip()[-1500:]
        return ("❌ Claude 无输出(exit %s)\n%s" % (p.returncode, err), session_id, True)
    try:
        d = json.loads(out)
    except Exception:
        return (out[-3000:], session_id, True)

    text = d.get("result") or ""
    if not text and d.get("is_error"):
        text = "❌ 执行失败:%s" % json.dumps(d, ensure_ascii=False)[:1500]
    return (text, d.get("session_id") or session_id, bool(d.get("is_error")))


# ---------------- 企业微信连接 ----------------

class Bot:
    def __init__(self):
        self.ws = None
        self.alive = False
        self.send_lock = threading.Lock()
        self.pending = {}  # req_id -> [Event, frame]
        self.busy = False          # 是否有任务在执行
        self.busy_since = 0.0
        self.busy_text = ""
        self.current_proc = None   # 当前 claude 子进程,用于中断
        self.task_id = 0           # 任务编号,每轮任务 +1,用于识别陈旧任务
        self.task_done = False     # 当前任务是否已结束
        self.last_result = ""      # 最近一次任务的结论,用于回答「做到什么程度了」
        self.last_result_at = 0.0
        self.last_recv = time.time()

    def send(self, obj):
        """发送一帧。任何失败都不阻塞调用方,返回 True/False。"""
        data = json.dumps(obj, ensure_ascii=False)
        # 加超时获取锁,避免某次卡死的 send 永久阻塞所有后续发送
        if not self.send_lock.acquire(timeout=10):
            log.error("send 获取锁超时,丢弃一帧: %s", data[:120])
            return False
        try:
            ws = self.ws
            if ws is None or not self.alive:
                return False
            # 注意:settimeout 是 socket 级设置,会同时影响主循环的 recv。
            # 发送后必须恢复,否则 recv 会被 10 秒超时打断(导致反复重连)。
            old = ws.gettimeout()
            try:
                ws.settimeout(10)
                ws.send(data)
            finally:
                try:
                    ws.settimeout(old)
                except Exception:
                    pass
            return True
        except Exception as e:
            log.error("send 失败: %r", e)
            return False
        finally:
            self.send_lock.release()

    def req_id(self):
        return uuid.uuid4().hex

    def connect(self):
        self.ws = websocket.create_connection(
            WS_URL, timeout=60,
            header=["User-Agent: wecom-bot-bridge/1.0"],
        )

    def subscribe(self):
        """发送订阅并等待响应。必须在主接收循环启动后调用。"""
        rid = self.req_id()
        slot = [threading.Event(), None]
        self.pending[rid] = slot
        self.send({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": rid},
            "body": {"bot_id": CFG["bot_id"], "secret": CFG["secret"]},
        })
        if not slot[0].wait(20):
            self.pending.pop(rid, None)
            raise RuntimeError("订阅超时,未收到 aibot_subscribe 响应")
        resp = slot[1] or {}
        self.pending.pop(rid, None)
        if resp.get("errcode") != 0:
            raise RuntimeError("订阅失败: %s" % resp)
        log.info("订阅成功")

    def ping_loop(self):
        while self.alive:
            time.sleep(PING_INTERVAL)
            if not self.alive:
                break
            try:
                self.send({"cmd": "ping", "headers": {"req_id": self.req_id()}})
            except Exception as e:
                log.warning("ping 失败: %s", e)
                break

    # ---- 回复 ----

    def stream(self, req_id, stream_id, content, finish=False, feedback_id=None):
        stream = {"id": stream_id, "finish": finish, "content": content}
        if feedback_id:
            stream["feedback"] = {"id": feedback_id}
        self.send({
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {"msgtype": "stream", "stream": stream},
        })

    def push(self, chatid, content, chat_type=1):
        """主动推送(markdown)。chat_type: 1=单聊 2=群聊。"""
        self.send({
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self.req_id()},
            "body": {
                "chatid": chatid, "chat_type": chat_type,
                "msgtype": "markdown", "markdown": {"content": content},
            },
        })

    def reply_long(self, req_id, stream_id, chatid, chat_type, text):
        """回复可能超长的内容:首段走流式,其余分片主动推送,不丢内容。"""
        parts = split_markdown(text, MAX_BYTES)
        log.info("回复分 %d 段,总字节 %d", len(parts), len(text.encode("utf-8")))
        if len(parts) == 1:
            self.stream(req_id, stream_id, parts[0], finish=True)
            return
        # 首段:用流式回复收尾
        self.stream(req_id, stream_id, parts[0], finish=True)
        # 后续分片:主动推送
        for i, part in enumerate(parts[1:], start=2):
            head = "(%d/%d)\n\n" % (i, len(parts))
            try:
                self.push(chatid, head + part, chat_type)
            except Exception as e:
                log.error("分片 %d 推送失败: %s", i, e)
            time.sleep(0.5)  # 避免触发 30 条/分钟限频

    # ---- 消息处理 ----

    def handle_msg(self, frame):
        body = frame.get("body") or {}
        req_id = (frame.get("headers") or {}).get("req_id")
        msgtype = body.get("msgtype")
        chatid = body.get("chatid") or (body.get("from") or {}).get("userid")
        chat_type = 1 if body.get("chattype") == "single" else 2

        if msgtype != "text":
            self.stream(req_id, uuid.uuid4().hex,
                        "目前只支持文字消息。", finish=True)
            return

        text = ((body.get("text") or {}).get("content") or "").strip()
        # 群里消息带 "@机器人名 ",去掉
        text = re.sub(r"^@\S+\s*", "", text).strip()
        if not text:
            return
        log.info("收到消息 chat=%s type=%s: %s", chatid, chat_type, text[:200])

        # 中断指令:立即处理,不受 busy 影响
        if text in ("停止", "中断", "取消", "/stop"):
            self.do_stop(req_id)
            return

        # 忙碌时:控制指令照常处理,普通消息立即提示而不排队。
        # 但「做到什么程度了」这类查询直接回答进度,不必等任务结束。
        if self.busy and text not in ("开启新会话", "新会话", "/new", "/NEW",
                                      "清理上下文", "清空上下文", "/clear",
                                      "状态", "/status") and not _is_progress_query(text):
            used = time.time() - self.busy_since
            self.stream(req_id, uuid.uuid4().hex,
                        "⏳ 上一个任务还在执行(已用 %.0f 秒),暂不接收新指令。\n"
                        "> 发「停止」可中断当前任务。\n"
                        "> 指令:`%s…`" % (used, self.busy_text[:40]),
                        finish=True)
            return
        if self.busy and _is_progress_query(text):
            self.stream(req_id, uuid.uuid4().hex, self._progress_text(), finish=True)
            return

        threading.Thread(
            target=self.process, args=(req_id, chatid, chat_type, text),
            daemon=True,
        ).start()

    def _progress_text(self):
        used = time.time() - self.busy_since
        msg = ("⏳ 当前任务还在执行,已用 %.0f 秒。\n> 指令:`%s…`"
               % (used, self.busy_text[:60]))
        if self.last_result:
            age = time.time() - self.last_result_at
            msg += ("\n\n**上一轮任务的结果**(%.0f 分钟前):\n%s"
                    % (age / 60, self.last_result[:600]))
        msg += "\n\n> 发「停止」可中断当前任务。"
        return msg

    def _register_proc(self, p):
        self.current_proc = p

    def do_stop(self, req_id):
        p = self.current_proc
        if p is None:
            self.stream(req_id, uuid.uuid4().hex, "当前没有正在执行的任务。", finish=True)
            return
        log.info("用户请求中断任务")
        kill_proc(p)
        self.stream(req_id, uuid.uuid4().hex, "🛑 已中断当前任务。", finish=True)

    def process(self, req_id, chatid, chat_type, text):
        stream_id = uuid.uuid4().hex
        sid = None

        # 控制指令
        if text in ("开启新会话", "新会话", "/new", "/NEW"):
            with session_lock:
                save_session({})
            self.stream(req_id, stream_id,
                        "✅ 已开启新会话,之前的上下文不再带入。", finish=True)
            return
        if text in ("清理上下文", "清空上下文", "/clear"):
            with session_lock:
                d = load_session()
                sid = d.get("session_id")
                d["cleared_at"] = time.time()
                d["session_id"] = None
                save_session(d)
            self.stream(req_id, stream_id,
                        "✅ 已清理上下文,下一条消息从空白开始。", finish=True)
            return
        if text in ("状态", "/status"):
            d = load_session()
            msg = ("**当前会话**\n> session: `%s`\n> 工作目录: `%s`\n> 忙碌: %s"
                   % (d.get("session_id") or "(无,下条消息新建)",
                      CFG["work_dir"], "是" if self.busy else "否"))
            if d.get("resume_list"):
                msg += "\n> 发「resume 序号」可切换会话"
            self.stream(req_id, stream_id, msg, finish=True)
            return
        if text in ("resume", "/resume", "会话列表"):
            self.do_resume_list(req_id, stream_id)
            return
        m = re.match(r"^(?:resume|/resume|接续)\s*(\d+)$", text, re.I)
        if m:
            self.do_resume_pick(req_id, stream_id, int(m.group(1)))
            return

        self.task_id += 1
        my_task = self.task_id
        self.task_done = False
        self.busy = True
        self.busy_since = time.time()
        self.busy_text = text
        try:
            self._run_task(req_id, chatid, chat_type, text, stream_id, my_task)
        except Exception as e:
            log.exception("任务异常: %r", e)
            try:
                self.stream(req_id, stream_id, "❌ 处理出错:%s" % e, finish=True)
            except Exception:
                pass
        finally:
            # 只有当前任务才能清忙碌标记:旧任务被「停止」后收尾时,
            # 不能把用户已经发起的新任务标记成空闲。
            if my_task == self.task_id:
                self.busy = False
                self.current_proc = None

    def do_resume_list(self, req_id, stream_id):
        items = list_sessions(CFG["work_dir"], limit=10)
        if not items:
            self.stream(req_id, stream_id,
                        "没有找到可接续的会话。", finish=True)
            return
        cur = load_session().get("session_id")
        lines = ["**最近的会话**(发「resume 序号」接续)"]
        for i, it in enumerate(items, start=1):
            ts = time.strftime("%m-%d %H:%M", time.localtime(it["mtime"]))
            mark = " ←当前" if it["id"] == cur else ""
            lines.append("%d. `%s` · %d 轮%s\n   > %s" % (
                i, ts, it["turns"], mark, _snippet(it["last"])))
        lines.append("\n> 发「状态」查看当前会话")
        with session_lock:
            d = load_session()
            d["resume_list"] = [it["id"] for it in items]
            save_session(d)
        self.stream(req_id, stream_id, "\n".join(lines), finish=True)

    def do_resume_pick(self, req_id, stream_id, n):
        with session_lock:
            d = load_session()
            ids = d.get("resume_list") or []
        if not ids:
            self.stream(req_id, stream_id,
                        "请先发「resume」获取会话列表,再发「resume 序号」。",
                        finish=True)
            return
        if not 1 <= n <= len(ids):
            self.stream(req_id, stream_id,
                        "序号超出范围,当前列表有 %d 个会话。" % len(ids),
                        finish=True)
            return
        sid = ids[n - 1]
        with session_lock:
            d = load_session()
            d["session_id"] = sid
            d["last_active"] = time.time()
            d.pop("resume_list", None)  # 列表一次性使用,避免过时序号
            save_session(d)
        log.info("用户切换到会话 %s", sid)
        self.stream(req_id, stream_id,
                    "✅ 已接续第 %d 个会话 `%s`。\n下一条消息会带上它的历史上下文。"
                    % (n, sid[:8]), finish=True)

    def _run_task(self, req_id, chatid, chat_type, text, stream_id, my_task):
        with session_lock:
            sid = load_session().get("session_id")

        # 会话是否该换,由用户决定(「开启新会话」/「清理上下文」)。
        # 上下文快满时 Claude Code 自己会压缩,这里不替用户开新会话。
        # 只有文件异常膨胀(远超 1M token 的合理体积)才兜底重置,防止 resume 卡死。
        notice = ""
        size = session_size(sid)
        if size > SESSION_HARD_BYTES:
            log.warning("session %s 文件异常膨胀(%d MB),兜底开新会话", sid, size // (1024 * 1024))
            with session_lock:
                save_session({})
            notice = ("⚠️ 上一会话文件异常膨胀(%d MB),续接可能失败,"
                      "已开启新会话。\n\n" % (size // (1024 * 1024)))
            sid = None

        self.stream(req_id, stream_id, "🤔 收到,正在处理…", finish=False)

        # 进度回调:每 20 秒推一次已用时间,避免用户以为没反应
        last_tick = [0.0]

        def on_tick(elapsed):
            if self.task_done or my_task != self.task_id:
                return
            if elapsed - last_tick[0] < 20:
                return
            last_tick[0] = elapsed
            try:
                self.stream(req_id, stream_id,
                            "⏳ 正在处理… 已用 %.0f 秒" % elapsed, finish=False)
            except Exception:
                pass

        t0 = time.time()
        result, new_sid, is_err = run_claude(
            text, sid, on_tick=on_tick, register=self._register_proc)
        elapsed = time.time() - t0

        # 上下文快满(≥90%)时提示用户,由用户决定是否「开启新会话」
        used, window = _context_usage(new_sid or sid)
        if used and window and used / window >= CONTEXT_WARN_RATIO:
            log.warning("上下文用量 %d / %d", used, window)
            notice += ("提示:上下文已用 %.0f%%(%dK / %dK),发「开启新会话」可重置。\n\n"
                       % (100.0 * used / window, used // 1000, window // 1000))

        # resume 失败检测:请求续接 A,却回来了 B,说明上下文已丢失
        lost = bool(sid) and bool(new_sid) and new_sid != sid and not is_err
        if lost:
            log.error("resume 失败:请求 %s,实际 %s,上下文已丢失", sid, new_sid)
            notice += ("⚠️ 续接原会话失败,本轮是在无历史上下文的新会话中回答的。\n\n")

        with session_lock:
            d = load_session()
            if new_sid:
                d["session_id"] = new_sid
            d["last_active"] = time.time()
            save_session(d)

        result = notice + result
        self.last_result = result[:1500]
        self.last_result_at = time.time()

        # 超长自动分片发送,不再截断丢弃
        if elapsed > 30 and not is_err:
            result += "\n\n(耗时 %.0f 秒)" % elapsed

        # 任务已被「停止」或被新任务顶替:结论仍收进会话记录(上下文不丢),
        # 但不再往对话里推,避免旧任务的回复打断用户正在看的新任务。
        if my_task != self.task_id:
            log.info("任务 %d 已被替换,结论不推送", my_task)
            self.task_done = True
            return

        log.info("回复 chat=%s err=%s len=%d 用时%.1fs",
                 chatid, is_err, len(result), elapsed)
        try:
            self.reply_long(req_id, stream_id, chatid, chat_type, result)
        except Exception as e:
            log.error("回复失败,改主动推送: %s", e)
            try:
                self.push(chatid, result[:MAX_BYTES], chat_type)
            except Exception as e2:
                log.error("兜底推送也失败: %s", e2)
        finally:
            self.task_done = True

    def handle_event(self, frame):
        body = frame.get("body") or {}
        req_id = (frame.get("headers") or {}).get("req_id")
        ev = (body.get("event") or {}).get("eventtype")
        log.info("事件: %s", ev)
        if ev == "enter_chat":
            self.send({
                "cmd": "aibot_respond_welcome_msg",
                "headers": {"req_id": req_id},
                "body": {"msgtype": "text", "text": {
                    "content": "你好,我是 Claude Code。直接发消息即可下指令。\n"
                               "· 发「开启新会话」开始全新对话\n"
                               "· 发「清理上下文」清空记忆\n"
                               "· 发「状态」查看当前会话\n"
                               "· 发「resume」列出最近会话,再发「resume 序号」接续"}},
            })
        elif ev == "disconnected_event":
            log.warning("收到 disconnected_event,本连接已被顶掉")
            self.alive = False

    def run(self):
        backoff = 1
        while True:
            try:
                self.connect()
                self.alive = True
                self.last_recv = time.time()
                backoff = 1
                threading.Thread(target=self.ping_loop, daemon=True).start()
                threading.Thread(target=self.subscribe, daemon=True).start()
                log.info("已连接,等待消息…")
                while self.alive:
                    try:
                        raw = self.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        # 读超时不是断线:ping/pong 每 30 秒一次,120 秒无任何帧才算死
                        if time.time() - self.last_recv > 120:
                            raise RuntimeError("120 秒未收到任何数据,判定连接失效")
                        continue
                    self.last_recv = time.time()
                    frame = json.loads(raw)
                    cmd = frame.get("cmd")
                    rid = (frame.get("headers") or {}).get("req_id")
                    if rid in self.pending:
                        slot = self.pending[rid]
                        slot[1] = frame
                        slot[0].set()
                        continue
                    if cmd == "aibot_msg_callback":
                        self.handle_msg(frame)
                    elif cmd == "aibot_event_callback":
                        self.handle_event(frame)
                    elif cmd == "pong" or frame.get("errcode") == 0:
                        pass
                    else:
                        log.debug("未处理帧: %s", frame)
            except KeyboardInterrupt:
                log.info("手动停止")
                return
            except Exception as e:
                log.error("连接异常: %r, %d 秒后重连", e, backoff)
                self.alive = False
                try:
                    self.ws.close()
                except Exception:
                    pass
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    setup_log()
    Bot().run()
