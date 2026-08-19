# -*- coding: utf-8 -*-
"""DSH（DeepSeek Harness）适配：同步 ~/.dsh/settings.yaml 中本工程模型的多模态声明。

DSH 的 pi-ai adapter 不探测网关的多模态能力（其源码注释明确：nothing can
interrogate a gateway for its modalities），判定图片能力只信任配置文件里
声明的 input 字段。模型加载成功后，把 baseURL 指向本工程端口的 provider
对应模型条目的 input 同步为 [text, image]（检测到 mmproj）或 [text]（无视觉塔）。
DSH 通过 chokidar 监听该文件，外部修改即时生效，无需重启 DSH。

只更新已存在的模型条目，不新增、不删除，保持 DSH 配置的自主性。
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

try:
    import yaml
except ImportError:      # 未安装 PyYAML：DSH 适配整体静默禁用
    yaml = None

from .gguf import find_mmproj

DSH_HOME = Path.home() / ".dsh"
SETTINGS_FILE = DSH_HOME / "settings.yaml"


def _is_engine_base(base: str, port: int) -> bool:
    """baseURL 是否指向本工程（localhost/127.0.0.1 且端口匹配）。"""
    try:
        u = urlparse(base)
    except ValueError:
        return False
    return u.hostname in ("localhost", "127.0.0.1") and (u.port or 80) == port


def sync_dsh_input(model_path: str, port: int) -> str | None:
    """把 DSH 配置中对应模型的 input 声明同步为实际能力。

    返回变更描述字符串；无变更（配置缺失 / 未找到目标 / 已一致）返回 None。
    失败静默（读取或写回异常返回 None），不打扰模型加载流程。
    """
    if yaml is None or not SETTINGS_FILE.is_file():
        return None
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict):
        return None
    providers = (doc.get("llm-pi-ai") or {}).get("providers")
    if not isinstance(providers, dict):
        return None

    declared = ["text", "image"] if find_mmproj(model_path) else ["text"]
    target = os.path.normcase(str(model_path))
    changed = False
    for profile in providers.values():
        if not isinstance(profile, dict):
            continue
        if not _is_engine_base(str(profile.get("baseURL") or ""), port):
            continue
        models = profile.get("models")
        if not isinstance(models, list):
            continue
        for m in models:
            if not isinstance(m, dict):
                continue
            if os.path.normcase(str(m.get("id") or "")) != target:
                continue
            if m.get("input") != declared:
                m["input"] = declared
                changed = True
    if not changed:
        return None
    # 原子写回，避免与 DSH 自身的写入竞争
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return f"已同步 DSH 配置：模型 input={declared}"
