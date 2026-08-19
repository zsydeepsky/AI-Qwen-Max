"""程序入口：组装 config / backend / store / HTTP server / CLI。

用法（pip install -e . 后用 max 命令，否则 python -m ai_qwen_max 等效）：
  max                      交互模式（CLI 对话 + HTTP 服务）
  max --serve              纯服务模式（无 CLI）
  max --model <路径|序号>   跳过模型选择
  max --ctx 65536          跳过档位选择
  max --port 8080          前端端口
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

import uvicorn

from . import __version__
from .backend import Backend
from .cli import Cli
from .config import Config
from .orphan import detect, probe_llama, register, terminate, unregister
from .server import AppCtx, create_app
from .store import SessionStore


def _find_server_exe(root: Path) -> Path | None:
    """定位 llama-server.exe：开发态 build/bin/，打包态 sys._MEIPASS/llama/。"""
    frozen = getattr(sys, "frozen", False)
    if frozen:
        p = Path(sys._MEIPASS) / "llama" / "llama-server.exe"   # type: ignore[attr-defined]
        if p.exists():
            return p
    for p in (root / "build" / "bin" / "llama-server.exe",
              root / "llama" / "llama-server.exe"):
        if p.exists():
            return p
    return None


def main() -> int:
    parser = argparse.ArgumentParser(prog="max", description="AI-Qwen-Max 本地推理服务")
    parser.add_argument("--serve", action="store_true", help="纯 HTTP 服务模式（无 CLI）")
    parser.add_argument("--model", default="", help="模型路径或 config.models 序号")
    parser.add_argument("--ctx", type=int, default=0, help="上下文档位（4K/16K/64K/256K）")
    parser.add_argument("--port", type=int, default=0, help="前端 HTTP 端口")
    args = parser.parse_args()

    if getattr(sys, "frozen", False):
        root = Path(sys.executable).parent        # 绿色版：max.exe 所在目录
    else:
        root = Path(__file__).resolve().parent.parent   # 开发态：仓库根（不随 cwd 漂移）
    inst_dir = root / ".max"
    inst_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(inst_dir / "config.json")

    server_exe = _find_server_exe(root)
    if server_exe is None:
        print("未找到 llama-server.exe：先运行 scripts/build.ps1（开发）或检查打包完整性。")
        return 1

    backend = Backend(cfg, inst_dir, server_exe)
    store = SessionStore(inst_dir)
    actx = AppCtx(cfg, backend, store, inst_dir)
    app = create_app(actx)

    # ---- 前端端口：交互模式下由用户选择（语言 → 端口），--serve 用参数/配置 ----
    cli: Cli | None = None
    if args.serve:
        if args.port:
            cfg.data["port"] = args.port
            cfg.save()
    else:
        cli = Cli(cfg, backend, store, actx.events, frontend_port=0)
        cli.select_startup()      # 语言 + 前端端口（写入 cfg["port"]，含占用探测）
        if args.port:             # 命令行显式端口优先于交互输入
            cfg.data["port"] = args.port
            cli.frontend_port = args.port
            cfg.save()

    # ---- 启动 HTTP（daemon 线程）----
    uconfig = uvicorn.Config(app, host="127.0.0.1", port=int(cfg["port"]),
                             log_level="warning")
    userver = uvicorn.Server(uconfig)
    actx.uvicorn_server = userver
    t = threading.Thread(target=userver.run, daemon=True)
    t.start()

    # 先登记自己：attach/start 里的 set_llama 依赖本条目的存在，注册须在前
    register(inst_dir, os.getpid(), int(cfg["port"]))

    # ---- 孤儿检测：僵死 CLI 终结 + 孤儿 llama-server attach 复用 ----
    # 目标：避免 CLI 被强杀后 llama-server（~71GB）沦为孤儿；有孤儿则直接接入
    # 复用（模型仍在共享内存），退出本 CLI 时自然触发其退出。
    zombie_clis, orphan_llamas = [], []
    try:
        zombie_clis, orphan_llamas = detect(inst_dir, os.getpid())
    except Exception as e:      # 注册表损坏等极端情况不阻断启动
        print(f"[warn] 孤儿检测失败，继续正常启动：{e}")
    for z in zombie_clis:
        print(f"检测到僵死 AI-Qwen-Max 进程 (pid={z['pid']})，正在终结…")
        terminate(z["pid"])
        unregister(inst_dir, z["pid"])
    attach_info: tuple[str, int] | None = None
    if orphan_llamas:
        if len(orphan_llamas) > 1:
            print(f"检测到 {len(orphan_llamas)} 个孤儿 llama-server，仅接入第一个，其余保留。")
        o = orphan_llamas[0]
        model, ctx = o.get("model"), o.get("ctx")
        if not model or not ctx:
            print("[warn] 孤儿 llama-server 条目缺 model/ctx，跳过接入。")
        else:
            print(f"检测到孤儿 llama-server（模型 {Path(model).stem}，ctx={ctx}），正在接入…")
            try:
                # 孤儿可能已退出/PID 被无关进程复用：先快速预探测 /health，
                # 避免 attach 的 wait_ready（600s）空转拖死启动
                if probe_llama(int(o["port"])):
                    backend.attach(int(o["port"]), model, int(ctx), int(o["pid"]))
                    attach_info = (model, int(ctx))
                else:
                    print(f"[warn] 孤儿 llama-server（port={o['port']}）已不可达，跳过接入。")
            except Exception as e:
                print(f"[warn] 接入失败（{e}），将走正常启动流程。")
                attach_info = None
            finally:
                # 来源条目 CLI 已死：无论接入成败该条目都已失效，注销以免残留
                from_pid = o.get("_from_cli_pid")
                if from_pid:
                    unregister(inst_dir, int(from_pid))

    try:
        if args.serve:
            print(f"AI-Qwen-Max v{__version__} 服务模式：http://127.0.0.1:{cfg['port']}  (/help)")
            while t.is_alive():
                t.join(timeout=1.0)
        else:
            assert cli is not None
            if attach_info:
                # attach 模式：跳过模型/档位/思考强度选择，直接复用孤儿服务器
                cli.run_attached(*attach_info)
            else:
                # llama-server 在选定模型后由 _load 启动：--port 0 由 OS 分配可用端口，
                # Backend 从日志发现实际端口后自动对接，用户无需关心
                cli.run(model_preset=args.model, ctx_preset=args.ctx)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        print("\n正在关闭（保存 KV 缓存到 SSD）…")
        backend.stop()
        unregister(inst_dir, os.getpid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
