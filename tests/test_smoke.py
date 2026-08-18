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
    r = client.get("/help")
    check("/help", r.status_code == 200 and len(r.json()["endpoints"]) >= 10)
    r = client.get("/")
    if web_dir() is not None:
        check("/ (index.html)", r.status_code == 200 and "text/html" in r.headers["content-type"]
              and b"AI-Qwen-Max Chat" in r.content)
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

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
