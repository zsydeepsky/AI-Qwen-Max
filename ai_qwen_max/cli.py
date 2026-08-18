"""CLI 交互：语言/模型/档位/思考强度选择 → 功能选单（对话/删历史/API 日志）。

设计要点：
  - 全局 ESC 语义：子功能内 = 取消/后退；推理中 = 打断；顶层选单 = 退出
    （msvcrt 逐键读取，input() 无法捕获 ESC）
  - 载入期 spinner（ASCII 帧 + 已耗时）
  - API 日志 = 直接轮询 events 环形缓冲（与 Web 监听页同源数据）
  - 标题栏状态机（SetConsoleTitleW）；思考流暗灰渲染 + THINK_BUDGET 截断
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

from .backend import Backend
from .config import CTX_CHOICES, Config
from .llm import EFFORT_THINK_PCT, GenResult, LLM
from .store import Session, SessionStore

THINK_BUDGET = 4096   # 思考流渲染预算（字符）——避免长思考刷屏
EFFORT_CHOICES = ["off", "low", "medium", "xHigh"]

# Windows 控制台 ANSI 转义（Win10 1511+ 默认支持）
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_CYAN = "\x1b[36m"
_YELLOW = "\x1b[33m"
_GREY = "\x1b[90m"

# ---- i18n ----
STR = {
    "zh": {
        "lang_title": "选择语言",
        "model_title": "模型选择",
        "model_empty": "  （config 中尚未登记模型，请输入 gguf 模型文件的完整路径）",
        "model_prompt": "输入序号或模型路径 > ",
        "model_bad_idx": "[错误] 序号越界（可用 1-{n} 或输入路径）",
        "model_not_file": "[错误] 文件不存在：{p}",
        "model_registered": "[已登记新模型]",
        "ctx_title": "上下文档位",
        "ctx_prompt": "档位 > ",
        "port_title": "前端端口（Web/API 服务）",
        "port_prompt": "端口 > ",
        "port_busy": "端口 {port} 已被占用，请换一个",
        "effort_title": "思考强度",
        "effort_prompt": "选择 > ",
        "loading": "载入中…",
        "load_ok": "载入完成，耗时 {t:.0f}s",
        "load_fail": "[载入失败] {e}",
        "hdr_loaded": "======== 载入成功：{model} / {ctx} / 思考:{effort} ========",
        "menu_title": "功能选单",
        "menu_chat": "开始对话",
        "menu_del": "删除历史对话",
        "menu_log": "API 日志",
        "menu_quit": "退出",
        "menu_prompt": "选择 > ",
        "quit": "ESC. 退出",
        "sessions": "会话：",
        "session_new": "[n] 新建会话",
        "session_pick": "选择 > ",
        "session_none": "（暂无历史会话）",
        "session_bad": "无效选择，重试。",
        "chat_hint": "\n输入消息发送；ESC 中断生成 / 返回选单。\n",
        "you": "你>",
        "rolled_back": "已回退 {n} 条消息。",
        "gen_fail": "[生成失败] {e}",
        "interrupted": "[已中断]",
        "think_tag": "┄┄ thinking ┄┄",
        "think_cut": " …（思考流截断）",
        "replay_head": "── 会话「{t}」共 {n} 条 ──",
        "replay_end": "── 回放结束 ──",
        "think_chars": "（思考 {n} 字符）",
        "img_missing": "[图片不存在: {p}]",
        "del_confirm": "确认删除「{t}」？(y/n，ESC=n) ",
        "del_done": "已删除会话「{t}」（引擎缓存按 TTL 自然过期）。",
        "del_no": "已取消。",
        "log_title": "── API 实时日志（ESC/q 返回选单）──",
        "log_hint": "（显示前端 HTTP 服务收到的请求与引擎返回；CLI 自身对话直连引擎不经过此处）",
        "log_pending": "[{ts}] {method} {path} …",
        "log_line": "[{ts}] {method} {path} → {status} · {dur}s",
        "log_err": "[{ts}] {method} {path} ✗ {err}",
        "err_invalid": "无效输入，重试。",
    },
    "en": {
        "lang_title": "Select language",
        "model_title": "Model selection",
        "model_empty": "  (No models registered in config; enter the full path to a .gguf file)",
        "model_prompt": "Index or model path > ",
        "model_bad_idx": "[Error] Index out of range (1-{n}, or enter a path)",
        "model_not_file": "[Error] File not found: {p}",
        "model_registered": "[New model registered]",
        "ctx_title": "Context tier",
        "ctx_prompt": "Tier > ",
        "port_title": "Frontend port (Web/API)",
        "port_prompt": "Port > ",
        "port_busy": "Port {port} is already in use, pick another",
        "effort_title": "Thinking effort",
        "effort_prompt": "Choice > ",
        "loading": "Loading model...",
        "load_ok": "Loaded in {t:.0f}s",
        "load_fail": "[Load failed] {e}",
        "hdr_loaded": "======== Loaded: {model} / {ctx} / effort:{effort} ========",
        "menu_title": "Menu",
        "menu_chat": "Start chat",
        "menu_del": "Delete chat history",
        "menu_log": "API log",
        "menu_quit": "Quit",
        "menu_prompt": "Choice > ",
        "quit": "ESC. Quit",
        "sessions": "Sessions:",
        "session_new": "[n] New session",
        "session_pick": "Select > ",
        "session_none": "(No saved sessions)",
        "session_bad": "Invalid choice, retry.",
        "chat_hint": "\nType a message to send; ESC interrupts generation / returns to menu.\n",
        "you": "you>",
        "rolled_back": "Rolled back {n} messages.",
        "gen_fail": "[Generation failed] {e}",
        "interrupted": "[interrupted]",
        "think_tag": "┄┄ thinking ┄┄",
        "think_cut": " ...(thinking stream truncated)",
        "replay_head": "── Session \"{t}\" · {n} messages ──",
        "replay_end": "── end of replay ──",
        "think_chars": "({n} chars of reasoning)",
        "img_missing": "[Image not found: {p}]",
        "del_confirm": "Delete \"{t}\"? (y/n, ESC=n) ",
        "del_done": "Deleted session \"{t}\" (engine cache ages out via TTL).",
        "del_no": "Cancelled.",
        "log_title": "── Live API log (ESC to return) ──",
        "log_hint": "(Requests received by the frontend HTTP service and engine responses; CLI chats go direct to the engine)",
        "log_pending": "[{ts}] {method} {path} ...",
        "log_line": "[{ts}] {method} {path} → {status} · {dur}s",
        "log_err": "[{ts}] {method} {path} ✗ {err}",
        "err_invalid": "Invalid input, retry.",
    },
}


def _set_title(text: str) -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(text)


def _widen_console(min_cols: int = 150) -> None:
    """尝试把控制台窗口加宽到 min_cols 列（仅经典 conhost 生效；Windows Terminal
    会忽略 SetConsoleWindowInfo，静默失败即可）。窗口像素宽度直接决定标题栏
    能显示多少字符。"""
    if sys.platform != "win32" or not sys.stdout.isatty():
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _CSBI(ctypes.Structure):
            _fields_ = [("dwSize", wintypes._COORD),
                        ("dwCursorPosition", wintypes._COORD),
                        ("wAttributes", wintypes.WORD),
                        ("srWindow", wintypes.SMALL_RECT),
                        ("dwMaximumWindowSize", wintypes._COORD)]

        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)                    # STD_OUTPUT_HANDLE
        info = _CSBI()
        if not k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
            return
        cur_cols = info.srWindow.Right - info.srWindow.Left + 1
        rows = info.srWindow.Bottom - info.srWindow.Top + 1
        if cur_cols >= min_cols or rows <= 0:
            return
        cols = max(min(min_cols, info.dwMaximumWindowSize.X), cur_cols)
        # 窗口宽度不能超过缓冲区：先扩缓冲区，再扩窗口
        if info.dwSize.X < cols:
            k32.SetConsoleScreenBufferSize(h, wintypes._COORD(cols, info.dwSize.Y))
        rect = wintypes.SMALL_RECT(0, info.srWindow.Top, cols - 1,
                                   info.srWindow.Top + rows - 1)
        k32.SetConsoleWindowInfo(h, True, ctypes.byref(rect))
    except Exception:
        pass


def _fmt_bytes(n) -> str:
    """字节 → 紧凑可读（标题栏用）：1.2G / 512M / 3K。"""
    n = int(n or 0)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}G"
    if n >= 1024 ** 2:
        return f"{n // 1024 ** 2}M"
    return f"{max(n // 1024, 0)}K"


def _supports_ansi() -> bool:
    return sys.stdout.isatty()


def _dim(s: str) -> str:
    return f"{_DIM}{s}{_RESET}" if _supports_ansi() else s


# ================= 键盘读取（ESC 感知） =================

def _cell_width(c: str) -> int:
    """East Asian 宽字符在 cmd.exe 里占 2 列，其它 1 列。仅用于输入行重绘宽度计算。"""
    if not c:
        return 0
    code = ord(c)
    if (
        (0x1100 <= code <= 0x115F) or           # Hangul Jamo init
        (0x2E80 <= code <= 0x303E) or           # CJK Radicals/Kana supplement/...
        (0x3041 <= code <= 0x33FF) or           # Hiragana/Katakana/CJK Symbols
        (0x3400 <= code <= 0x4DBF) or           # CJK Ext A
        (0x4E00 <= code <= 0x9FFF) or           # CJK Unified Ideographs
        (0xA000 <= code <= 0xA4CF) or           # Yi syllables
        (0xAC00 <= code <= 0xD7A3) or           # Hangul Syllables
        (0xF900 <= code <= 0xFAFF) or           # CJK Compat Ideographs
        (0xFE30 <= code <= 0xFE4F) or           # CJK Compat Forms
        (0xFF00 <= code <= 0xFF60) or           # Fullwidth forms (FF01~FF5E + others)
        (0xFFE0 <= code <= 0xFFE6)              # Fullwidth symbols
    ):
        return 2
    return 1


def _read_line_esc(prompt: str) -> tuple[str, bool]:
    """读一行；返回 (text, esc)。ESC 立即返回空串+True。

    Windows 控制台用 msvcrt 逐键读取（input() 拿不到 ESC）；
    非 Windows 或 stdin 非终端（管道/重定向）退化 input()。

    中文（宽字符）编辑：每次改动后**整行重绘**，避免单字符退格只擦除 1 列导致“半字残留”。
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if sys.platform != "win32" or not sys.stdin.isatty():
        try:
            return input(), False
        except EOFError:
            return "", True
    import msvcrt

    def _redraw(buf: list[str]) -> None:
        """清除光标到行首 → 写 prompt + buf → 把光标停在末尾。"""
        # 计算当前 buf 的显示列宽（按宽字符算 2 列）
        w = sum(_cell_width(c) for c in buf)
        # 1) 回退到当前 buf 的末尾列数（= 从当前光标列一路退到 prompt 之后）
        #    简便做法：先把 buf 列宽个 \b 退回 prompt 末尾（此时 buf 是“现在要渲染的目标内容”
        #    的前一状态是旧 buf，所以我们要先把光标退回 prompt 后，再覆盖输出。
        #    最稳：用 ANSI SGR 擦行（\x1b[2K 消整行） + \r 回到行首。
        #    Windows 10 cmd 默认未开 VT 模式，但本 fork 其它模块已用到 ANSI 颜色，
        #    保险起见退化为：退足够多格（max = prompt_len + old_w），再重写整行。
        sys.stdout.write("\r")
        sys.stdout.write(prompt)
        sys.stdout.write("".join(buf))
        # 右侧多余旧字符：先填满空格，再退回到末尾
        # 实际行尾可能超出 buf 显示宽度若干列——用擦行 ANSI 最稳；
        # 不支持的终端退化：额外写 40 空格 + 40 退格兜底。
        try:
            # EL: Erase in Line, to end of line (CSI 0 K / CSI K)
            sys.stdout.write("\x1b[K")
        except Exception:
            sys.stdout.write(" " * 40 + "\b" * 40)
        sys.stdout.write("\r")
        sys.stdout.write(prompt)
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    buf: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch == "\x1b":                       # ESC
            sys.stdout.write("\n")
            return "", True
        if ch in ("\r", "\n"):                 # 回车
            sys.stdout.write("\n")
            return "".join(buf), False
        if ch in ("\x00", "\xe0"):             # 功能键前缀：吞掉
            msvcrt.getwch()
            continue
        if ch == "\x03":                       # Ctrl+C
            raise KeyboardInterrupt
        changed = False
        if ch == "\x08":                       # 退格
            if buf:
                buf.pop()
                changed = True
        elif ch.isprintable():
            buf.append(ch)
            changed = True
        if changed:
            _redraw(buf)


