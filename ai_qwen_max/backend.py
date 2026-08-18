"""llama-server 子进程管理：启动参数拼装、就绪探测、模型热切换、优雅退出。"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .gguf import nextn_layer_count

PARALLEL = 3          # slot 数：2 路 API + 1 路 CLI/Web
READY_TIMEOUT_S = 600  # 大模型冷加载宽限
SHUTDOWN_GRACE_S = 30

# llama-server 打印实际监听地址的行（HTTP bind 在模型加载前，但此 INFO 行在加载后输出）
_LISTEN_RE = re.compile(rb"listening on http://[^:/\s]+:(\d+)")


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
        self._client: httpx.Client | None = None

    # ---- 进程生命周期 ----

    def build_cmd(self, model: str, ctx: int) -> list[str]:
        cfg = self.cfg
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
            # KV 量化锚定 K8V8（q8_0/q8_0）—— 生产定论
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
        # 投机解码：仅当模型内嵌 MTP (nextn) 层
        if cfg.get("use_mtp", True) and nextn_layer_count(model) > 0:
            cmd += ["--spec-type", "draft-mtp"]
        return cmd

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # 引擎定制开关（详见 docs/ENGINE_PATCHES.md）
        env.setdefault("GGML_VK_PREFER_HOST_MEMORY", "1")   # HostCached GTT：快照恢复 45×
        env.setdefault("QWENMAX_FA_F16ACC", "1")            # fp16 注意力累加：prefill +9%
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
        # --port 0：等 server 报出实际监听端口，拿到后才能和它通信
        self.port = self._discover_port(log_path, log_offset)
        self._client = httpx.Client(base_url=self.base_url, timeout=600)
        self.wait_ready()

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

    def stop(self) -> None:
        """优雅退出：POST /max/shutdown（Windows 跨进程信号不可靠），超时硬杀。"""
        if self.proc and self.proc.poll() is None:
            if self._client is not None:
                try:
                    self._client.post("/max/shutdown", timeout=5)
                except httpx.HTTPError:
                    pass
            try:
                self.proc.wait(timeout=SHUTDOWN_GRACE_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None

    # ---- HTTP 便捷 ----

    @property
    def base_url(self) -> str:
        # 端口未知（未启动）时指向 :1，连接立刻失败 → 上层按"未就绪"处理
        return f"http://127.0.0.1:{self.port or 1}"

    def healthy(self) -> bool:
        if not self.proc or self.proc.poll() is not None or self._client is None:
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
