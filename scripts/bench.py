#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-Qwen-Max 基准测试
测量：TTFT（首 token 延迟）、decode 吞吐（token/s）、前缀缓存命中、
多 session 缓存隔离、能耗（tok/s per Watt，离电有效）。
仅依赖标准库，向 OpenAI 兼容 /v1/chat/completions 发送流式请求。

用法示例：
  python scripts\bench.py --url http://127.0.0.1:8080 --prompt "你好"
  python scripts\bench.py --url http://127.0.0.1:8080 --prompt-file prompt.txt --repeat 3 --max-tokens 256
  python scripts\bench.py --url http://127.0.0.1:8080 --sessions 3 --rounds 3      # 多 session 缓存隔离
  python scripts\bench.py --url http://127.0.0.1:8080 --prompt "你好" --energy     # 能效（离电）
"""
import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request

DEFAULT_MODEL = "qwen3.8-27b"


def stream_chat(url, model, prompt, max_tokens, temperature=0.0, messages=None):
    """流式请求，逐块解析 SSE。

    messages：完整对话历史（append-only，真实 chat 客户端行为）；缺省时用单条 user prompt。
    返回 (ttft, first_token_time, last_token_time, n_tokens, usage, content_text)。
    """
    body = {
        "model": model,
        "messages": messages if messages is not None else [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        url + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    first_token_time = None
    last_token_time = None
    n_tokens = 0
    usage = None
    content_parts = []
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                # 思考型模型（Qwen3.8）先输出 reasoning_content 再输出 content，两者都计入
                piece = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
                if piece:
                    now = time.perf_counter()
                    if first_token_time is None:
                        first_token_time = now
                    last_token_time = now
                    n_tokens += 1
                    if delta.get("content"):
                        content_parts.append(delta["content"])
    ttft = (first_token_time - t0) if first_token_time else None
    return ttft, first_token_time, last_token_time, n_tokens, usage, "".join(content_parts)


def fmt_sec(x):
    if x is None:
        return "n/a"
    return f"{x * 1000:.1f} ms" if x < 1.0 else f"{x:.3f} s"


# ---------------- 能耗采样（Windows：WMI root\wmi BatteryStatus.DischargeRate，单位 mW） ----------------

class PowerSampler:
    """后台 PowerShell 周期采样电池放电率写入临时 CSV；请求结束后按时间窗求平均功率。

    仅离电（电池放电）时有意义；插电时 DischargeRate 为 0，avg_watts 返回 None。
    """

    PS_TEMPLATE = r"""
