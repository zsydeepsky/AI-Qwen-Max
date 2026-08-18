"""/api/events 观测流：环形缓冲 + SSE 推送（Web 界面的 API 监听页依赖）。

事件记录 = 一个 /v1 请求的生命周期卡片（Web 界面以 id 幂等 upsert）：
  {id, ts, method, path, status?, dur_s?, error?, body?, reasoning?, text?}
流式期间周期性重推累计文本（reasoning/text 只保留尾部）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque

RING_CAP = 300


class ApiEvents:
    def __init__(self) -> None:
        self._ring: deque[dict] = deque(maxlen=RING_CAP)
        self._version = 0
        self._cond = asyncio.Condition()

    # ---- 记录侧（可在任意线程调用）----

    def begin(self, req_id: str, method: str, path: str, body: str = "") -> dict:
        return {"id": req_id, "ts": time.strftime("%H:%M:%S"),
                "method": method, "path": path,
                "summary": self.summarize(method, path, body),
                "body": body[:4000]}

    @staticmethod
    def summarize(method: str, path: str, body: str) -> str:
        """请求摘要：pending 状态也能看出"这请求了个啥"。

        chat/completions → "第N轮 · <最后一条user消息截断>"；
        其它 JSON POST → 顶层键；GET → 空串。
        """
        if method != "POST" or not body.strip().startswith("{"):
            return ""
        try:
            obj = json.loads(body)
        except ValueError:
            return ""
        if not isinstance(obj, dict):
            return ""
        msgs = obj.get("messages")
        if isinstance(msgs, list) and msgs and path.endswith("chat/completions"):
            n_user = sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")
            last = ""
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user":
                    c = m.get("content")
                    if isinstance(c, list):   # 多模态：拼 text 段
                        c = " ".join(p.get("text", "") for p in c
                                     if isinstance(p, dict) and p.get("type") == "text")
                    last = " ".join(str(c).split())
                    break
            return f"第{n_user}轮 · {last[:60]}"
        return ",".join(k for k in obj.keys() if k not in ("messages",))[:60]

    def finish(self, rec: dict, status: int | None = None, dur_s: float | None = None,
               error: str | None = None, reasoning: str | None = None,
               text: str | None = None) -> dict:
        out = dict(rec)
        if status is not None:
            out["status"] = status
        if dur_s is not None:
            out["dur_s"] = round(dur_s, 2)
        if error:
            out["error"] = error[:500]
        if reasoning:
            out["reasoning"] = reasoning[-4000:]
        if text:
            out["text"] = text[-4000:]
        return out

    def emit(self, record: dict) -> None:
        """推送一条记录（线程安全）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon_threadsafe(self._emit_sync, record)

    def _emit_sync(self, record: dict) -> None:
        self._version += 1
        record = dict(record)
        record["_v"] = self._version
        self._ring.append(record)
        asyncio.ensure_future(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    # ---- 订阅侧（async）----

    async def subscribe(self):
        """SSE 生成器：先回放环形缓冲，再按 _v 差量推送。"""
        last_v = 0
        for rec in list(self._ring):   # 回放历史
            yield self._sse(rec)
            last_v = max(last_v, rec.get("_v", 0))
        while True:
            async with self._cond:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
            for rec in list(self._ring):
                if rec.get("_v", 0) > last_v:
                    yield self._sse(rec)
                    last_v = rec["_v"]

    @staticmethod
    def _sse(rec: dict) -> str:
        payload = {k: v for k, v in rec.items() if not k.startswith("_")}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
