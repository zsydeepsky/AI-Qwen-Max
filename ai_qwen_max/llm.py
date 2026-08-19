"""推理客户端：直连 llama-server 的流式对话（CLI 轨）与非流式补全（辅助请求）。

精确性能字段来源：
  流式：SSE 末尾会出现一条含 `usage` 的非 delta 事件，以及一条不含 choices
        仅含 `timings` 的“footer”事件（strix-halo-llamacpp 特性）。两个事件都
        无 `content/reasoning` delta，不影响 UI。
  非流式：返回体顶层 `timings` 字段（prompt/predicted_per_second / cache_n / draft_n_accepted）。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterator

import httpx


class GenResult:
    __slots__ = (
        "content", "reasoning",
        "prompt_tokens", "cache_tokens", "completion_tokens",
        "ttft_s",                         # first reasoning/content delta 出现时间
        "prefill_s", "decode_s",          # PP / TG 分段耗时（基于 usage.timings 时覆盖）
        "prefill_tps", "decode_tps",      # tok/s
        "dur_s",                          # 整轮总耗时（monotonic 差值）
        "draft_attempted", "draft_accepted",  # MTP 统计
        "mtp_efficiency",                 # accepted / attempted（≥1 是 MTP overrun）
    )

    def __init__(self) -> None:
        self.content = ""
        self.reasoning = ""
        self.prompt_tokens = 0
        self.cache_tokens = 0
        self.completion_tokens = 0
        self.ttft_s: float | None = None
        self.prefill_s: float | None = None
        self.decode_s: float | None = None
        self.prefill_tps: float | None = None
        self.decode_tps: float | None = None
        self.dur_s: float | None = None
        self.draft_attempted = 0
        self.draft_accepted = 0
        self.mtp_efficiency: float | None = None

    @property
    def tps(self) -> float | None:
        """兼容旧调用（总 decode 速度）。"""
        return self.decode_tps


def template_supports_effort(tpl: str | None) -> bool:
    """模板是否原生支持 reasoning_effort 档位（qwen35 等新模板有，qwen35moe 无）。"""
    return bool(tpl) and "reasoning_effort" in tpl


# 模板不支持 reasoning_effort 时的降级档位指令：以 system 消息注入，
# 文案模仿 qwen35 模板的 reasoning_instructions（英文，Qwen3 思考习惯英语）。
EFFORT_SYSTEM_HINT = {
    "low": (
        "Reasoning effort is set to low. Keep your thinking brief and focused, "
        "moving directly to the conclusion without unnecessary elaboration."
    ),
    "medium": (
        "Reasoning effort is set to medium. Think through the task step by step "
        "before answering, but avoid excessive detail and stay concise."
    ),
    "xHigh": (
        "Reasoning effort is set to xhigh. Please think carefully through the task, "
        "validate key assumptions, consider plausible alternatives, and prioritize "
        "correctness, consistency, and clarity in the final answer."
    ),
}


def effort_chat_kwargs(effort: str, tpl: str | None) -> dict[str, Any]:
    """按模板能力返回 chat_template_kwargs。

    off → 显式关闭思考（enable_thinking，模板通用）；
    模板支持 reasoning_effort → 走模板档位（xhigh/medium/low，大小写敏感必须小写）；
    模板不支持 → 退回开启思考，档位靠 system 指令（effort_system_injection）体现。
    """
    if effort in ("off", "none"):
        return {"enable_thinking": False}
    if template_supports_effort(tpl):
        return {"reasoning_effort": effort.lower()}
    return {"enable_thinking": True}


def effort_system_injection(effort: str, tpl: str | None) -> str | None:
    """模板不支持 reasoning_effort 时，返回应注入头部 system 的档位指令；否则 None。"""
    if effort in ("off", "none") or template_supports_effort(tpl):
        return None
    return EFFORT_SYSTEM_HINT.get(effort)


# 思考预算按 effort 档绑定"输出窗口"百分比：
# off = 思考关闭；其余档位在消耗到预算 80% 时注入诱导语，100% 时强制 </think>
# 输出窗口 = min(max_tokens, ctx) − prompt_token − 模板余量（见 _think_budget）
EFFORT_THINK_PCT = {"off": 0.0, "low": 0.03, "medium": 0.10, "xHigh": 0.30}

# 模板/system/think 标记等非对话内容 token 开销余量，从输出窗口中扣除，
# 避免 prompt 占满 ctx 时预算仍虚高、软注入永远没有触发空间
TEMPLATE_OVERHEAD = 256

# 诱导语：第一人称自嗓音（Qwen3 CoT 原生分布），
# 以 Qwen3 收尾公式 "Okay, let me ... write the final answer" 结尾，
# 把 P(下一个 token 是 </think>) 推到峰值。
THINK_NUDGE = (
    "\n\n...wait, I'm approaching the output limit. I must stop analyzing now.\n"
    "I've already worked out the key points above — they are sufficient.\n"
    "I can always make changes later, it's good enough for now.\n"
    "Okay, let me close my thinking here and write the final answer directly,\n"
    "keeping it clear and concise.\n\n"
)


def think_budget_kwargs(effort: str, budget: int) -> dict:
    """think_budget > 0 且思考开启时：软注入（80%）+ 硬终结（100% 强制 </think>）。"""
    if budget <= 0 or effort in ("off", "none"):
        return {}
    return {
        "reasoning_budget_tokens": budget,
        "reasoning_budget_soft_message": THINK_NUDGE,
        "reasoning_budget_soft_ratio": 0.8,
    }


class LLM:
    def __init__(self, backend, effort: str = "low", ctx: int = 0):
        self.backend = backend   # Backend 实例
        self.effort = effort
        self.ctx = ctx           # context window（tokens）；思考预算 = 输出窗口 × effort 档百分比

    # ---- 思考预算 ----

    def _prompt_tokens(self, messages: list[dict]) -> int | None:
        """请求前 tokenize 拼接文本，拿 prompt 近似 token 数（用于预算缩放）。

        llama-server 原生 POST /tokenize；失败（后端未起/超时）返回 None，
        调用方回退旧逻辑 ctx×pct。
        """
        try:
            parts = []
            for m in messages:
                c = m.get("content", "")
                parts.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
            r = self.backend.post("/tokenize",
                                  json={"content": "\n".join(parts), "add_special": False},
                                  timeout=30)
            r.raise_for_status()
            return int(r.json().get("count", 0))
        except Exception:
            return None

    def _think_budget(self, messages: list[dict], max_tokens: int) -> int:
        """思考预算 = 输出窗口 × effort 档百分比。

        输出窗口 = min(max_tokens(若>0), ctx) − prompt_token − 模板余量。
        prompt 越长预算越小：软注入（80%）永远有触发空间，思考不会烧到上下文墙。
        """
        pct = EFFORT_THINK_PCT.get(self.effort, 0.0)
        if pct <= 0 or not self.ctx:
            return 0
        ceiling = min(max_tokens, self.ctx) if max_tokens and max_tokens > 0 else self.ctx
        prompt_n = self._prompt_tokens(messages)
        if prompt_n is None:
            return int(self.ctx * pct)             # tokenize 失败：回退旧逻辑
        avail = max(0, ceiling - prompt_n - TEMPLATE_OVERHEAD)
        return int(avail * pct)

    # ---- CLI 流式对话 ----

    def chat_stream(self, messages: list[dict], *, temperature: float = 0.7,
                    max_tokens: int = -1,
                    stop: threading.Event | None = None,
                    result: GenResult | None = None) -> Iterator[tuple[str, str]]:
        """yield (kind, delta)。kind ∈ {"reasoning", "content"}。

        若传入 `result`，本方法会把 usage/timings 写进去（CLI 调用者应始终传）。
        """
        # 模板不支持 reasoning_effort 时，把档位降级为头部 system 指令注入
        tpl = getattr(self.backend, "chat_template", None)
        hint = effort_system_injection(self.effort, tpl)
        msgs = list(messages)
        if hint:
            msgs.insert(0, {"role": "system", "content": hint})
        payload = {
            "model": "default",
            "messages": msgs,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": effort_chat_kwargs(self.effort, tpl),
            **think_budget_kwargs(self.effort, self._think_budget(msgs, max_tokens)),
        }
        with httpx.stream("POST", f"{self.backend.base_url}/v1/chat/completions",
                          json=payload, timeout=httpx.Timeout(600, connect=10)) as r:
            r.raise_for_status()
            for item in _iter_sse(r):
                # 第一项：(kind, delta) 给 UI；第二项：{"usage"/"timings"} footer
                if isinstance(item, tuple):
                    yield item
                    if stop is not None and stop.is_set():
                        break
                elif isinstance(item, dict) and result is not None:
                    _apply_footer(result, item)

    def chat(self, messages: list[dict], **kw: Any) -> GenResult:
        """同步收集一轮完整生成（CLI 主用），含精确 perf。"""
        t0 = time.monotonic()
        res = GenResult()
        first = True
        pp_done = False
        for kind, delta in self.chat_stream(messages, result=res, **kw):
            now = time.monotonic()
            if first:
                res.ttft_s = now - t0
                # PP = t0 → first token；之后 = TG（decode）
                res.prefill_s = res.ttft_s
                first = False
                pp_done = True
                t_decode0 = now
            if kind == "reasoning":
                res.reasoning += delta
            else:
                res.content += delta
        total = time.monotonic() - t0
        res.dur_s = total
        # 若 footer 给了精确值不覆盖；否则用分段 wall-clock 估计 tps
        if pp_done and res.prefill_tps is None and res.prefill_s and res.prompt_tokens:
            # 估计的 prompt 处理量：请求总 token（估算）来自 messages 分词不准，
            # 以 footer usage.prompt_tokens 为准；没有 footer 就不填 prefill_tps。
            pass
        if res.decode_s is None and pp_done and res.content:
            # content 长度 约等于 completion_tokens（不含 MTP overrun）——做粗估计
            est_decode = max(0.001, total - res.prefill_s) if res.prefill_s else total
            n_words = len(res.content)  # 中文近似：字符 ≈ token
            res.decode_s = est_decode
            if res.completion_tokens:
                res.decode_tps = res.completion_tokens / est_decode
            elif n_words:
                res.decode_tps = n_words / est_decode
        if res.draft_attempted:
            res.mtp_efficiency = (res.draft_accepted / res.draft_attempted
                                  if res.draft_attempted else None)
        return res

    # ---- 非流式补全（标题生成等辅助请求，不污染缓存池）----

    def complete(self, messages: list[dict], *, max_tokens: int = 48,
                 temperature: float = 0.2, enable_thinking: bool = False) -> str:
        payload = {
            "model": "default",
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
        }
        r = self.backend.post("/v1/chat/completions", json=payload, timeout=120)
        r.raise_for_status()
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""


# ================ SSE 解析（扩展 usage/timings footer） ================

def _iter_sse(response: httpx.Response) -> Iterator[tuple[str, str] | dict]:
    """解析 SSE：先产出 (kind, delta) 给 UI；最后一条 [DONE] 前若存在 footer 事件（不含 choices
    或有 usage/timings 顶层字段），整条作为 dict 返回。
    """
    import json as _json
    footer: dict | None = None
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            if footer:
                yield footer
            return
        try:
            obj = _json.loads(data)
        except ValueError:
            continue
        choices = obj.get("choices") or []
        has_delta_out = False
        if choices:
            delta = choices[0].get("delta") or {}
            rc = delta.get("reasoning_content")
            if rc:
                yield "reasoning", rc
                has_delta_out = True
            c = delta.get("content")
            if c:
                yield "content", c
                has_delta_out = True
        # footer 收集：有 usage 或 timings 顶层字段、且未 yield delta
        footer_payload = {}
        if "usage" in obj:
            footer_payload["usage"] = obj["usage"]
        if "timings" in obj:
            footer_payload["timings"] = obj["timings"]
        # choices 的最后一条若含 finish_reason 带 usage 也写入（非流式习惯）
        if footer_payload:
            if has_delta_out:
                # 同一 SSE 行即含 delta 又带 footer：极少；先 UI 再 footer
                yield footer_payload
            else:
                footer = _merge_footer(footer, footer_payload)
    if footer:
        yield footer


def _merge_footer(a: dict | None, b: dict) -> dict:
    if a is None:
        return dict(b)
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _apply_footer(res: GenResult, footer: dict) -> None:
    """把 usage/timings footer 精确值写入 GenResult。

    写入顺序遵循 max.py：先 timings（PP/TG 吞吐/耗时优先用精确测量），
    再 usage（cache_pct/ctx_tokens 用量优先用 usage 的 prompt_tokens_details，
    因为它代表"本轮 PP 实际命中缓存的 token 数"，永远 <= prompt_tokens。
    timings.cache_n 在启用了 speculative decoding 时会把每一轮草稿验证
    的 cache 命中也累计进去，数值可远超 prompt_n → 绝对不能做 cache_pct 分子。
    """
    usage = footer.get("usage") or {}
    timings = footer.get("timings") or {}
    # 1) timings：精确 PP / TG 吞吐（draft 统计也在这里）
    #    vendor/llama.cpp 语义（README + test_completion.py:661）：
    #      prompt_n = 实际处理的 prompt token 数（不含缓存命中）
    #      cache_n  = 从 KV cache 恢复的 token 数
    #      prompt_n + cache_n == 请求总 prompt token 数
    if timings:
        pn = int(timings.get("prompt_n") or 0)
        cn_cache = int(timings.get("cache_n") or 0)
        pms = float(timings.get("prompt_ms") or 0.0) / 1000.0
        pps = float(timings.get("prompt_per_second") or 0.0)
        if pn or cn_cache:
            res.prompt_tokens = pn + cn_cache    # 请求总 token 数
        if cn_cache:
            res.cache_tokens = cn_cache          # 从 cache 恢复的 KV token 数
        if pms > 0:
            res.prefill_s = pms
        if pps > 0:
            res.prefill_tps = pps
        cn = int(timings.get("predicted_n") or 0)
        cms = float(timings.get("predicted_ms") or 0.0) / 1000.0
        cps = float(timings.get("predicted_per_second") or 0.0)
        if cn:
            res.completion_tokens = cn
        if cms > 0:
            res.decode_s = cms
        if cps > 0:
            res.decode_tps = cps
        dn = int(timings.get("draft_n") or 0)
        da = int(timings.get("draft_n_accepted") or 0)
        if dn:
            res.draft_attempted = dn
        if da:
            res.draft_accepted = da
    # 2) usage：token 计数补充/校正（取较大值防流式 footer 少报）
    if usage:
        pt = int(usage.get("prompt_tokens") or 0)
        if pt:
            res.prompt_tokens = max(pt, res.prompt_tokens)
        cc = int(usage.get("completion_tokens") or 0)
        if cc:
            res.completion_tokens = cc
        # 规范字段：usage.prompt_tokens_details.cached_tokens
        details_cached = int(
            ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
             if isinstance(usage.get("prompt_tokens_details"), dict) else 0) or 0
        )
        cached = details_cached or int(usage.get("cache_tokens") or 0)
        if cached:
            res.cache_tokens = min(cached, res.prompt_tokens)
    # 数据层兜底：cache 不可能超过请求总 token 数
    if res.cache_tokens > res.prompt_tokens:
        res.cache_tokens = res.prompt_tokens
    if res.draft_attempted:
        res.mtp_efficiency = (res.draft_accepted / res.draft_attempted
                              if res.draft_attempted else None)


def _iter_sse_deltas(response: httpx.Response) -> Iterator[tuple[str, str]]:
    """旧接口兼容：只返回 delta 对，过滤 footer。"""
    for item in _iter_sse(response):
        if isinstance(item, tuple):
            yield item
