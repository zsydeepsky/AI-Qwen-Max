"""孤儿进程检测与复用：AI-Qwen-Max CLI 集群注册表 + 孤儿 llama-server attach。

问题背景：CLI 被强杀（任务管理器/崩溃）时 __main__ 的 finally 不执行，
其 spawn 的 llama-server（占 ~71GB 内存）变成孤儿进程。本模块用
.max/runtime.json 注册表追踪本机所有 CLI 与其 spawn 的 llama-server，
新 CLI 启动时做孤儿检测：

  1. 注册表里 pid 已死的 CLI → 其 llama-server 是孤儿，可被新 CLI attach 复用
     （模型已加载在内存，零冷加载成本）；
  2. pid 存活但 /backend 探测持续失败的 CLI → 僵死进程，直接终结；
  3. 无孤儿 llama-server → 新 CLI 走正常"选模型 + 启动"流程。

注册表只记录本工具自己的进程（命令行匹配不是必需——条目由 CLI 启动/退出
时写入/删除，pid 存活即可判定归属）。

注：Windows 专用（本工具硬约束）。非 Windows 上 pid 探测降级为 tasklist。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

FILE_NAME = "runtime.json"


def registry_path(inst_dir: Path) -> Path:
    return inst_dir / FILE_NAME


def _load(inst_dir: Path) -> dict[str, Any]:
    p = registry_path(inst_dir)
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {"cli": []}


def _save(inst_dir: Path, data: dict[str, Any]) -> None:
    p = registry_path(inst_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, p)   # 原子替换，避免并发读半截文件


def _cli_entries(inst_dir: Path) -> list[dict[str, Any]]:
    return _load(inst_dir).get("cli", [])


def register(inst_dir: Path, pid: int, port: int) -> None:
    """登记一个新 CLI。已存在同 pid 条目则更新 port。"""
    data = _load(inst_dir)
    cli = data.setdefault("cli", [])
    for i, e in enumerate(cli):
        if e.get("pid") == pid:
            e["port"] = port
            e["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save(inst_dir, data)
            return
    cli.append({"pid": pid, "port": port,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "llama": None})
    _save(inst_dir, data)


def unregister(inst_dir: Path, pid: int) -> None:
    """CLI 退出时注销自己。"""
    data = _load(inst_dir)
    data["cli"] = [e for e in data.get("cli", []) if e.get("pid") != pid]
    _save(inst_dir, data)


def set_llama(inst_dir: Path, pid: int, llama: dict[str, Any]) -> None:
    """给 CLI 条目记录其 spawn/attach 的 llama-server（pid/port/model/ctx）。"""
    data = _load(inst_dir)
    for e in data.get("cli", []):
        if e.get("pid") == pid:
            e["llama"] = llama
            break
    _save(inst_dir, data)


def pid_alive(pid: int) -> bool:
    """进程是否存活。Windows 用 OpenProcess（同用户可查询），异常降级 False。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            pass
    # 非 Windows / ctypes 异常兜底
    r = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}"],
                       capture_output=True, text=True, timeout=10)
    return str(pid) in r.stdout


def probe_backend(port: int, tries: int = 3) -> dict[str, Any] | None:
    """探测 127.0.0.1:{port}/backend。持续失败返回 None（判定为僵死/不存在）。"""
    url = f"http://127.0.0.1:{port}/backend"
    for _ in range(tries):
        try:
            r = httpx.get(url, timeout=1.5)
            if r.status_code == 200:
                return r.json()
        except (httpx.HTTPError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return None


def probe_llama(port: int, tries: int = 3) -> bool:
    """快速探测孤儿 llama-server 是否可用（/health 且 status=ok）。

    供 attach 前调用：孤儿可能已退出或 PID 被无关进程复用，直接 attach 会让
    wait_ready 空转 READY_TIMEOUT_S 阻塞启动。持续失败返回 False。
    """
    url = f"http://127.0.0.1:{port}/health"
    for _ in range(tries):
        try:
            r = httpx.get(url, timeout=1.5)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return True
        except (httpx.HTTPError, OSError, ValueError):
            pass
        time.sleep(0.3)
    return False


def terminate(pid: int) -> None:
    """强杀进程。Windows 用 taskkill /F，异常忽略（进程可能已退出）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(int(pid))],
                           capture_output=True, timeout=10)
        else:
            os.kill(int(pid), 9)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _is_max_cli(pid: int) -> bool:
    """确认 pid 进程命令行确为 AI-Qwen-Max（防 PID 复用误杀无关进程）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, timeout=10)
        cl = (r.stdout or "").lower()
        return "ai_qwen_max" in cl or "max.exe" in cl
    except Exception:
        return False


def detect(inst_dir: Path, self_pid: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """孤儿检测（排除 self_pid）。

    返回 (zombie_clis, orphan_llamas)：
      zombie_clis   pid 存活、端口有效、/backend 持续不可达且确为 max CLI 的
                    僵死进程（应终结）——探测失败但非 max 进程（PID 已被无关
                    进程复用）一律忽略，不误杀；
      orphan_llamas 已失效 CLI 条目遗留的 llama-server（可 attach 复用）：
                    含 pid 已死、端口非法（0/越界，无法探测）、PID 被复用
                    导致身份不一致三类情况。
    """
    zombie_clis: list[dict[str, Any]] = []
    orphan_llamas: list[dict[str, Any]] = []
    for e in _cli_entries(inst_dir):
        pid = int(e.get("pid") or 0)
        if pid == self_pid or pid <= 0:
            continue
        llama = e.get("llama")
        port = int(e.get("port") or 0)
        # 存活且端口有效 → 可能是真活 CLI：探测 /backend 并核对身份
        if pid_alive(pid) and (1 <= port <= 65535):
            info = probe_backend(port)
            if info is not None and info.get("pid") == pid:
                continue          # 确认存活且身份一致 → 正常实例
            if info is None:
                if _is_max_cli(pid):
                    zombie_clis.append({"pid": pid, "port": port})
                continue          # 僵死(zombie) 或 PID 被无关进程复用 → 不动其 llama
            # info.pid != 注册 pid → PID 被另一个 max 实例复用 → 旧条目失效，按死条目处理
        # 已失效条目（pid 死 / 端口非法 / PID 复用）：遗留 llama-server 是孤儿
        if llama and llama.get("port"):
            llama = dict(llama)
            llama["_from_cli_pid"] = pid
            orphan_llamas.append(llama)
    return zombie_clis, orphan_llamas