def _read_key() -> str:
    """读单个键（用于 y/n 确认与日志页退出）。"""
    if sys.platform != "win32":
        return input().strip().lower()
    import msvcrt
    while True:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            msvcrt.getwch()
            continue
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch


def _esc_pressed() -> bool:
    """非阻塞探测 ESC（日志页轮询用）。"""
    if sys.platform != "win32":
        return False
    import msvcrt
    if not msvcrt.kbhit():
        return False
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        msvcrt.getwch()
        return False
    if ch == "\x1b":
        return True
    return False


class InterruptPoller:
    """生成期键盘轮询：按 ESC 触发中断事件。"""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.interrupt = threading.Event()

    def start(self) -> None:
        if sys.platform != "win32":
            return
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self) -> None:
        import msvcrt
        while not self.stop_event.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == "\x1b":
                    self.interrupt.set()
                    return
            self.stop_event.wait(0.05)

    def close(self) -> None:
        self.stop_event.set()


# ================= CLI 主类 =================

class Cli:
    def __init__(self, cfg: Config, backend: Backend, store: SessionStore, events, frontend_port: int = 8080):
        self.cfg = cfg
        self.backend = backend
        self.store = store
        self.events = events          # ApiEvents（API 日志页数据源）
        self.frontend_port = frontend_port   # 轮询 /api/stats 的本地端口
        self.lang = str(cfg.get("lang", "zh"))
        self.llm = LLM(backend, effort=str(cfg.get("reasoning_effort", "low")))
        # 标题栏刷新：后台线程轮询 /api/stats，写入快照，状态突变时推新标题
        self._title_thread: threading.Thread | None = None
        self._title_stop = threading.Event()
        self._title_state: dict = {}  # 最近一次 phase/tps 缓存，避免频繁刷 title
        self._last_status = ""        # 最近一轮精确 perf（footer），idle 状态位展示
        self._last_cache: dict = {}   # 最近一次 /api/stats 的 cache 段（拼标题用）

    def L(self, key: str, **kw: Any) -> str:
        s = STR.get(self.lang, STR["zh"]).get(key, key)
        return s.format(**kw) if kw else s

    def select_startup(self) -> int:
        """启动前置交互：语言 → 前端端口。返回端口（已探测可用并持久化到 config）。"""
        _widen_console()
        self._select_language()
        return self._select_port()

    def _select_port(self) -> int:
        """前端 HTTP 端口，默认 8080；socket 试绑探测可用性，占用则重输。"""
        import socket
        default = int(self.cfg.get("port", 8080) or 8080)
        print(f"\n{self.L('port_title')}：  (Enter = {default})")
        while True:
            raw, esc = _read_line_esc(self.L("port_prompt"))
            if esc:
                raise SystemExit(0)
            raw = raw.strip()
            if not raw:
                port = default
            elif raw.isdigit() and 1 <= int(raw) <= 65535:
                port = int(raw)
            else:
                print(self.L("err_invalid"))
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", port))
            except OSError:
                print(self.L("port_busy", port=port))
                continue
            self.cfg.data["port"] = port
            self.cfg.save()
            self.frontend_port = port
            return port

    # ================= 启动流程 =================

    def run(self, model_preset: str = "", ctx_preset: int = 0) -> None:
        """模型之后的流程：模型 → 档位 → 思考强度 → 载入 → 功能选单循环。

        语言与前端端口在 select_startup() 中先行完成（__main__ 需要先拿到端口起 HTTP）。
        """
        _widen_console()          # conhost：拉宽窗口以容纳长标题；Windows Terminal 静默忽略
        while True:
            model = self._select_model(model_preset)
            model_preset = ""                      # preset 只消费一次（载入失败重选时不复用）
            ctx = self._select_ctx(ctx_preset)
            ctx_preset = 0
            effort = self._select_effort()
            self.llm.ctx = ctx     # 思考预算 = ctx × effort 档百分比（off=0/low=10%/medium=25%/xHigh=50%）
            err, dur = self._load(model, ctx)
            if err is not None:
                print(self.L("load_fail", e=err))
                continue                           # 回到模型选择
            print(_dim("  " + self.L("load_ok", t=dur)))
            self._print_header(model, ctx, effort)
            break
        self._start_title_updater()
        try:
            self._menu_loop()
        finally:
            self._stop_title_updater()
            _set_title("AI-Qwen-Max | idle")

    def _start_title_updater(self) -> None:
        import httpx as _h
        self._client = _h.Client(base_url=f"http://127.0.0.1:{self.frontend_port}", timeout=2)
        self._title_thread = threading.Thread(target=self._title_loop, daemon=True)
        self._title_thread.start()

    def _stop_title_updater(self) -> None:
        self._title_stop.set()
        if self._title_thread is not None:
            self._title_thread.join(timeout=2.0)

    def _title_loop(self) -> None:
        """标题栏状态机（1s 采样 /api/stats）：
           推理中：差分 slot 计数估算实时 tps；
           空闲：状态位退回最近一轮 footer 精确 perf（_last_status），每 5s 刷一次。
           统一格式见 _format_title。
        """
        prev: dict | None = None
        prev_ts = 0.0
        idle_gap = 0
        while not self._title_stop.is_set():
            snap = self._fetch_stats()
            ts = time.monotonic()
            if snap is None:
                prev = None
                self._title_stop.wait(2.0)
                continue
            es = snap.get("engine_state") or {}
            phase = es.get("phase", "idle")
            cache = snap.get("cache") or {}
            if cache:
                self._last_cache = cache
            pp = es.get("prefill") or {}
            dc = es.get("decode") or {}
            if phase == "prefill":
                idle_gap = 0
                proc = int(pp.get("processed") or 0)
                pct = pp.get("pct") or 0.0
                tps: float | None = None
                if prev and prev_ts:
                    prev_proc = int(((prev.get("engine_state") or {}).get("prefill") or {}).get("processed") or 0)
                    dt = ts - prev_ts
                    if dt > 0 and proc > prev_proc:
                        tps = round((proc - prev_proc) / dt, 1)
                status = f"PP {tps} t/s {pct}%" if tps is not None else f"PP {pct}%"
            elif phase == "decode":
                idle_gap = 0
                dec = int(dc.get("decoded") or 0)
                tps2 = None
                if prev and prev_ts:
                    prev_dec = int(((prev.get("engine_state") or {}).get("decode") or {}).get("decoded") or 0)
                    dt = ts - prev_ts
                    if dt > 0 and dec > prev_dec:
                        tps2 = round((dec - prev_dec) / dt, 1)
                status = f"TG {tps2} t/s" if tps2 is not None else "TG"
            else:
                idle_gap += 1
                if idle_gap < 5:
                    prev = snap
                    prev_ts = ts
                    self._title_stop.wait(1.0)
                    continue
                idle_gap = 0
                status = self._last_status or "idle"
            _set_title(self._format_title(status, cache))
            prev = snap
            prev_ts = ts
            self._title_stop.wait(1.0)

    def _format_title(self, status: str, cache: dict) -> str:
        """标题栏统一格式：
           AI-Qwen-Max | model | 状态 | cache 命中% | hit 命中token | RAM | SSD | paralle
        """
        model = Path(self.backend.model).stem if self.backend.model else "no-model"
        hit = cache.get("recent_hit_pct")
        hit_tok = int(cache.get("hit_tokens") or 0)
        ram = _fmt_bytes((cache.get("ram_pool") or {}).get("bytes", 0))
        ssd = _fmt_bytes((cache.get("ssd_pool") or {}).get("bytes", 0))
        npar = cache.get("n_slots") or 0
        bits = ["AI-Qwen-Max", model, status]
        if hit:
            bits.append(f"cache {hit}%")
        if hit_tok:
            bits.append(f"hit {hit_tok}t")
        bits.append(f"RAM {ram}")
        bits.append(f"SSD {ssd}")
        if npar:
            bits.append(f"paralle {npar}")
        return " | ".join(bits)

    def _fetch_stats(self) -> dict | None:
        try:
            r = self._client.get("/api/stats")
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def _print_header(self, model: str, ctx: int, effort: str) -> None:
        ctx_label = f"{ctx // 1024}K"
        pct = EFFORT_THINK_PCT.get(effort, 0.0)
        budget = int(ctx * pct)
        effort_label = effort if pct <= 0 else f"{effort}·think {budget // 1024}K"
        print("\n" + _CYAN + self.L("hdr_loaded", model=Path(model).name,
                                     ctx=ctx_label, effort=effort_label) + _RESET)

    # ---- 语言 ----

    def _select_language(self) -> None:
        saved = str(self.cfg.get("lang", "zh"))
        print(f"\n选择语言 / Language:  [1] 中文   [2] English   (Enter = {saved})")
        while True:
            raw, esc = _read_line_esc("> ")
            if esc:
                raise SystemExit(0)
            if not raw:
                self.lang = saved if saved in STR else "zh"
                break
            if raw in ("1", "zh", "cn"):
                self.lang = "zh"
                break
            if raw in ("2", "en"):
                self.lang = "en"
                break
            print(self.L("err_invalid"))
        self.cfg.data["lang"] = self.lang
        self.cfg.save()

    # ---- 模型（序号选已有 / 输路径新增，参照旧版设计） ----

    def _select_model(self, preset: str = "") -> str:
        while True:
            models = [m for m in (self.cfg.get("models") or []) if Path(m).exists()]
            print(f"\n===== {self.L('model_title')} =====")
            if models:
                for i, m in enumerate(models, 1):
                    print(f"  {i}. {m}")
            else:
                print(self.L("model_empty"))
            print(f"  {self.L('quit')}")
            if preset:
                if preset.isdigit() and 1 <= int(preset) <= len(models):
                    return models[int(preset) - 1]
                if Path(preset).exists():
                    return preset
            raw, esc = _read_line_esc(self.L("model_prompt"))
            if esc:
                raise SystemExit(0)
            raw = raw.strip().strip('"').strip("'")   # 兼容拖拽带引号路径
            if not raw:
                continue
            if raw.isdigit() and not Path(raw).exists():
                i = int(raw) - 1
                if not (0 <= i < len(models)):
                    print(self.L("model_bad_idx", n=len(models)))
                    continue
                return models[i]
            p = str(Path(raw).expanduser().resolve())
            if not Path(p).exists():
                print(self.L("model_not_file", p=p))
                continue
            if p not in models and p.lower().endswith(".gguf"):
                registered = list(self.cfg.get("models") or [])
                registered.append(p)
                self.cfg.data["models"] = registered
                self.cfg.save()
                print(_dim(self.L("model_registered")))
            return p

    # ---- 上下文档位 ----

    def _select_ctx(self, preset: int = 0) -> int:
        default = self.cfg.get("default_ctx", 32768)
        labels = {c: f"{c // 1024}K" for c in CTX_CHOICES}
        if preset in CTX_CHOICES:
            return preset
        menu = "  ".join(f"{i}. {labels[c]}" for i, c in enumerate(CTX_CHOICES, 1))
        di = (CTX_CHOICES.index(default) if default in CTX_CHOICES
              else CTX_CHOICES.index(32768))
        print(f"\n{self.L('ctx_title')}： {menu} (Enter = {labels[CTX_CHOICES[di]]})")
        while True:
            raw, esc = _read_line_esc(self.L("ctx_prompt"))
            if esc:
                raise SystemExit(0)
            raw = raw.strip()
            if not raw:
                return CTX_CHOICES[di]
            if raw.isdigit() and 1 <= int(raw) <= len(CTX_CHOICES):
                return CTX_CHOICES[int(raw) - 1]
            print(self.L("err_invalid"))

    # ---- 思考强度 ----

    def _select_effort(self) -> str:
        saved = str(self.cfg.get("reasoning_effort", "low"))
        saved = saved if saved in EFFORT_CHOICES else "low"
        print(f"\n{self.L('effort_title')}：  " +
              "  ".join(f"{i}. {e}" for i, e in enumerate(EFFORT_CHOICES, 1)) +
              f"  (Enter = {saved})")
        while True:
            raw, esc = _read_line_esc(self.L("effort_prompt"))
            if esc:
                raise SystemExit(0)
            raw = raw.strip()
            if not raw:
                choice = saved
            elif raw.isdigit() and 1 <= int(raw) <= len(EFFORT_CHOICES):
                choice = EFFORT_CHOICES[int(raw) - 1]
            elif raw in EFFORT_CHOICES:
                choice = raw
            else:
                print(self.L("err_invalid"))
                continue
            self.cfg.data["reasoning_effort"] = choice
            self.cfg.save()
            self.llm.effort = choice
            return choice

    # ---- 载入（spinner + 计时） ----

    def _load(self, model: str, ctx: int) -> tuple[str | None, float]:
        """返回 (错误信息|None, 耗时秒)。"""
        _set_title("AI-Qwen-Max | loading...")
        stop = threading.Event()
        t0 = time.monotonic()

        def spin() -> None:
            frames = "|/-\\"
            i = 0
            while not stop.is_set():
                el = int(time.monotonic() - t0)
                sys.stdout.write(f"\r  {self.L('loading')} {frames[i % 4]} {el}s ")
                sys.stdout.flush()
                stop.wait(0.1)
                i += 1

        th = threading.Thread(target=spin, daemon=True)
        th.start()
        err: str | None = None
        try:
            self.backend.start(model, ctx)
        except Exception as e:
            err = str(e)
        stop.set()
        th.join()
        sys.stdout.write("\r" + " " * 60 + "\r")   # 清掉 spinner 行
        return err, time.monotonic() - t0

    # ================= 功能选单 =================

    def _menu_loop(self) -> None:
        while True:
            print(f"\n===== {self.L('menu_title')} =====")
            print(f"  1. {self.L('menu_chat')}")
            print(f"  2. {self.L('menu_del')}")
            print(f"  3. {self.L('menu_log')}")
            print(f"  {self.L('quit')}")
            raw, esc = _read_line_esc(self.L("menu_prompt"))
            if esc:                                   # 返回/退出只认 ESC
                return
            raw = raw.strip()
            if raw == "1":
                self._chat_entry()
            elif raw == "2":
                self._delete_entry()
            elif raw == "3":
                self._apilog_view()
            # 其余输入：重新打印选单

    # ---- 1. 对话 ----

    def _chat_entry(self) -> None:
        session = self._select_session()
        if session is None:
            return
        self.chat_loop(session)

    def _select_session(self) -> Session | None:
        """会话选择；ESC → None（返回选单）。"""
        sessions = self.store.list()
        print(f"\n{self.L('sessions')}")
        print(f"  {self.L('session_new')}")
        shown = sessions[:30]
        if not shown:
            print(_dim(f"  {self.L('session_none')}"))
        for i, s in enumerate(shown):
            ctx_tag = f" @{s.get('ctx')}" if s.get("ctx") else ""
            print(f"  [{i}] {s['title']}  ({s['n_messages']} 条{ctx_tag})  {s.get('created', '')}")
        while True:
            raw, esc = _read_line_esc(self.L("session_pick"))
            if esc:
                return None
            raw = raw.strip().lower()
            if raw == "n":
                return self.store.create(ctx=self.cfg.get("default_ctx", 32768))
            if raw.isdigit() and int(raw) < len(shown):
                s = self.store.get(shown[int(raw)]["session_id"])
                if s is not None:
                    return s
            print(self.L("session_bad"))

    def chat_loop(self, session: Session) -> None:
        self._replay(session)
        print(_dim(self.L("chat_hint")))
        while True:
            line, esc = _read_line_esc(f"{_CYAN}{self.L('you')}{_RESET} ")
            if esc:
                return                              # ESC → 功能选单
            line = line.rstrip()
            if not line.strip():
                continue
            self._one_turn(session, line)

    def _one_turn(self, session: Session, line: str) -> None:
        """最基础聊天：纯文本 user 消息 → 流式回复 → 落盘 → 性能报告。"""
        session.append({"role": "user", "content": line})
        if not session.meta.get("title") or session.meta["title"] == "未命名会话":
            title_src = line.strip().replace("\n", " ")[:28]
            if title_src:
                session.meta["title"] = title_src
                session.save_meta()

        _set_title("AI-Qwen-Max | generating...")
        poller = InterruptPoller()
        poller.start()
        shown_reasoning = 0
        t_first_write = False
        t0 = time.monotonic()
        res = GenResult()
        try:
            stream = self.llm.chat_stream(session.messages, stop=poller.interrupt, result=res)
            for kind, delta in stream:
                if kind == "reasoning":
                    # 思考流首 token = TTFT（PP 结束、模型开始吐内容）
                    if res.ttft_s is None:
                        res.ttft_s = time.monotonic() - t0
                    res.reasoning += delta
                    if shown_reasoning < THINK_BUDGET:
                        piece = res.reasoning[shown_reasoning:THINK_BUDGET]
                        if not t_first_write:
                            sys.stdout.write(_dim(self.L("think_tag") + "\n"))
                            t_first_write = True
                        sys.stdout.write(_dim(piece))
                        sys.stdout.flush()
                        shown_reasoning += len(piece)
                        if len(res.reasoning) >= THINK_BUDGET:
                            sys.stdout.write(_dim(self.L("think_cut") + "\n"))
                else:
                    if res.ttft_s is None:
                        res.ttft_s = time.monotonic() - t0
                    res.content += delta
                    sys.stdout.write(delta)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            print(_dim("\n" + self.L("interrupted")))
        except Exception as e:   # 后端不可达等
            print(f"\n{_YELLOW}{self.L('gen_fail', e=e)}{_RESET}")
            poller.close()
            return
        finally:
            poller.close()
            if t_first_write and not res.content:
                sys.stdout.write("\n")

        interrupted = poller.interrupt.is_set()
        tail = "\n" + self.L("interrupted") if interrupted else ""
        print(tail)
        assistant: dict[str, Any] = {"role": "assistant", "content": res.content}
        if res.reasoning:
            assistant["reasoning_content"] = res.reasoning
        if res.content or res.reasoning:
            session.append(assistant)
        interrupted_perf = poller.interrupt.is_set()
        perf_line = self._format_perf(res, interrupted_perf)
        if perf_line:
            print(_dim(perf_line))
        # footer 精确 perf → idle 状态位（标题栏状态段，5s 内生效）
        parts = []
        if res.prefill_tps:
            parts.append(f"PP {res.prefill_tps:.0f} t/s")
        if res.decode_tps:
            parts.append(f"TG {res.decode_tps:.1f} t/s")
        if parts:
            self._last_status = " · ".join(parts)
        _set_title(self._format_title(self._last_status or "idle", self._last_cache))

    def _format_perf(self, res, interrupted: bool) -> str:
        """按 max.py 的风格产出一行暗色内联统计。

        max.py 性能行格式（_print_turn_stats）：
          ── ctx {n}/{ctx} (x.x%) │ TTFT 1.2s │ PP 664 t/s │ TG 16.91 t/s [│ 已中断] ──
        """
        bits: list[str] = []
        # ctx n/total (%)
        ctx_total = getattr(self.backend, "ctx", None)
        if res.prompt_tokens is not None and res.completion_tokens is not None and ctx_total:
            n = res.prompt_tokens + res.completion_tokens
            bits.append(f"ctx {n}/{ctx_total} ({100.0 * n / ctx_total:.1f}%)")
        # TTFT · PP · TG
        bits.append(
            f"TTFT {res.ttft_s:.1f}s" if res.ttft_s is not None else "TTFT --")
        bits.append(
            f"PP {res.prefill_tps:.1f} t/s" if res.prefill_tps else "PP --")
        bits.append(
            f"TG {res.decode_tps:.2f} t/s" if res.decode_tps else "TG --")
        if interrupted:
            bits.append("已中断(半截保留)")
        return f"── {' │ '.join(bits)} ──" if bits else ""

    # ---- 2. 删除历史 ----

    def _delete_entry(self) -> None:
        while True:
            sessions = self.store.list()
            shown = sessions[:30]
            if not shown:
                print(f"\n{self.L('session_none')}")
                return
            print(f"\n{self.L('sessions')}")
            for i, s in enumerate(shown):
                print(f"  [{i}] {s['title']}  ({s['n_messages']} 条)  {s.get('created', '')}")
            raw, esc = _read_line_esc(self.L("session_pick"))
            if esc:
                return                              # ESC → 功能选单
            raw = raw.strip()
            if not (raw.isdigit() and int(raw) < len(shown)):
                print(self.L("session_bad"))
                continue
            target = shown[int(raw)]
            # y/n 确认；ESC = n
            sys.stdout.write(self.L("del_confirm", t=target["title"]))
            sys.stdout.flush()
            key = _read_key().lower()
            print()
            if key == "\x1b" or key == "n":
                print(_dim(self.L("del_no")))
                continue
            if key == "y":
                self.store.delete(target["session_id"])
                print(self.L("del_done", t=target["title"]))
                continue
            print(self.L("err_invalid"))

    # ---- 3. API 日志 ----

    def _apilog_view(self) -> None:
        print(f"\n{self.L('log_title')}")
        print(_dim(self.L("log_hint")))
        seen = 0
        ring = getattr(self.events, "_ring", None)
        if ring is None:
            return
        # 进入时回放最近 20 条
        for rec in list(ring)[-20:]:
            self._print_event(rec)
            seen = max(seen, rec.get("_v", 0))
        # 实时轮询，ESC 退出
        while True:
            time.sleep(0.1)
            for _ in range(3):
                if _esc_pressed():
                    return
                time.sleep(0.1)
            for rec in list(ring):
                if rec.get("_v", 0) > seen:
                    self._print_event(rec)
                    seen = rec["_v"]

    def _print_event(self, rec: dict) -> None:
        ts, method, path = rec.get("ts", ""), rec.get("method", "?"), rec.get("path", "?")
        summary = str(rec.get("summary", "")).strip()
        if rec.get("error"):
            print(f"{_YELLOW}{self.L('log_err', ts=ts, method=method, path=path, err=rec['error'])}{_RESET}")
            if summary:
                print(_dim(f"    → {summary}"))
            return
        if rec.get("status") is None:
            line = self.L("log_pending", ts=ts, method=method, path=path)
            if summary:
                line += _dim(f"  → {summary}")   # pending 时直接带摘要：知道在跑什么
            print(_dim(line) if not summary else line)
            return
        line = self.L("log_line", ts=ts, method=method, path=path,
                      status=rec.get("status"), dur=rec.get("dur_s", 0))
        if rec.get("status", 500) >= 400:
            print(f"{_YELLOW}{line}{_RESET}")
        else:
            print(line)
        body = summary or str(rec.get("body", "")).strip()
        if body:
            print(_dim(f"    → {body[:100]}"))
        text = str(rec.get("text", "")).strip()
        reasoning = rec.get("reasoning", "")
        if text or reasoning:
            tag = (f"r:{len(reasoning)}ch " if reasoning else "") + text[-120:]
            print(_dim(f"    ← {tag}"))

    # ================= 辅助 =================

    def _replay(self, session: Session) -> None:
        """进入会话时回放历史（assistant 只显示回答，思考给摘要行）。"""
        if not session.messages:
            return
        print(_dim(self.L("replay_head", t=session.title, n=len(session.messages))))
        for m in session.messages[-12:]:
            role = m.get("role", "?")
            content = m.get("content")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    elif isinstance(p, dict) and p.get("type") == "image_url":
                        parts.append("[图片]")
                content = " ".join(x for x in parts if x)
            text = str(content or "").strip()
            if role == "user":
                print(f"{_CYAN}{self.L('you')}{_RESET} {text[:400]}")
            elif role == "assistant":
                r = m.get("reasoning_content")
                if r:
                    print(_dim(self.L("think_chars", n=len(r))))
                print(f"{_YELLOW}AI:{_RESET} {text[:600]}")
        print(_dim(self.L("replay_end")))
