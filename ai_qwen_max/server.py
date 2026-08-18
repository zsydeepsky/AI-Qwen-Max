"""前端 HTTP 服务：OpenAI 兼容反代 + Max 管理 API + /api/events 观测流。

路由清单见 GET /help。web/ 界面（index.html）依赖本模块的端点契约，改动需同步。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .backend import Backend
from .config import CTX_CHOICES, Config
from .events import ApiEvents
from .store import SessionStore

API_GATE_CAP = 2   # 并发推理闸：与 llama-server slot 预算匹配（2 路 API + 1 路内部）


def web_dir() -> Path | None:
    """定位 web/ 静态界面目录：开发态在包上级目录，打包态在 sys._MEIPASS。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent.parent
    p = base / "web"
    return p if p.is_dir() else None


class AppCtx:
    """全局上下文：config / backend / store / events / gate。"""

    def __init__(self, cfg: Config, backend: Backend, store: SessionStore, root: Path):
        self.cfg = cfg
        self.backend = backend
        self.store = store
        self.root = root
        self.events = ApiEvents()
        self.gate_sem = asyncio.Semaphore(API_GATE_CAP)
        self.gate_active = 0
        self.gate_waiting = 0
        self._aclient: httpx.AsyncClient | None = None
        self._aclient_base: str | None = None
        self.last_perf: dict[str, Any] = {}
        self.uvicorn_server = None   # 由 __main__ 注入，用于优雅停机

    @property
    def aclient(self) -> httpx.AsyncClient:
        """llama-server 异步客户端。backend 用 OS 分配端口（每次启动可能不同），
        所以按 backend.base_url 惰性建/重建，未启动时指向 :1（连接失败→上层按未就绪处理）。"""
        base = self.backend.base_url
        if self._aclient is None or self._aclient_base != base:
            self._aclient = httpx.AsyncClient(
                base_url=base, timeout=httpx.Timeout(600, connect=5))
            self._aclient_base = base
        return self._aclient


