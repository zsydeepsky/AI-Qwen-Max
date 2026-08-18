"""会话存储：.max/chat/<sid>/ 下的 meta.json + messages.json + dialogue.txt + media/。

全部写入走 tmp+rename 原子替换，崩溃不留半写文件。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

_MSG_FIELDS = ("role", "content", "reasoning_content", "tool_calls", "name", "tool_call_id")


def normalize_messages(msgs: list[dict]) -> list[dict]:
    """持久化前的清洗：只保留已知字段（含工具调用相关字段，完整保留对话结构）。"""
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        clean = {k: m[k] for k in _MSG_FIELDS if m.get(k) is not None}
        if clean.get("role"):
            out.append(clean)
    return out


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}-", suffix=".tmp", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        os.replace(tmp.name, path)
    except BaseException:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def new_sid() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{os.urandom(2).hex()}"


class Session:
    def __init__(self, root: Path):
        self.root = root
        self.meta_path = root / "meta.json"
        self.msgs_path = root / "messages.json"
        self.dialogue_path = root / "dialogue.txt"
        self.media_dir = root / "media"
        self.meta: dict[str, Any] = {}
        self.messages: list[dict] = []
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        try:
            if self.meta_path.exists():
                self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.meta = {}
        try:
            if self.msgs_path.exists():
                raw = json.loads(self.msgs_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self.messages = normalize_messages(raw)
        except (json.JSONDecodeError, OSError):
            self.messages = []

    def save_meta(self) -> None:
        _atomic_write(self.meta_path, json.dumps(self.meta, ensure_ascii=False, indent=2))

    def save_messages(self) -> None:
        _atomic_write(self.msgs_path, json.dumps(self.messages, ensure_ascii=False, indent=1))

    # ---- 消息操作 ----

    def append(self, msg: dict) -> None:
        self.messages.append({k: msg[k] for k in _MSG_FIELDS if msg.get(k) is not None})
        self.save_messages()
        self._append_dialogue(msg)

    def replace_all(self, msgs: list[dict]) -> None:
        self.messages = normalize_messages(msgs)
        self.save_messages()

    def merge_request(self, req_msgs: list[dict], assistant: dict | None) -> None:
        """API 轨（X-Conversation-Id）落盘：req_msgs 为客户端发来的全量历史。

        始终以 req_msgs 为基准全量替换（客户端持有权威历史），
        assistant 回复（含 reasoning_content）最后追加。
        """
        merged = normalize_messages(req_msgs)
        if assistant:
            merged = merged + [{k: assistant[k] for k in _MSG_FIELDS if assistant.get(k) is not None}]
        self.messages = merged
        self.save_messages()
        if assistant:
            self._append_dialogue(assistant)

    def rollback(self, rounds: int = 1) -> int:
        """回退最近 rounds 轮（一轮 = user + assistant）。返回实际删除的消息数。"""
        deleted = 0
        for _ in range(rounds):
            while self.messages and self.messages[-1].get("role") != "user":
                self.messages.pop()
                deleted += 1
            if self.messages and self.messages[-1].get("role") == "user":
                self.messages.pop()
                deleted += 1
        if deleted:
            self.save_messages()
            self._truncate_dialogue()
        return deleted

    # ---- media ----

    def save_media(self, filename: str, data: bytes) -> str:
        safe = re.sub(r"[^\w.\-]", "_", filename) or "file"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        (self.media_dir / safe).write_bytes(data)
        return safe

    def media_path(self, filename: str) -> Path | None:
        p = self.media_dir / re.sub(r"[^\w.\-]", "_", filename)
        return p if p.exists() else None

    # ---- dialogue.txt（人类可读回放，追加式）----

    def _append_dialogue(self, msg: dict) -> None:
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, list):  # 多模态：拼接文本 + [图片] 标记
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif isinstance(p, dict) and p.get("type") == "image_url":
                    parts.append("[图片]")
            content = " ".join(x for x in parts if x)
        text = str(content or "")
        reasoning = msg.get("reasoning_content")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.dialogue_path.open("a", encoding="utf-8") as f:
            f.write(f"\n===== [{stamp}] {role} =====\n")
            if role == "assistant" and reasoning:
                f.write(f"--- thinking ---\n{reasoning}\n--- answer ---\n")
            f.write(text.rstrip() + "\n")

    def _truncate_dialogue(self) -> None:
        """回退后整体重写 dialogue.txt 以保持一致。"""
        lines = ["# AI-Qwen-Max dialogue replay", ""]
        for m in self.messages:
            lines.append(f"===== {m.get('role', '?')} =====")
            content = m.get("content")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    elif isinstance(p, dict) and p.get("type") == "image_url":
                        parts.append("[图片]")
                content = " ".join(x for x in parts if x)
            if m.get("reasoning_content"):
                lines.append(f"[thinking] {str(m['reasoning_content'])[:200]}...")
            lines.append(str(content or "").rstrip())
            lines.append("")
        _atomic_write(self.dialogue_path, "\n".join(lines))

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or "未命名会话")


class SessionStore:
    def __init__(self, root: Path):
        self.root = root / "chat"
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta_p = d / "meta.json"
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
            except (json.JSONDecodeError, OSError):
                meta = {}
            msgs_p = d / "messages.json"
            n_msgs = 0
            try:
                if msgs_p.exists():
                    raw = json.loads(msgs_p.read_text(encoding="utf-8"))
                    n_msgs = len(raw) if isinstance(raw, list) else 0
            except (json.JSONDecodeError, OSError):
                n_msgs = 0
            out.append({
                "session_id": d.name,
                "title": str(meta.get("title") or d.name),
                "created": meta.get("created", ""),
                "ctx": meta.get("ctx"),
                "n_messages": n_msgs,
            })
        return out

    def create(self, ctx: int | None = None, title: str = "") -> Session:
        sid = new_sid()
        s = Session(self.root / sid)
        s.meta = {
            "session_id": sid,
            "title": title,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ctx": ctx,
        }
        s.save_meta()
        return s

    def get(self, sid: str) -> Session | None:
        d = self.root / sid
        if not d.is_dir():
            return None
        return Session(d)

    def get_or_create(self, sid: str) -> Session:
        """按指定 id 打开会话（API 轨 X-Conversation-Id 直接映射目录名）。"""
        d = self.root / sid
        s = Session(d)
        if not s.meta:
            s.meta = {"session_id": sid, "title": "API 会话",
                      "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            s.save_meta()
        return s

    def delete(self, sid: str) -> bool:
        d = self.root / sid
        if not d.is_dir():
            return False
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        return True
