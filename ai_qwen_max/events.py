"""/api/events 观测流：环形缓冲 + SSE 推送（Web 界面的 API 监听页依赖）。

事件记录 = 一个 /v1 请求的生命周期卡片（Web 界面以 id 幂等 upsert）：
  {id, ts, method, path, status?, dur_s?, error?, body?, reasoning?, text?}
流式期间周期性重推累计文本（reasoning/text 只保留尾部）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque

RING_CAP = 300
LOGQ_CAP = 2000


class ApiEvents:
    def __init__(self) -> None:
        self._ring: deque[dict] = deque(maxlen=RING_CAP)
        self._version = 0
        self._cond = asyncio.Condition()
        # append-only 日志队列（CLI 日志页增量消费，消费即清空）：
        # 每条 /v1 请求在 request（begin）与完成（finish）各入队一条，
        # 已入队记录永不修改；finish 记录携带 dur_s/cache/perf 等指标。
        self._logq: list[dict] = []
        self._lock = threading.Lock()

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
               text: str | None = None,
               cache_n: int | None = None, prompt_n: int | None = None,
               perf: dict | None = None) -> dict:
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
        if cache_n is not None:
            out["cache_n"] = cache_n
        if prompt_n is not None:
            out["prompt_n"] = prompt_n
        if perf:
            out["perf"] = perf
        return out

    def emit(self, record: dict) -> None:
        """推送一条记录（线程安全，需 running loop）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon_threadsafe(self._upsert, record)

    def emit_sync(self, record: dict) -> None:
        """无 running loop 环境（CLI 主线程）直接写入。"""
        self._upsert(record)

    def _upsert(self, record: dict) -> None:
        """按 id 更新 ring 中已有记录（同一请求的生命周期推进），不存在则插入。

        流式请求的节流推送/最终 finish 都更新同一条，避免日志页刷屏；
        _v 递增以便 Web SSE 差量订阅者收到更新（按 id 幂等 upsert）。
        同步追加 append-only 日志队列：新请求（begin）与完成（finish）
        各入队一条，中间的节流推送不入队。
        """
        self._version += 1
        record = dict(record)
        record["_v"] = self._version
        rid = record.get("id")
        for i, r in enumerate(self._ring):
            if r.get("id") == rid:
                self._ring[i] = record
                break
        else:
            self._ring.append(record)
            with self._lock:
                self._logq.append(record)          # 新请求：request 日志
        if record.get("status") is not None or record.get("error"):
            with self._lock:
                self._logq.append(record)          # 完成：合并 stream 的 finish 日志
                if len(self._logq) > LOGQ_CAP:
                    del self._logq[:len(self._logq) - LOGQ_CAP]
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return      # 无 running loop（CLI 线程 emit_sync）：只入 ring/队列
        asyncio.ensure_future(self._notify())

    def drain_log(self) -> list[dict]:
        """取走日志队列的全部新记录并清空（CLI 日志页增量消费，幂等）。"""
        with self._lock:
            out = self._logq
            self._logq = []
            return out

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