def create_app(actx: AppCtx) -> FastAPI:
    app = FastAPI(title="AI-Qwen-Max", docs_url=None, redoc_url=None)
    # Web 界面可能以 file:// 打开（Origin: null），放开 CORS
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"], expose_headers=["*"])

    def jerr(status: int, msg: str) -> JSONResponse:
        return JSONResponse({"error": {"message": msg, "type": "max_error"}}, status_code=status)

    # ================ /api/stats：精确引擎查询（空闲/PP/TG + KV 缓存池） ================

    @app.get("/api/stats")
    async def api_stats(refresh: bool = True):
        """
        精确引擎查询端点。返回结构：
            engine_state:
                phase ∈ {"idle", "prefill", "decode"}
                slot:     正在处理的 slot id（idle 时为 null）
                prefill:  {total, processed, from_cache, pct, tps}   # PP 阶段
                decode:   {decoded, remain, tps}                      # TG 阶段
            cache:
                total_tokens: 所有 slot n_ctx 合计（占满时最大可达 tokens）
                ram_pool, ssd_pool: {entries, bytes, limit_bytes}
                recent_hit_pct: 最近（≤10 次请求）n_cache/n_prompt 均值
        """
        ready = actx.backend.healthy()
        out: dict[str, Any] = {"engine_state": {"phase": "idle", "slot": None,
                                                 "prefill": None, "decode": None},
                                "cache": None, "backend_ready": ready}
        if not ready:
            return out
        # --- slots：每 slot id_task 可能为空，取 is_processing 为真者做当前相位估计 ---
        slots: list[dict] = []
        cache_totals = {"cache": 0, "prompt": 0, "processed": 0, "hits": 0, "requests": 0}
        try:
            r = await actx.aclient.get("/slots", timeout=5 if refresh else 2)
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, list):
                    slots = j
        except (httpx.HTTPError, ValueError):
            pass
        n_ctx_sum = 0
        decoding = False
        active_slot: dict | None = None
        for s in slots:
            n_ctx_sum += int(s.get("n_ctx") or 0)
            pp_tok = int(s.get("n_prompt_tokens") or 0)
            pp_proc = int(s.get("n_prompt_tokens_processed") or 0)
            pp_cache = int(s.get("n_prompt_tokens_cache") or 0)
            if pp_tok > 0:
                cache_totals["requests"] += 1
                cache_totals["prompt"] += pp_tok
                cache_totals["processed"] += min(pp_proc, pp_tok)
                cache_totals["cache"] += pp_cache
                # 命中率以“命中 token / 总 prompt”计；cache<=prompt 时比值合理
                # （slot 空闲时 cache 仍保留上一请求值，以 processed>0 视为近期请求）
                if pp_proc > 0:
                    cache_totals["hits"] += pp_cache
            if s.get("is_processing"):
                # PP = 还有 prompt 没处理完；TG = 全部 prompt 处理完
                if pp_proc < pp_tok and pp_tok > 0:
                    active_slot = s
                    out["engine_state"]["phase"] = "prefill"
                else:
                    active_slot = s
                    out["engine_state"]["phase"] = "decode"
                    decoding = True
        # 解码优先：任一 slot 在 decode 状态（即已经过 PP 进入 TG）就以 TG 展示
        if decoding and out["engine_state"]["phase"] != "decode":
            active_slot = next((s for s in slots
                                if s.get("is_processing") and int(s.get("n_prompt_tokens_processed") or 0) >= int(s.get("n_prompt_tokens") or 0) > 0),
                               None) or active_slot
            out["engine_state"]["phase"] = "decode"
        if active_slot is not None:
            out["engine_state"]["slot"] = int(active_slot.get("id") or 0)
            pp_tok = int(active_slot.get("n_prompt_tokens") or 0)
            pp_proc = int(active_slot.get("n_prompt_tokens_processed") or 0)
            pp_cache = int(active_slot.get("n_prompt_tokens_cache") or 0)
            pp_pct = round(100.0 * pp_proc / pp_tok, 1) if pp_tok else 0.0
            out["engine_state"]["prefill"] = {
                "total": pp_tok, "processed": pp_proc, "from_cache": pp_cache,
                "pct": pp_pct, "tps": None}
            # tps 估算：用当前请求累计值（无跨帧时间戳时由 CLI 端做差分）
            nt = active_slot.get("next_token")
            nt = nt if isinstance(nt, dict) else {}
            n_decoded = int(nt.get("n_decoded") or 0)
            remain = int(nt.get("n_remain") or 0)
            out["engine_state"]["decode"] = {
                "decoded": n_decoded, "remain": remain, "tps": None}
        # --- cache 池：RAM / SSD 用量 + 命中 token ---
        # vendor /cache/stats 为扁平字段（ram_entries/ram_bytes/ram_tokens/ssd_*），
        # 兼容嵌套 {"ram": {...}} 变种
        pool: dict[str, Any] = {
            "total_tokens": n_ctx_sum,
            "n_slots": len(slots),
            "hit_tokens": cache_totals["cache"],
            "ram_pool": {"entries": 0, "bytes": 0, "tokens": 0, "limit_bytes": 0},
            "ssd_pool": {"entries": 0, "bytes": 0, "tokens": 0, "limit_bytes": 0},
            "recent_hit_pct": 0.0,
        }
        try:
            r = await actx.aclient.get("/cache/stats", timeout=3 if refresh else 2)
            if r.status_code == 200:
                cs = r.json() or {}
                if isinstance(cs, dict):
                    for key, tag in (("ram", "ram_pool"), ("ssd", "ssd_pool")):
                        sub = cs.get(key) if isinstance(cs.get(key), dict) else {}
                        pool[tag]["entries"] = int(sub.get("entries")
                                                   or cs.get(f"{key}_entries") or cs.get(f"n_{key}") or 0)
                        pool[tag]["bytes"] = int(sub.get("bytes")
                                                 or cs.get(f"{key}_bytes") or cs.get(f"n_{key}_bytes") or 0)
                        pool[tag]["tokens"] = int(sub.get("tokens")
                                                  or cs.get(f"{key}_tokens") or 0)
                        pool[tag]["limit_bytes"] = int(sub.get("limit_bytes") or 0)
        except (httpx.HTTPError, ValueError):
            pass
        if cache_totals["prompt"] > 0:
            pool["recent_hit_pct"] = round(100.0 * cache_totals["cache"] / cache_totals["prompt"], 1)
        out["cache"] = pool
        # --- 最近一次流式请求的精确 perf（若存在） ---
        if actx.last_perf:
            out["perf"] = dict(actx.last_perf)
        return out

    # ================= /v1 反代 =================

    @app.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def proxy_v1(path: str, request: Request):
        url = f"/v1/{path}"
        body = b""
        if request.method == "POST":
            body = await request.body()
        is_post = request.method == "POST"
        wants_stream = False
        if is_post and body:
            try:
                wants_stream = bool(json.loads(body).get("stream"))
            except (ValueError, AttributeError):
                wants_stream = False
        conv_id = request.headers.get("X-Conversation-Id", "")

        req_id = uuid.uuid4().hex[:12]
        rec = actx.events.begin(
            req_id, request.method, url,
            body.decode("utf-8", "replace") if is_post else "")
        actx.events.emit(rec)

        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ("content-type", "authorization", "accept")}
        t0 = time.monotonic()
        gate_acquired = False
        if is_post:
            actx.gate_waiting += 1
            await actx.gate_sem.acquire()
            actx.gate_waiting -= 1
            actx.gate_active += 1
            gate_acquired = True
        try:
            if is_post:
                upstream = actx.aclient.build_request(request.method, url, content=body, headers=headers)
            else:
                upstream = actx.aclient.build_request(request.method, url, headers=headers)
            if wants_stream:
                return await _stream_proxy(actx, upstream, rec, conv_id, t0)
            r = await actx.aclient.send(upstream)
            actx.events.emit(actx.events.finish(rec, status=r.status_code,
                                                dur_s=time.monotonic() - t0))
            if conv_id and path == "chat/completions" and r.status_code == 200:
                _persist_completion(actx, conv_id, body, r.content)
            return Response(content=r.content, status_code=r.status_code,
                            media_type=r.headers.get("content-type"))
        except httpx.HTTPError as e:
            actx.events.emit(actx.events.finish(rec, error=str(e), dur_s=time.monotonic() - t0))
            return jerr(502, f"推理后端不可达：{e}")
        finally:
            if gate_acquired:
                actx.gate_active -= 1
                actx.gate_sem.release()

    async def _stream_proxy(actx: AppCtx, upstream: httpx.Request, rec: dict,
                            conv_id: str, t0: float) -> StreamingResponse:
        """流式透传：原样转发字节，旁路解析 delta 供观测与落盘。"""
        resp = await actx.aclient.send(upstream, stream=True)

        async def gen():
            reasoning = ""
            text = ""
            last_push = 0.0
            buffer = b""

            def _drain(buf: bytes) -> bytes:
                """处理 buf 中所有完整行（以 \\n 结尾），返回不完整尾行。"""
                nonlocal reasoning, text, last_push
                if b"\n" not in buf:
                    return buf
                usable, _, remainder = buf.rpartition(b"\n")
                for line in usable.decode("utf-8", "replace").split("\n"):
                    kind, delta = _delta_from_sse_line(line)
                    if kind == "reasoning":
                        reasoning += delta
                    elif kind == "content":
                        text += delta
                now = time.monotonic()
                if now - last_push > 0.15:   # 150ms 节流推送累计文本
                    actx.events.emit(actx.events.finish(rec, reasoning=reasoning, text=text))
                    last_push = now
                return remainder

            try:
                async for chunk in resp.aiter_raw():
                    buffer += chunk
                    buffer = _drain(buffer)
                    yield chunk
            except httpx.HTTPError:
                pass
            finally:
                # drain 残留：流结束时 buffer 可能还有一个不完整行（无尾部 \\n），
                # 尝试作为完整行解析，避免最后一个 delta 丢失。
                if buffer:
                    _drain(buffer + b"\n")
                await resp.aclose()
                actx.events.emit(actx.events.finish(
                    rec, status=resp.status_code, dur_s=time.monotonic() - t0,
                    reasoning=reasoning, text=text))
                actx.last_perf["dur_s"] = time.monotonic() - t0
                if conv_id:
                    assistant: dict[str, Any] = {"role": "assistant", "content": text}
                    if reasoning:
                        assistant["reasoning_content"] = reasoning
                    try:
                        req_body = json.loads(rec.get("body", "") or "{}")
                        _persist_messages(actx, conv_id,
                                          req_body.get("messages") or [], assistant)
                    except ValueError:
                        pass

        return StreamingResponse(
            gen(), status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/event-stream"))

    def _persist_completion(actx: AppCtx, conv_id: str, req_body: bytes, resp_body: bytes) -> None:
        """非流式完成的落盘。"""
        try:
            req = json.loads(req_body)
            resp = json.loads(resp_body)
            msg = resp.get("choices", [{}])[0].get("message", {})
            assistant: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("reasoning_content"):
                assistant["reasoning_content"] = msg["reasoning_content"]
            _persist_messages(actx, conv_id, req.get("messages") or [], assistant)
        except (ValueError, IndexError, KeyError):
            pass

    def _persist_messages(actx: AppCtx, conv_id: str, req_messages: list, assistant: dict) -> None:
        s = actx.store.get(conv_id)
        if s is None:
            # X-Conversation-Id 指向不存在的会话：创建同 id 会话（API 轨）
            s = actx.store.get_or_create(conv_id)
        s.merge_request(req_messages, assistant)

    # ================= Max 管理 API =================

    @app.get("/health")
    async def health():
        loaded = actx.backend.healthy()
        return {"status": "ok" if loaded else "loading", "loaded": loaded}

    @app.get("/help")
    async def help_():
        return {"endpoints": [
            {"method": "GET|POST", "path": "/v1/*", "desc": "OpenAI 兼容反代（X-Conversation-Id 落盘）"},
            {"method": "GET", "path": "/health", "desc": "前端存活 + 后端就绪"},
            {"method": "GET", "path": "/status", "desc": "聚合状态（模型/上下文/性能/缓存）"},
            {"method": "POST", "path": "/shutdown", "desc": "优雅关闭（先落盘 KV 缓存）"},
            {"method": "POST", "path": "/model/load", "desc": "切换模型/上下文档位 ?model=&ctx="},
            {"method": "GET", "path": "/chat/all", "desc": "会话列表"},
            {"method": "POST", "path": "/chat/new", "desc": "新建会话 ?ctx="},
            {"method": "GET", "path": "/chat/get/{sid}", "desc": "会话完整消息"},
            {"method": "DELETE", "path": "/chat/delete/{sid}", "desc": "删除会话"},
            {"method": "POST", "path": "/chat/{sid}/media", "desc": "上传媒体 ?filename="},
            {"method": "GET", "path": "/queue", "desc": "并发闸状态"},
            {"method": "GET", "path": "/cache/stats", "desc": "缓存池统计（RAM/SSD/heal）"},
            {"method": "POST", "path": "/cache/evict", "desc": "主动驱逐 RAM→SSD ?ram_target_mib="},
            {"method": "GET", "path": "/api/events", "desc": "API 观测流（SSE）"},
            {"method": "GET", "path": "/api/stats", "desc": "精确引擎查询（空闲/PP/TG + KV 缓存池）"},
        ]}

    @app.get("/status")
    async def status():
        ready = actx.backend.healthy()
        out: dict[str, Any] = {"backend_ready": ready, "model": actx.backend.model}
        if ready:
            try:
                r = await actx.aclient.get("/slots", timeout=5)
                slots = r.json() if r.status_code == 200 else []
                if slots and isinstance(slots, list):
                    actx.last_perf["cache_hit_pct"] = slots[0].get("cache_hit_pct")
            except (httpx.HTTPError, ValueError):
                pass
            try:
                r = await actx.aclient.get("/cache/stats", timeout=5)
                if r.status_code == 200:
                    cs = r.json()
                    ram = cs.get("ram", {}) or {}
                    ssd = cs.get("ssd", {}) or {}
                    heal = cs.get("heal", {}) or {}
                    out["cache"] = {
                        "ram_entries": ram.get("entries", 0),
                        "ram_bytes": ram.get("bytes", 0),
                        "ssd_entries": ssd.get("entries", 0),
                        "ssd_bytes": ssd.get("bytes", 0),
                        "heal_requests": heal.get("requests", cs.get("n_heal_reqs", 0)),
                        "heal_tokens": heal.get("tokens", cs.get("n_heal_tokens", 0)),
                    }
            except (httpx.HTTPError, ValueError):
                pass
        if actx.last_perf:
            out["perf"] = dict(actx.last_perf)
        if actx.backend.ctx:
            out["ctx_total"] = actx.backend.ctx
        return out

    @app.post("/model/load")
    async def model_load(model: str = "", ctx: int = 0):
        models = actx.cfg.get("models") or []
        path = model
        if model.isdigit() and int(model) < len(models):
            path = models[int(model)]
        if not path or not Path(path).exists():
            return jerr(400, f"模型不存在：{model}")
        target_ctx = ctx if ctx in CTX_CHOICES else actx.cfg.get("default_ctx", 32768)
        try:
            await asyncio.to_thread(actx.backend.start, path, target_ctx)
        except (RuntimeError, TimeoutError) as e:
            return jerr(500, f"加载失败：{e}")
        return {"model": path, "ctx": target_ctx, "ready": True}

    @app.post("/shutdown")
    async def shutdown():
        """优雅关闭：先让 uvicorn 停止接受新请求，进程退出时 Backend.stop() 落盘缓存。"""
        server = actx.uvicorn_server
        if server is not None:
            asyncio.get_event_loop().call_later(0.3, setattr, server, "should_exit", True)
        return {"message": "正在保存 KV 缓存并关闭…"}

    # ---- 会话 ----

    @app.get("/chat/all")
    async def chat_all():
        return actx.store.list()

    @app.post("/chat/new")
    async def chat_new(ctx: int = 0):
        tier = ctx if ctx in CTX_CHOICES else actx.cfg.get("default_ctx", 32768)
        s = actx.store.create(ctx=tier)
        return {"session_id": s.meta["session_id"], "ctx": tier}

    @app.get("/chat/get/{sid}")
    async def chat_get(sid: str):
        s = actx.store.get(sid)
        if s is None:
            return jerr(404, f"会话不存在：{sid}")
        return {"session_id": sid, "title": s.title, "ctx": s.meta.get("ctx"),
                "messages": s.messages}

    @app.delete("/chat/delete/{sid}")
    async def chat_delete(sid: str):
        return {"deleted": actx.store.delete(sid)}

    @app.post("/chat/{sid}/media")
    async def chat_media(sid: str, request: Request, filename: str = "file"):
        s = actx.store.get(sid)
        if s is None:
            return jerr(404, f"会话不存在：{sid}")
        data = await request.body()
        return {"saved": s.save_media(filename, data)}

    # ---- 观测 ----

    @app.get("/queue")
    async def queue_():
        return {"capacity": API_GATE_CAP, "active": actx.gate_active, "waiting": actx.gate_waiting}

    @app.get("/cache/stats")
    async def cache_stats():
        try:
            r = await actx.aclient.get("/cache/stats", timeout=5)
            return JSONResponse(r.json(), status_code=r.status_code)
        except httpx.HTTPError as e:
            return jerr(502, f"后端不可达：{e}")

    @app.post("/cache/evict")
    async def cache_evict(ram_target_mib: int = 0):
        try:
            r = await actx.aclient.post("/cache/evict", params={"ram_target_mib": ram_target_mib},
                                        timeout=30)
            return JSONResponse(r.json(), status_code=r.status_code)
        except httpx.HTTPError as e:
            return jerr(502, f"后端不可达：{e}")

    @app.get("/api/events")
    async def api_events():
        return StreamingResponse(
            actx.events.subscribe(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 静态 Web 界面（最后挂载：API 路由全部注册后，未匹配路径才落到 web/）----
    # / → index.html，其余文件按路径直出；未来可任意扩展 html/js/css。
    wdir = web_dir()
    if wdir is not None:
        app.mount("/", StaticFiles(directory=str(wdir), html=True), name="web")
    else:
        @app.get("/")
        async def index():
            return {"service": "AI-Qwen-Max", "version": "1.0.0", "docs": "/help"}

    return app


def _delta_from_sse_line(line: str) -> tuple[str, str]:
    if not line.startswith("data:"):
        return ("", "")
    data = line[5:].strip()
    if data == "[DONE]":
        return ("", "")
    try:
        obj = json.loads(data)
    except ValueError:
        return ("", "")
    try:
        delta = obj["choices"][0].get("delta") or {}
    except (KeyError, IndexError, TypeError):
        return ("", "")
    if delta.get("reasoning_content"):
        return ("reasoning", delta["reasoning_content"])
    if delta.get("content"):
        return ("content", delta["content"])
    return ("", "")
