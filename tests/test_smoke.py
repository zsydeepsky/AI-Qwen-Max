"""无引擎冒烟测试：FastAPI 端点契约 / 会话存储 / GGUF 探测。

运行：python -m tests.test_smoke   （无需 llama-server）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient   # type: ignore[import]

from ai_qwen_max.backend import Backend
from ai_qwen_max.config import CTX_CHOICES, Config
from ai_qwen_max.gguf import nextn_layer_count
from ai_qwen_max.server import AppCtx, create_app, web_dir
from ai_qwen_max.store import SessionStore

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {extra}")


def main() -> int:
    root = Path(__file__).parent.parent / ".test-max"
    root.mkdir(exist_ok=True)

    # ---- config ----
    print("[config]")
    cfg = Config(root / "config.json")
    check("default ctx", cfg["default_ctx"] == 32768)
    check("ctx tiers", CTX_CHOICES == [4096, 8192, 16384, 32768, 65536, 131072, 262144])
    cfg.data["power"] = "High"          # 旧键迁移
    cfg.load()
    check("legacy power key dropped", "power" not in cfg.data)

    # ---- store ----
    print("[store]")
    store = SessionStore(root)
    s = store.create(ctx=4096)
    sid = s.meta["session_id"]
    s.append({"role": "user", "content": "你好", "tool_calls": None})
    s.append({"role": "assistant", "content": "你好！", "reasoning_content": "思考…"})
    check("list", store.list()[0]["session_id"] == sid and store.list()[0]["n_messages"] == 2)
    got = store.get(sid)
    assert got is not None
    check("reasoning persisted", got.messages[1].get("reasoning_content") == "思考…")
    # merge_request：尾部匹配 → 增量
    got.merge_request(
        [{"role": "user", "content": "你好"},
         {"role": "assistant", "content": "你好！", "reasoning_content": "思考…"},
         {"role": "user", "content": "第二问"}],
        {"role": "assistant", "content": "第二答"})
    check("merge append", len(got.messages) == 4 and got.messages[-1]["content"] == "第二答")
    # tool_calls 完整保留
    got.merge_request(
        [{"role": "user", "content": "调工具"},
         {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "f"}}]},
         {"role": "tool", "content": "结果", "tool_call_id": "c1"}],
        {"role": "assistant", "content": "done"})
    tc = got.messages[1].get("tool_calls")
    check("tool_calls preserved", bool(tc) and tc[0]["function"]["name"] == "f")   # type: ignore[index]
    check("tool msg preserved", got.messages[2].get("role") == "tool")
    # rollback：一轮 = 最后的 user 消息及其后全部回复（工具调用轮一并回退）
    n = got.rollback(1)
    check("rollback", n == 4 and got.messages == [])
    # media
    saved = got.save_media("a b.png", b"\x89PNG")
    check("media sanitized+saved", (root / "chat" / sid / "media" / "a_b.png").exists() or saved == "a_b.png")
    check("delete", store.delete(sid) and store.get(sid) is None)

    # ---- gguf（真模型若存在） ----
    print("[gguf]")
    model = Path(os.environ.get("MAX_TEST_MODEL", ""))
    if model.is_file():
        n = nextn_layer_count(model)
        check(f"nextn探测 {model.name}", n >= 0)
        print(f"       nextn_predict_layers = {n}")
    else:
        print("  skip（未设置 MAX_TEST_MODEL 或文件不存在）")

    import tempfile
    from ai_qwen_max.gguf import find_mmproj, model_media
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "model.gguf").write_bytes(b"GGUF")
        check("find_mmproj 无配套= None",
              find_mmproj(td / "model.gguf") is None)
        check("model_media 无 mmproj= 仅文本",
              model_media(td / "model.gguf") == "文本")
        (td / "mmproj-F16.gguf").write_bytes(b"GGUF")
        hit = find_mmproj(td / "model.gguf")
        check("find_mmproj 探测到 mmproj",
              hit == str(td / "mmproj-F16.gguf"))
        check("model_media 空 mmproj= 文本/图片（老 clip 按视觉处理）",
              model_media(td / "model.gguf") == "文本/图片")
        check("find_mmproj 目录不存在= None",
              find_mmproj(td / "nope" / "m.gguf") is None)

    # ---- FastAPI（无后端） ----
    print("[http]")
    backend = Backend.__new__(Backend)   # 不启动进程
    backend.cfg = cfg
    backend.proc = None
    backend.model = None
    backend.ctx = None
    import httpx
    backend._client = httpx.Client(base_url="http://127.0.0.1:1", timeout=1)
    actx = AppCtx(cfg, backend, store, root)
    client = TestClient(create_app(actx))

    r = client.get("/health")
    check("/health loading", r.status_code == 200 and r.json()["status"] == "loading")
    r = client.get("/api/capabilities")
    check("/api/capabilities 未加载=空能力",
          r.status_code == 200 and r.json()["model"] is None
          and r.json()["capabilities"] == [])
    r = client.get("/help")
    check("/help", r.status_code == 200 and len(r.json()["endpoints"]) >= 10)
    r = client.get("/")
    if web_dir() is not None:
        check("/ (index.html)", r.status_code == 200 and "text/html" in r.headers["content-type"]
              and b"AI-Qwen-Max" in r.content)
        r = client.get("/no-such-file.css")
        check("/ 静态 404", r.status_code == 404)
    else:
        check("/", r.status_code == 200 and r.json()["service"] == "AI-Qwen-Max")

    r = client.post("/chat/new")
    sid2 = r.json()["session_id"]
    check("/chat/new", r.status_code == 200 and sid2)
    r = client.get("/chat/all")
    check("/chat/all", any(x["session_id"] == sid2 for x in r.json()))
    r = client.get(f"/chat/get/{sid2}")
    check("/chat/get", r.status_code == 200 and r.json()["messages"] == [])
    r = client.post(f"/chat/{sid2}/media?filename=x.png", content=b"\x89PNG")
    check("/chat media", r.status_code == 200 and r.json()["saved"])
    r = client.delete(f"/chat/delete/{sid2}")
    check("/chat/delete", r.json()["deleted"] is True)
    r = client.get("/queue")
    check("/queue", r.json()["capacity"] == 2)
    r = client.get("/status")
    check("/status", r.json()["backend_ready"] is False)
    r = client.get("/v1/models")
    check("/v1 proxy 502 (no backend)", r.status_code == 502)

    # /api/events：事件格式单元断言（HTTP 层无限 SSE 流与 TestClient 不兼容，端到端由冒烟覆盖）
    rec = actx.events.begin("t1", "POST", "/v1/chat/completions", '{"stream": true}')
    rec = actx.events.finish(rec, status=200, dur_s=1.23, reasoning="想", text="答")
    import json as _json
    payload = _json.loads(actx.events._sse(rec).removeprefix("data: ").strip())
    check("event format", payload["id"] == "t1" and payload["status"] == 200
          and payload["method"] == "POST" and "_v" not in payload)

    # ---- 事件追加式日志队列（CLI 日志页数据源） ----
    print("[event-logq]")
    from ai_qwen_max.events import ApiEvents
    ev = ApiEvents()
    rec = ev.begin("r1", "POST", "/v1/chat/completions", '{"stream": true}')
    ev.emit_sync(rec)                                   # request → 入队 1 条
    rec = ev.finish(rec, status=200, dur_s=2.5, cache_n=800, prompt_n=1000,
                    perf={"ttft_s": 0.3, "pp_tps": 45.0, "tg_tps": 12.0})
    ev.emit_sync(rec)                                   # finish → 再入队 1 条
    q = ev.drain_log()
    check("logq 2 records", len(q) == 2)
    check("logq request first", q[0].get("status") is None and q[0]["id"] == "r1")
    check("logq finish second", q[1]["status"] == 200 and q[1]["perf"]["tg_tps"] == 12.0)
    ev.emit_sync(ev.finish(rec, status=500, error="boom"))
    check("logq drain clears", ev.drain_log()[0]["error"] == "boom"
          and ev.drain_log() == [])

    # ---- LLM 思考预算（按输出窗口缩放，prompt 越长预算越小） ----
    print("[think-budget]")
    from ai_qwen_max.llm import LLM, TEMPLATE_OVERHEAD

    class _FakeBk:
        """tokenize 返回固定 count；count=None 时模拟后端不可用。"""
        def __init__(self, count):
            self.count = count
        def post(self, path: str, **kw):
            if path == "/tokenize":
                if self.count is None:
                    raise RuntimeError("no backend")
                count = self.count          # 闭包捕获，避免 _R.json 里 self 指向 _R 自身
                class _R:
                    def raise_for_status(self): ...
                    def json(self): return {"count": count}
                return _R()
            raise AssertionError(f"unexpected path {path}")

    msgs = [{"role": "user", "content": "hello"}]
    ctx = 32768
    b = LLM(_FakeBk(10), effort="xHigh", ctx=ctx)._think_budget(msgs, -1)
    check("xHigh 30% of avail", b == int(0.30 * (ctx - 10 - TEMPLATE_OVERHEAD)))
    b_off = LLM(_FakeBk(10), effort="off", ctx=ctx)._think_budget(msgs, -1)
    check("off → 0", b_off == 0)
    b_low = LLM(_FakeBk(10), effort="low", ctx=ctx)._think_budget(msgs, -1)
    check("low 3% of avail", b_low == int(0.03 * (ctx - 10 - TEMPLATE_OVERHEAD)))
    long_msgs = [{"role": "user", "content": "x" * 200000}]
    b_long = LLM(_FakeBk(ctx - 512), effort="xHigh", ctx=ctx)._think_budget(long_msgs, -1)
    check("prompt≈ctx → tiny budget", 0 <= b_long < 1000)
    b_cap = LLM(_FakeBk(10), effort="medium", ctx=ctx)._think_budget(msgs, 8192)
    check("max_tokens caps ceiling", b_cap == int(0.10 * (8192 - 10 - TEMPLATE_OVERHEAD)))
    b_fb = LLM(_FakeBk(None), effort="xHigh", ctx=ctx)._think_budget(msgs, -1)
    check("tokenize fail → fallback ctx×pct", b_fb == int(0.30 * ctx))
    b_zero = LLM(_FakeBk(10), effort="low", ctx=0)._think_budget(msgs, -1)
    check("ctx=0 → 0", b_zero == 0)

    # ---- 缓存命中数据提取（_cache_numbers / _delta_from_sse_line） ----
    print("[cache-nums]")
    from ai_qwen_max.server import _cache_numbers, _delta_from_sse_line

    cn, pn = _cache_numbers({"usage": {"prompt_tokens": 100,
                                       "prompt_tokens_details": {"cached_tokens": 60}}})
    check("cache from usage.details", (cn, pn) == (60, 100))
    cn, pn = _cache_numbers({"usage": {"prompt_tokens": 100, "cache_tokens": 40}})
    check("cache fallback usage.cache_tokens", (cn, pn) == (40, 100))
    cn, pn = _cache_numbers({"timings": {"prompt_n": 30, "cache_n": 70}})
    check("cache fallback timings", (cn, pn) == (70, 100))
    cn, pn = _cache_numbers({"usage": {"prompt_tokens": 50,
                                       "prompt_tokens_details": {"cached_tokens": 999}}})
    check("cache clamped to total", (cn, pn) == (50, 50))
    cn, pn = _cache_numbers({})
    check("empty → (0,0)", (cn, pn) == (0, 0))
    kind, delta, f = _delta_from_sse_line('data: {"choices":[{"delta":{"content":"hi"}}]}')
    check("sse delta line", kind == "content" and delta == "hi" and f == {})
    kind, delta, f = _delta_from_sse_line(
        'data: {"usage":{"prompt_tokens":10},"timings":{"cache_n":5}}')
    check("sse footer line", kind == "" and delta == "" and f.get("timings") == {"cache_n": 5})
    # include_usage=false 时 timings 挂在 finish_reason chunk（有 choices）上
    kind, delta, f = _delta_from_sse_line(
        'data: {"choices":[{"finish_reason":"stop","index":0,"delta":{}}],'
        '"timings":{"prompt_n":30,"cache_n":70}}')
    check("sse finish_reason+timings", kind == "" and delta == ""
          and f.get("timings", {}).get("cache_n") == 70)
    # 纯 delta 行带 usage（罕见同框）：delta 优先，footer 丢弃（同 llm.py 语义取舍）
    kind, delta, f = _delta_from_sse_line(
        'data: {"choices":[{"delta":{"content":"hi"}}],"usage":{"prompt_tokens":5}}')
    check("sse delta+usage → delta wins", kind == "content" and delta == "hi")

    # ---- 反代 include_usage 注入逻辑（与 proxy_v1 同构） ----
    import json as _j
    def _inject(body: str, path: str) -> bytes | None:
        obj = _j.loads(body)
        so = obj.get("stream_options")
        if (path == "chat/completions" and obj.get("stream")
                and not (isinstance(so, dict) and "include_usage" in so)):
            obj.setdefault("stream_options", {})["include_usage"] = True
            return _j.dumps(obj).encode()
        return None
    out = _inject('{"stream":true,"messages":[]}', "chat/completions")
    check("inject include_usage", out is not None and b'"include_usage": true' in out)
    out = _inject('{"stream":true,"messages":[],"stream_options":{"include_usage":false}}',
                  "chat/completions")
    check("respect explicit include_usage", out is None)
    out = _inject('{"stream":false,"messages":[]}', "chat/completions")
    check("non-stream untouched", out is None)

    # ---- emit_sync：无 running loop 环境（CLI 主线程）直接入 ring ----
    from ai_qwen_max.events import ApiEvents
    ev = ApiEvents()
    ev.emit_sync(ev.finish(ev.begin("x1", "POST", "/v1/chat/completions",
                                    '{"messages":[{"role":"user","content":"hi"}]}'),
                           status=200, cache_n=30, prompt_n=100))
    got = list(ev._ring)
    check("emit_sync written", len(got) == 1 and got[0]["status"] == 200
          and got[0]["cache_n"] == 30 and got[0]["prompt_n"] == 100)

    # ---- DSH 配置同步（不触碰真实 ~/.dsh，全部在 .test-max 内） ----
    print("[dsh]")
    import yaml as _yaml
    from ai_qwen_max import dsh as _dsh
    _orig_file, _orig_mm = _dsh.SETTINGS_FILE, _dsh.find_mmproj
    _dsh.SETTINGS_FILE = root / "settings.yaml"

    def _doc(**providers):
        _dsh.SETTINGS_FILE.write_text(
            _yaml.safe_dump({"llm-pi-ai": {"providers": providers}},
                            allow_unicode=True, sort_keys=False),
            encoding="utf-8")

    def _local(**kw):
        base = {"displayName": "AI-Qwen-Max", "api": "openai-completions",
                "baseURL": "http://localhost:8317/v1", "models": [
                    {"id": r"C:\models\Qwen.gguf", "name": "Qwen"}]}
        base.update(kw)
        return base

    # 1) 有 mmproj → input=[text, image]
    _dsh.find_mmproj = lambda p: r"C:\models\mmproj.gguf" if p else None
    _doc(local=_local())
    note = _dsh.sync_dsh_input(r"C:\models\Qwen.gguf", 8317)
    got = _yaml.safe_load(_dsh.SETTINGS_FILE.read_text(encoding="utf-8"))
    m = got["llm-pi-ai"]["providers"]["local"]["models"][0]
    check("dsh input=[text,image]", m["input"] == ["text", "image"] and note is not None)

    # 2) 无 mmproj → input=[text]
    _dsh.find_mmproj = lambda p: None
    note = _dsh.sync_dsh_input(r"C:\models\Qwen.gguf", 8317)
    got = _yaml.safe_load(_dsh.SETTINGS_FILE.read_text(encoding="utf-8"))
    m = got["llm-pi-ai"]["providers"]["local"]["models"][0]
    check("dsh input=[text]", m["input"] == ["text"] and note is not None)

    # 3) 模型条目不存在 → 不新增、返回 None
    _doc(local=_local())
    note = _dsh.sync_dsh_input(r"C:\models\Other.gguf", 8317)
    got = _yaml.safe_load(_dsh.SETTINGS_FILE.read_text(encoding="utf-8"))
    check("dsh unknown model untouched",
          note is None and len(got["llm-pi-ai"]["providers"]["local"]["models"]) == 1)

    # 4) 端口不匹配的 provider 不动
    _doc(other={"displayName": "Other", "baseURL": "http://localhost:9999/v1",
                "models": [{"id": r"C:\models\Qwen.gguf"}]})
    note = _dsh.sync_dsh_input(r"C:\models\Qwen.gguf", 8317)
    got = _yaml.safe_load(_dsh.SETTINGS_FILE.read_text(encoding="utf-8"))
    m = got["llm-pi-ai"]["providers"]["other"]["models"][0]
    check("dsh foreign port untouched", note is None and "input" not in m)

    # 5) 已一致 → 不再写文件
    _doc(local=_local(models=[{"id": r"C:\models\Qwen.gguf",
                               "name": "Qwen", "input": ["text", "image"]}]))
    _dsh.find_mmproj = lambda p: r"C:\models\mmproj.gguf"
    note = _dsh.sync_dsh_input(r"C:\models\Qwen.gguf", 8317)
    check("dsh idempotent", note is None)

    # 6) 配置文件缺失 → 静默 None
    _dsh.SETTINGS_FILE.unlink(missing_ok=True)
    check("dsh missing file", _dsh.sync_dsh_input(r"C:\models\Qwen.gguf", 8317) is None)

    # 7) 未安装 PyYAML → 静默跳过
    _dsh.yaml = None
    _doc(local=_local())
    check("dsh no yaml silent",
          _dsh.sync_dsh_input(r"C:\models\Qwen.gguf", 8317) is None)
    _dsh.yaml = _yaml
    _dsh.SETTINGS_FILE, _dsh.find_mmproj = _orig_file, _orig_mm

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
