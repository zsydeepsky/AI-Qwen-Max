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
    "models": [],                # GGUF 绝对路径列表
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
    "avail_mem_min_gb": 4,       # 系统可用内存低于此值不做主动驱逐判断
    "cache_ram_target_gb": 16,   # RAM 池软上限（驱逐参考）
    "reasoning_effort": "low",   # off | low | medium | xHigh（思考预算按档绑定 max_output 百分比：3%/10%/30%）
    "use_mtp": True,             # 投机解码（模型支持 nextn 时自动启用）
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
        # 兼容旧配置：剔除已废弃的 power 档位键
        self.data.pop("power", None)

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
