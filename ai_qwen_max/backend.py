"""llama-server 子进程管理：启动参数拼装、就绪探测、模型热切换、优雅退出。"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from . import orphan
from .config import Config
from .gguf import chat_template, find_mmproj, model_max_output
from .llm import EFFORT_THINK_PCT

PARALLEL = 3          # slot 数：2 路 API + 1 路 CLI/Web
READY_TIMEOUT_S = 600  # 大模型冷加载宽限
SHUTDOWN_GRACE_S = 60  # 含 uvicorn 退出钩子落盘 KV 缓存的耗时，30s 对 384K×3 上下文不够

# llama-server 打印实际监听地址的行（HTTP bind 在模型加载前，但此 INFO 行在加载后输出）
_LISTEN_RE = re.compile(rb"listening on http://[^:/\s]+:(\d+)")


def _spec_draft_config(cfg: Config, model: str) -> dict | None:
    """返回匹配 model 的 DFlash2 投机解码配置（dict），未配置草稿返回 None。

    从 config.models 对象条目读取：
      DFlash2_draft_model  草稿模型路径（必填，文件不存在视为未配置）
      spec_n_max           每步草稿 token 上限（clamp 到草稿 block size；None = 引擎默认 3）
    路径比较用纯字符串规范化（abspath 不触碰文件系统，避免 BitLocker/缺失盘符
    在 resolve() 时抛 OSError 拖垮参数拼装）；草稿文件存在性单独用 is_file 校验。
    注：DFlash 的 --spec-draft-conf-min 在本引擎未实现（仅文档提及），暂不暴露。
    """
    target = os.path.normcase(os.path.abspath(model))
    for m in cfg.get("models") or []:
        if not isinstance(m, dict):
            continue
        if os.path.normcase(os.path.abspath(str(m.get("path") or ""))) != target:
            continue
        draft = str(m.get("DFlash2_draft_model") or "")
        if not draft or not Path(draft).expanduser().is_file():
            return None
        return {
            "type": "dflash",
            "draft": str(Path(draft).expanduser()),
            "n_max": m.get("spec_n_max"),
        }
    return None


class Backend:
    # 实际监听端口：--port 0 由 OS 分配，spawn 后从日志发现。None = 未启动。
    port: int | None = None

    def __init__(self, cfg: Config, root: Path, server_exe: Path):
        self.cfg = cfg
        self.root = root
        self.server_exe = server_exe
        self.proc: subprocess.Popen | None = None
        self.model: str | None = None
        self.ctx: int | None = None
        self.port = None
        # 当前 llama-server 进程 PID。正常 spawn 时 = self.proc.pid；
        # attach 孤儿（非本进程子进程）时记录孤儿 PID，用于注册表与退出确认。
        self.llama_pid: int | None = None
        # 当前模型的 GGUF 内嵌 chat_template（build_cmd 时探测一次）。
        # None 表示未加载或读取失败；用于判定模板是否支持 reasoning_effort 档位。
        self.chat_template: str | None = None
        self._client: httpx.Client | None = None

    # ---- 进程生命周期 ----

    def build_cmd(self, model: str, ctx: int) -> list[str]:
        cfg = self.cfg
        self.chat_template = chat_template(model)   # 探测模板能力（reasoning_effort 降级用）
        cmd = [
            str(self.server_exe),
            "--model", model,
            "--host", "127.0.0.1",
            "--port", "0",               # OS 分配可用端口，避免与其它实例冲突；实际端口从日志发现
            # 档位是 per-slot，总预算 = 档位 × slot 数
            "--ctx-size", str(ctx * PARALLEL),
            "--n-gpu-layers", "999",
            "--threads", str(cfg["threads"]),
            "--ubatch-size", str(cfg["ubatch"]),
            "--flash-attn", "on",
        ]
        # 多模态：模型同目录有配套 mmproj（LM Studio 下载惯例）则挂上视觉塔；
        # 纯文本模型无此文件，自动跳过
        mmproj = find_mmproj(model)
        if mmproj:
            cmd += ["--mmproj", mmproj]
        cmd += [
            "--cache-type-k", "q8_0",
            "--cache-type-v", "q8_0",
            "--parallel", str(PARALLEL),
            "--slot-prompt-similarity", "0.5",
            "--ctx-checkpoints", "128",
            "--cache-ram", str(cfg["cache_ram_mib"]),
            "--cache-reuse", "256",
            "--cache-ssd", str(cfg["cache_ssd_mib"]),
            "--cache-ssd-path", str(self.root / "cache-ssd"),
            "--cache-ssd-ttl-hours", str(cfg["cache_ssd_ttl_hours"]),
            "--verbosity", str(cfg["verbosity"]),
        ]
        # 思考预算：effort 档 × min(模型最大输出, ctx) 作为引擎级默认，覆盖所有客户端——
        # DSH/Web 等第三方调用方不会在请求体里带 reasoning_budget_tokens，若只靠 llm.py
        # 的请求级注入，它们永远没有预算（server 默认 -1 = 不限制）。
        # GGUF 读不到 max output 时按 32K 兜底；CLI 请求仍会用 llm.py 按"输出窗口"
        # 算的精确预算覆盖（请求级字段优先）。
        pct = cfg.get("effort_think_pct", EFFORT_THINK_PCT).get(cfg.get("reasoning_effort", "low"), 0.0)
        budget = int(min(model_max_output(model), ctx) * pct)
        cmd += ["--reasoning-budget", str(budget)]
        if budget > 0:
            # 上游机制：预算耗尽时在 </think> 前注入收尾文本，让模型自然收尾；
            # 注入词来自 config.default_reasoning_budget_injection（可被 .max/config.json 覆盖）
            cmd += ["--reasoning-budget-message", cfg["default_reasoning_budget_injection"]]
        # 投机解码：优先使用配置的 DFlash2 草稿模型（完全取代 MTP——
        # Qwen3.8 内嵌 MTP 层绑定 xHigh 思考，改 reasoning effort 后接受率雪崩
        # 反而拖慢输出；DFlash 独立草稿模型无此耦合）。未配置草稿路径则关闭。
        # spec_n_max 缺省不传，使用引擎默认（n_max=3）。
        spec = _spec_draft_config(cfg, model)
        if spec:
            cmd += ["--spec-type", "draft-dflash", "--spec-draft-model", spec["draft"]]
            if spec["n_max"] is not None:
                cmd += ["--spec-draft-n-max", str(int(spec["n_max"]))]
        return cmd

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # 引擎定制开关（详见 docs/ENGINE_PATCHES.md）
        env.setdefault("GGML_VK_PREFER_HOST_MEMORY", "1")   # HostCached GTT：快照恢复 45×
        env.setdefault("RYZENUMA_FA_F16ACC", "1")           # fp16 注意力累加：prefill +9%
        env.setdefault("GGML_VK_AMD_L_TILES", "0")          # 当前驱动下 l-tile 回退，保持关闭
        return env

    def start(self, model: str, ctx: int) -> None:
        self.stop()
        cmd = self.build_cmd(model, ctx)
        log_path = self.root / "llama-server.log"
        self.root.mkdir(parents=True, exist_ok=True)
        log_offset = log_path.stat().st_size if log_path.exists() else 0
        log_f = open(log_path, "ab")
        log_f.write(f"\n==== spawn {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n".encode())
        log_f.flush()
        self.proc = subprocess.Popen(
            cmd, stdout=log_f, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env=self._env())
        self.model, self.ctx = model, ctx
        self.llama_pid = self.proc.pid
        self.spec_info = _spec_draft_config(self.cfg, model)
        # --port 0：等 server 报出实际监听端口，拿到后才能和它通信
        self.port = self._discover_port(log_path, log_offset)
        self._client = httpx.Client(base_url=self.base_url, timeout=600)
        self.wait_ready()
        orphan.set_llama(self.root, os.getpid(),
                         {"pid": self.llama_pid, "port": self.port,
                          "model": model, "ctx": ctx})

    def _discover_port(self, log_path: Path, offset: int) -> int:
        """轮询日志，从 `listening on http://host:PORT` 行解析实际端口。

        该行在模型加载完成后输出，所以这里的等待 ≈ 冷加载时长（与原 wait_ready 重叠）。
        只从本次 spawn 的写入位置开始找，避免命中历史日志。
        """
        t0 = time.monotonic()
        while time.monotonic() - t0 < READY_TIMEOUT_S:
            if self.proc and self.proc.poll() is not None:
                tail = ""
                try:
                    tail = log_path.read_bytes()[offset:][-600:].decode("utf-8", "replace")
                except OSError:
                    pass
                raise RuntimeError(
                    f"llama-server exited with code {self.proc.returncode}\n{tail}")
            try:
                data = log_path.read_bytes()[offset:]
            except OSError:
                data = b""
            m = _LISTEN_RE.search(data)
            if m:
                return int(m.group(1))
            time.sleep(0.5)
        raise TimeoutError("等待 llama-server 监听端口超时")

    def wait_ready(self) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < READY_TIMEOUT_S:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with code {self.proc.returncode}（详见 .max/llama-server.log）")
            try:
                r = self._client.get("/health", timeout=2)
                if r.status_code == 200:
                    body = r.json()
                    if body.get("status") == "ok" and body.get("loaded", True):
                        return True
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(1.0)
        raise TimeoutError("llama-server 就绪超时")

    def attach(self, port: int, model: str, ctx: int, llama_pid: int) -> None:
        """接管一个孤儿 llama-server（非本进程 spawn 的子进程）。

        复用其已加载的模型（模型仍驻留共享内存，零冷加载成本）；本 CLI 成为其
        管理者，退出时照常发 /max/shutdown 触发其退出。失败（孤儿已死）抛异常，
        调用方回退正常启动流程。
        """
        self.stop()
        self.port = int(port)
        self.model = model
        self.ctx = ctx
        self.llama_pid = int(llama_pid)
        self.proc = None   # 不是本进程的子进程：stop() 只发信号，不 wait/kill
        self._client = httpx.Client(base_url=self.base_url, timeout=600)
        try:
            self.wait_ready()
        except Exception:
            self._client.close()
            self._client = None
            self.port = None
            self.llama_pid = None
            raise
        orphan.set_llama(self.root, os.getpid(),
                         {"pid": self.llama_pid, "port": self.port,
                          "model": model, "ctx": ctx})

    def stop(self) -> None:
        """优雅退出：POST /max/shutdown（Windows 跨进程信号不可靠），超时硬杀。

        uvicorn 退出钩子会先落盘 KV 缓存到 SSD（384K×3 上下文可能耗时数十秒），
        wait 宽限已覆盖该耗时；硬杀后仍不退出的进程直接放弃——
        任何路径都不抛异常，退出流程不被 TimeoutExpired 打断。
        attach 模式（proc 为 None）只发 shutdown 信号，不 wait/kill 非子进程。
        """
        try:
            # 只要有已知端口就发 shutdown（正常 spawn 与 attach 共用）
            if self.port is not None and self._client is not None:
                try:
                    self._client.post("/max/shutdown", timeout=15)
                except (httpx.HTTPError, OSError):
                    pass
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self.proc.wait(timeout=SHUTDOWN_GRACE_S)
                except subprocess.TimeoutExpired:
                    # 硬杀兜底；kill 后仍不退出的进程直接放弃，避免阻塞退出流程
                    try:
                        self.proc.kill()
                        self.proc.wait(timeout=10)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
        finally:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
            self.proc = None

    # ---- HTTP 便捷 ----

    @property
    def base_url(self) -> str:
        # 端口未知（未启动）时指向 :1，连接立刻失败 → 上层按"未就绪"处理
        return f"http://127.0.0.1:{self.port or 1}"

    def healthy(self) -> bool:
        if self.port is None or self._client is None:
            return False
        # attach 模式下 proc 为 None（非子进程），只按服务可用性判定
        if self.proc is not None and self.proc.poll() is not None:
            return False
        try:
            return self._client.get("/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def get(self, path: str, **kw: Any) -> httpx.Response:
        assert self._client is not None, "backend not started"
        return self._client.get(path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        assert self._client is not None, "backend not started"
        return self._client.post(path, **kw)

    def client(self) -> httpx.Client:
        assert self._client is not None, "backend not started"
        return self._client
