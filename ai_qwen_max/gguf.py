"""GGUF 元数据探测：仅用标准库解析头部键值，判断模型是否带 MTP (nextn) 层。"""

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


def nextn_layer_count(path: str | Path) -> int:
    """返回 {arch}.nextn_predict_layers 的层数（>0 表示模型内嵌 MTP draft 层）。

    例如 Qwen3.8-27B: qwen3.nextn_predict_layers = 1；A3B 无此键或为 0。
    """
    try:
        hit = _find_key(path, ".nextn_predict_layers")
    except (OSError, ValueError, struct.error):
        return 0
    if hit is None:
        return 0
    try:
        return int(hit[1])  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def model_arch(path: str | Path) -> str:
    try:
        hit = _find_key(path, "general.architecture")
    except (OSError, ValueError, struct.error):
        return ""
    return str(hit[1]) if hit else ""
