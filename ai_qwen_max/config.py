"""配置：.max/config.json 加载/保存（原子写），默认值即生产基线。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# 上下文档位（per-slot token 数）
CTX_CHOICES = [4096, 8192, 16384, 32768, 65536, 131072, 262144]

DEFAULT_CONFIG: dict[str, Any] = {
    # 模型对象列表：{"path": GGUF 绝对路径,
    #               "DFlash2_draft_model": 可选 DFlash2 草稿模型路径,
    #               "spec_n_max": 可选草稿 token 上限（默认 3，DFlash2 上限=block size 8）}
    "models": [],
    "default_model": "",         # 上次交互选中的模型（启动时 Enter 直接使用）
    "lang": "zh",                # 界面语言 zh | en
    "default_ctx": 32768,        # 新会话默认档位
    "verbosity": 3,              # llama-server 日志级别（4 = slot 级 trace）
    "port": 8080,                # 前端 HTTP（CLI 启动时可改）
    "threads": 16,               # CPU 线程（Zen5 全核）
    "ubatch": 4096,              # prefill 微批
    "cache_ram_mib": 49152,      # RAM prompt-cache 池
    "cache_ssd_mib": 65536,      # SSD prompt-cache 池（0=关 -1=无限）
    "cache_ssd_ttl_hours": 24,   # SSD 条目存活时间
    "cache_snapshot_ttl_hours": 720,  # snapshot 条目存活时间（小时；默认 30 天）
    "avail_mem_min_gb": 4,       # 系统可用内存低于此值不做主动驱逐判断
    "cache_ram_target_gb": 16,   # RAM 池软上限（驱逐参考）
    "reasoning_effort": "low",   # off | low | medium | xHigh（思考预算百分比见 effort_think_pct）
    # effort → 思考预算占输出窗口百分比（off 恒定 0；预算耗尽时强制 </think> 收尾，
    # 收尾文本由 default_reasoning_budget_injection 注入）。可在 .max/config.json 覆盖。
    "effort_think_pct": {"off": 0.0, "low": 0.03, "medium": 0.15, "xHigh": 0.30},
    # 思考预算耗尽时在 </think> 前注入的收尾文本（模型第一人称自嗓音，让模型自然收尾）；
    # 默认值针对 Qwen3.6 调优，后续按模型拆分配置时在 .max/config.json 覆盖
    "default_reasoning_budget_injection": (
        "\n\n...wait, I'm approaching the output limit. I must stop analyzing now.\n"
        "I've already worked out the key points above — they are sufficient.\n"
        "I can always make changes later, it's good enough for now.\n"
        "Okay, let me close my thinking here and write the final answer directly,\n"
        "keeping it clear and concise.\n\n"
    ),
}


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data.update(raw)
            except (json.JSONDecodeError, OSError):
                # 损坏的配置：保留默认值继续跑，不崩
                pass
        # 兼容旧配置：
        # - 剔除已废弃的 power 档位键与 use_mtp（MTP 已完全被 DFlash2 取代）
        # - models 从字符串路径列表升级为对象列表 {"path": ..., "DFlash2_draft_model": ...}
        self.data.pop("power", None)
        self.data.pop("use_mtp", None)
        models = self.data.get("models") or []
        if models and all(isinstance(m, str) for m in models):
            self.data["models"] = [{"path": m} for m in models if m]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent,
            prefix=".config-", suffix=".tmp", delete=False)
        try:
            json.dump(self.data, tmp, ensure_ascii=False, indent=2)
            tmp.close()
            os.replace(tmp.name, self.path)
        except BaseException:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    # dict-like 只读访问
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)
