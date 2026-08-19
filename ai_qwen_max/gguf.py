"""GGUF 元数据探测：仅用标准库解析头部键值，判断模型能力（模板/多模态/输出上限）。"""

from __future__ import annotations

import struct
from pathlib import Path

GGUF_MAGIC = b"GGUF"

# 标量类型字节宽（索引 = gguf 类型枚举；8=STRING 与 9=ARRAY 变长，另行处理）
_T_SZ = (1, 1, 2, 2, 4, 4, 4, 1, 0, 0)


def _skip_value(f, vtype: int) -> bool:
    """流式跳过一个 value（不物化）。返回 False 表示无法跳过（嵌套数组等罕见情形）。"""
    if vtype == 8:  # STRING
        (n,) = struct.unpack("<Q", f.read(8))
        f.seek(n, 1)
    elif vtype == 9:  # ARRAY
        (etype,) = struct.unpack("<I", f.read(4))
        (n,) = struct.unpack("<Q", f.read(8))
        if etype == 8:      # STRING 数组（tokenizer vocab 等）：逐项跳读
            for _ in range(n):
                (sl,) = struct.unpack("<Q", f.read(8))
                f.seek(sl, 1)
        elif etype == 9:    # 嵌套数组：gguf 规范未用，放弃
            return False
        else:
            f.seek(_T_SZ[etype] * n, 1)
    else:
        f.read(_T_SZ[vtype])
    return True


def _find_key(path: str | Path, suffix: str) -> tuple[str, object] | None:
    """全量遍历 kv 找第一个以 suffix 结尾的键（对标量值直接返回，不依赖键序）。"""
    p = Path(path)
    with p.open("rb") as f:
        if f.read(4) != GGUF_MAGIC:
            raise ValueError(f"not a GGUF file: {p}")
        (version,) = struct.unpack("<I", f.read(4))
        if version < 2:
            raise ValueError(f"unsupported gguf version {version}: {p}")
        f.read(8)  # tensor_count
        (n_kv,) = struct.unpack("<Q", f.read(8))
        for _ in range(n_kv):
            (klen,) = struct.unpack("<Q", f.read(8))
            key = f.read(klen).decode("utf-8", errors="replace")
            (vtype,) = struct.unpack("<I", f.read(4))
            if key.endswith(suffix) and vtype not in (8, 9):
                return key, _read_value(f, vtype)
            if not _skip_value(f, vtype):
                return None
    return None


def _read_str(f) -> str:
    (n,) = struct.unpack("<Q", f.read(8))
    return f.read(n).decode("utf-8", errors="replace")


def _read_value(f, vtype: int):
    if vtype == 0:  # UINT8
        return struct.unpack("<B", f.read(1))[0]
    if vtype == 1:  # INT8
        return struct.unpack("<b", f.read(1))[0]
    if vtype == 2:  # UINT16
        return struct.unpack("<H", f.read(2))[0]
    if vtype == 3:  # INT16
        return struct.unpack("<h", f.read(2))[0]
    if vtype == 4:  # UINT32
        return struct.unpack("<I", f.read(4))[0]
    if vtype == 5:  # INT32
        return struct.unpack("<i", f.read(4))[0]
    if vtype == 6:  # FLOAT32
        return struct.unpack("<f", f.read(4))[0]
    if vtype == 7:  # BOOL
        return struct.unpack("<B", f.read(1))[0] != 0
    if vtype == 8:  # STRING
        return _read_str(f)
    if vtype == 9:  # ARRAY
        (etype,) = struct.unpack("<I", f.read(4))
        (n,) = struct.unpack("<Q", f.read(8))
        return [_read_value(f, etype) for _ in range(n)]
    raise ValueError(f"unknown gguf value type {vtype}")


def model_arch(path: str | Path) -> str:
    try:
        hit = _find_key(path, "general.architecture")
    except (OSError, ValueError, struct.error):
        return ""
    return str(hit[1]) if hit else ""