$out = '__FILE__'
while ($true) {
    try {
        $b = Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus -ErrorAction Stop
        $mw = ($b | Measure-Object -Property DischargeRate -Sum).Sum
        if ($null -ne $mw) {
            $ts = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            Add-Content -Path $out -Value "$ts,$mw"
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
"""

    def __init__(self):
        fd, self.path = tempfile.mkstemp(suffix=".csv", prefix="qp_power_")
        os.close(fd)
        self.proc = None
        self.samples = []  # [(epoch 秒, 瓦)]

    def start(self):
        ps = self.PS_TEMPLATE.replace("__FILE__", self.path)
        try:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            self.proc = None

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.strip().split(",")
                    if len(parts) == 2:
                        try:
                            self.samples.append((int(parts[0]) / 1000.0, float(parts[1]) / 1000.0))
                        except ValueError:
                            continue
        except OSError:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass

    def avg_watts(self, t0, t1):
        """[t0, t1]（epoch 秒）窗口平均功率；无有效放电样本返回 None。"""
        vals = [w for (ts, w) in self.samples if t0 - 1 <= ts <= t1 + 1 and w > 0]
        if not vals:
            return None
        return sum(vals) / len(vals)


def run_once(url, model, prompt, max_tokens, label, sampler=None):
    t_req = time.time()
    ttft, ftt, ltt, n, usage, _text = stream_chat(url, model, prompt, max_tokens)
    t_end = time.time()
    prompt_tokens = usage.get("prompt_tokens") if usage else None
    completion_tokens = usage.get("completion_tokens") if usage else None
    total = (ltt - ftt) if (ltt and ftt) else None
    decode_tps = (n - 1) / total if (n > 1 and total and total > 0) else None
    msg = f"[{label}] TTFT={fmt_sec(ttft)}  tokens={n}"
    if prompt_tokens is not None:
        msg += f"  prompt_tok={prompt_tokens}"
    if completion_tokens is not None:
        msg += f"  completion_tok={completion_tokens}"
    if decode_tps:
        msg += f"  decode={decode_tps:.2f} tok/s"
    print(msg)
    if sampler:
        watts = sampler.avg_watts(t_req, t_end)
        if watts and decode_tps:
            print(f"        能效：{decode_tps / watts:.3f} tok/s/W（平均放电功率 {watts:.1f} W）")
        elif watts:
            print(f"        能效：平均放电功率 {watts:.1f} W（decode 数据不足）")
        else:
            print("        能效：n/a（插电或无电池放电数据）")
    return ttft


# ---------------- 多 session 缓存隔离（GOALS §6 第 1 层：多 session 并存） ----------------

def make_session_prompt(sid):
    filler = "、".join(f"主题词{sid}-{i}" for i in range(64))
    return (f"你是会话 {sid} 的专属助手。本会话背景资料（第 {sid} 套，与其他会话完全不同）：{filler}。"
            "请牢记以上背景。")


def run_sessions(url, model, n_sessions, rounds, max_tokens):
    """round-robin 交错请求 N 个会话；每会话维护完整对话历史（append-only）。

    这是真实 chat 客户端行为：每轮发送 base + 此前全部问答 + 新问题。
    llama.cpp 混合架构（attention+recurrent）的缓存回滚粒度是「消息边界 /
    prompt 尾部 checkpoint」，因此只有 append-only 历史才能命中前缀缓存——
    丢弃历史重发（LCP 停在旧 prompt 尾 checkpoint 之前）会触发全量重算。

    预期：各会话第 2+ 轮 TTFT 相对第 1 轮大幅下降（缓存独立命中，无串扰）。
    提示：服务器需 --parallel N 才有真并发 slot；slot 选择依赖
    --slot-prompt-similarity（LRU 在 3+ 会话交错时会错配 slot）。
    """
    histories = [[{"role": "user", "content": make_session_prompt(s)}] for s in range(n_sessions)]
    ttf = {}
    for r in range(rounds):
        for s in range(n_sessions):
            histories[s].append({
                "role": "user",
                "content": f"这是第 {r + 1} 轮提问。请用一句话回答：背景资料的第 {r + 2} 个主题词是什么？",
            })
            ttft, *_rest = stream_chat(url, model, None, max_tokens, messages=histories[s])
            ttf[(s, r)] = ttft
            reply = _rest[-1].strip()
            histories[s].append({"role": "assistant", "content": reply or "（无回复）"})
            print(f"[round {r + 1} / session {s}] TTFT={fmt_sec(ttft)}  history={len(histories[s])} msgs")

    print("-- 多 session 缓存隔离汇总 --")
    all_hit = True
    for s in range(n_sessions):
        first = ttf.get((s, 0))
        later = [ttf[(s, r)] for r in range(1, rounds) if ttf.get((s, r)) is not None]
        if first and later:
            best = min(later)
            gain = (1 - best / first) * 100
            print(f"  session {s}: first={fmt_sec(first)}  best={fmt_sec(best)}  cache 增益={gain:.0f}%")
            if gain < 50:
                all_hit = False
        else:
            print(f"  session {s}: 数据不足")
            all_hit = False
    print("结论：" + ("各 session 缓存独立命中" if all_hit else "存在缓存未命中/串扰，需排查（见 GOALS §6 第 1 层）"))


def main():
    ap = argparse.ArgumentParser(description="AI-Qwen-Max benchmark")
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prompt", default=None, help="prompt 文本")
    ap.add_argument("--prompt-file", default=None, help="从文件读取 prompt（长 prompt / 前缀）")
    ap.add_argument("--repeat", type=int, default=1, help="同前缀重复次数（观察前缀缓存命中）")
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--warmup", action="store_true", help="先跑一次不计入结果（预热 / 加载模型）")
    ap.add_argument("--sessions", type=int, default=0, help="多 session 缓存隔离测试的会话数（>0 启用该模式）")
    ap.add_argument("--rounds", type=int, default=3, help="多 session 模式的轮数")
    ap.add_argument("--energy", action="store_true", help="采样电池放电率输出 tok/s per Watt（离电有效）")
    args = ap.parse_args()

    sampler = None
    if args.energy:
        sampler = PowerSampler()
        sampler.start()
        print("能耗采样：已启动（WMI BatteryStatus；插电时无放电数据将显示 n/a）")

    try:
        if args.sessions > 0:
            print(f"多 session 模式：sessions={args.sessions}  rounds={args.rounds}  max_tokens={args.max_tokens}")
            run_sessions(args.url, args.model, args.sessions, args.rounds, max(args.max_tokens, 16))
            return

        if args.prompt_file:
            with open(args.prompt_file, "r", encoding="utf-8") as f:
                prompt = f.read()
        elif args.prompt:
            prompt = args.prompt
        else:
            prompt = "你好，请介绍一下你自己。"

        print(f"url={args.url}  model={args.model}  prompt_chars={len(prompt)}  "
              f"repeat={args.repeat}  max_tokens={args.max_tokens}")

        if args.warmup:
            print("-- 预热（不计入结果）--")
            run_once(args.url, args.model, prompt, min(args.max_tokens, 16), "warmup")

        ttf = []
        for i in range(args.repeat):
            ttft = run_once(args.url, args.model, prompt, args.max_tokens, f"run{i + 1}", sampler)
            if ttft is not None:
                ttf.append(ttft)

        if len(ttf) > 1 and ttf[0] > 0:
            later = [x for x in ttf[1:]]
            print(f"-- TTFT 汇总：first={fmt_sec(ttf[0])}  min={fmt_sec(min(ttf))}  "
                  f"avg={sum(ttf) / len(ttf) * 1000:.1f} ms  "
                  f"cache-hit 增益 vs first = {(1 - min(later) / ttf[0]) * 100:.0f}%")
    finally:
        if sampler:
            sampler.stop()


if __name__ == "__main__":
    main()