def chat_template(path: str | Path) -> str | None:
    """读取 GGUF 内嵌 `tokenizer.chat_template`（字符串 KV）。

    返回 None 表示读取失败或键缺失（_find_key 只处理标量、跳过 STRING，
    这里单独物化字符串）。用于探测模板是否原生支持 reasoning_effort 档位。
    """
    p = Path(path)
    try:
        with p.open("rb") as f:
            if f.read(4) != GGUF_MAGIC:
                return None
            f.read(4)   # version
            f.read(8)   # tensor_count
            (n_kv,) = struct.unpack("<Q", f.read(8))
            for _ in range(n_kv):
                (klen,) = struct.unpack("<Q", f.read(8))
                key = f.read(klen).decode("utf-8", errors="replace")
                (vtype,) = struct.unpack("<I", f.read(4))
                if vtype == 8 and key.endswith("tokenizer.chat_template"):
                    return _read_str(f)
                if not _skip_value(f, vtype):
                    return None
    except (OSError, struct.error):
        return None
    return None


def resolve_model_path(path: str | Path) -> str | None:
    """把用户输入（.gguf 文件或模型目录）解析到实际可加载的 gguf 文件。

    - 已指向 .gguf 文件：原样返回（必须存在）。
    - 指向目录：扫描目录内的 .gguf（优先排除 mmproj* 视觉投影），
      取体积最大的一个作为主模型；目录无 gguf 返回 None。
    - 路径不存在 / 是其它类型文件：返回 None。
    """
    p = Path(path).expanduser().resolve()
    if p.is_file():
        return str(p) if p.suffix.lower() == ".gguf" else None
    if not p.is_dir():
        return None
    try:
        ggufs = [f for f in p.iterdir()
                 if f.is_file() and f.suffix.lower() == ".gguf"]
        mains = [f for f in ggufs if "mmproj" not in f.name.lower()]
        if mains:
            return str(max(mains, key=lambda f: f.stat().st_size))
        if ggufs:
            return str(max(ggufs, key=lambda f: f.stat().st_size))
    except OSError:
        return None
    return None


def find_mmproj(model_path: str | Path) -> str | None:
    """探测模型同目录的配套视觉投影文件（LM Studio 下载惯例：mmproj*.gguf）。

    返回第一个匹配的路径；目录不存在或没有 mmproj 返回 None（纯文本模型正常情况）。
    """
    d = Path(model_path).parent
    try:
        cands = sorted(p for p in d.iterdir()
                       if p.is_file() and p.suffix.lower() == ".gguf"
                       and "mmproj" in p.name.lower())
    except OSError:
        return None
    return str(cands[0]) if cands else None


def model_media(model_path: str | Path) -> str:
    """模型能接受的媒体（文本/图片/音频）：读配套 mmproj 的 clip 元数据。

    有 mmproj 即有视觉塔；音频以 clip.has_audio_encoder 为准（Gemma3 等）。
    老式 mmproj 无 has_vision_encoder 键时按视觉处理（传统 LLaVA 均为视觉）。
    """
    caps = ["文本"]
    mm = find_mmproj(model_path)
    if mm is None:
        return "/".join(caps)
    vision = audio = None
    try:
        hit = _find_key(mm, ".has_vision_encoder")
        vision = bool(hit[1]) if hit else None
    except (OSError, ValueError, struct.error):
        vision = None
    try:
        hit = _find_key(mm, ".has_audio_encoder")
        audio = bool(hit[1]) if hit else None
    except (OSError, ValueError, struct.error):
        audio = None
    if vision is not False:      # True 或未知（老 clip 无此键）都算有视觉
        caps.append("图片")
    if audio is True:
        caps.append("音频")
    return "/".join(caps)


# 模型"最大输出窗口"的候选 GGUF 键（多数转换脚本不写，探测不到时按默认 32K 算）
_MAX_OUTPUT_SUFFIXES = ("output.max_tokens", "max_output_tokens", "max_tokens", "output.max", "max_output")
MAX_OUTPUT_DEFAULT = 32768


def model_max_output(path: str | Path) -> int:
    """返回模型最大输出 token 数；GGUF 探测不到时回退默认 32K。

    用于思考预算的分母（启动参数层静态近似）：budget = min(max_output, ctx) × pct。
    """
    for suffix in _MAX_OUTPUT_SUFFIXES:
        try:
            hit = _find_key(path, suffix)
        except (OSError, ValueError, struct.error):
            return MAX_OUTPUT_DEFAULT
        if hit is not None:
            try:
                v = int(hit[1])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
    return MAX_OUTPUT_DEFAULT
