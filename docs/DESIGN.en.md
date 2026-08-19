# AI-Qwen-Max Design Document

[简体中文](DESIGN.md) ｜ **English**

This document is the design baseline of the project: architecture decisions and verified technical conclusions (rejected items included).

## 1. Goals and non-goals

**Objective function**: maximum inference capability per unit of energy (tok/s per Watt), running Qwen-family GGUF models on Strix Halo unified-memory platforms.

**Non-goals** (explicitly abandoned):
- Multi-hardware / multi-platform support (Windows + gfx1151 + Vulkan only)
- GUI clients (CLI + a lightweight web/ static page is enough)
- General-purpose quantization schemes (TurboQuant TQ4 was tested and rejected)
- Modifying the OS power plan (power profiles proved worthless and were removed; only in-process compute efficiency)

## 2. Three-layer architecture

```
L3  User entry     CLI (cli.py) / web/ static UI (index.html entry, served at GET /, extensible html/js/css)
L2  Service front  ai_qwen_max package (FastAPI :8080)
                  ├─ Session management (.max/chat/<sid>/, atomic writes)
                  ├─ OpenAI-compatible reverse proxy (/v1/*, concurrency gate, X-Conversation-Id persistence)
                  ├─ Max management API (/model/load /chat/* /cache/* /status ...)
                  └─ Observability stream (/api/events SSE, 300-entry ring buffer)
L1  Inference      llama-server (:8081 subprocess, vendor/llama.cpp ryzen-uma-vulkan branch)
                  K8V8 / RAM+SSD two-tier prompt cache / MTP / checkpoints / retokenize
```

Between the frontend and the engine there is only HTTP (OpenAI-compatible plus a few extension endpoints); the engine can be started standalone for debugging (scripts/run.ps1).

## 3. Key design decisions (finalized)

### 3.1 KV quantization: K8V8 anchored
- K=q8_0, V=q8_0. The strix-halo baseline (baf0025) eliminated the coopmat1 FA re-dequant penalty for quantized KV, so quantized KV no longer costs prefill speed.
- **TQ4 (TurboQuant, 4.125bpv on the V side) rejected**: decode only +2%, but in testing it made the model refuse tool calls (behavioral degradation). Lesson: quantization acceptance must include behavioral tests (tool calling / long context); greedy short-chat smoke tests are not enough.
- A single KV layout is the prerequisite for SSD/RAM cache reuse; the cache fingerprint includes cache_type_k/v.

### 3.2 Cache: RAM -> SSD -> drop, three levels
- RAM pool (default 48GB, `--cache-ram`): multi-session prompt cache, LRU + similarity takeover.
- SSD pool (default 64GB, `--cache-ssd`): spilled on RAM eviction / engine exit, restored across restarts; default TTL 24h; index written atomically (tmp+rename).
- Fingerprint check: model description / layer count / embd / heads / path / size / build version / KV type — any mismatch clears the whole pool.
- Low-memory proactive eviction: `POST /cache/evict?ram_target_mib=N` (persist, not drop).

### 3.3 Generation checkpoints + BPE heal (the two guardians of cache hits)
- Hybrid architectures (SSM+attention) roll back via checkpoints, not KV shift. Upstream only builds checkpoints during the prompt phase, so last turn's reply got fully recomputed the next turn. Custom: rolling snapshot every 256 tokens during decode (2 kept) + a final snapshot when generation ends.
- Greedy per-token output vs whole-segment re-tokenization can disagree on BPE boundaries, defeating token-level LCP. Custom: retokenize_with_cache (text-level LCP + detokenize round-trip check); candidate pool = active slots + RAM states (SSD participates only on cold start).

### 3.4 Speculative decoding: DFlash2 (standalone draft model, replacing MTP)
- Previously used MTP (model-embedded nextn head, `--spec-type draft-mtp`): Qwen3.8's MTP head is tied to xHigh reasoning; changing the reasoning effort made the acceptance rate collapse, so it slowed output instead of helping — fully removed.
- Now uses `--spec-type draft-dflash --spec-draft-model <draft.gguf>`: the draft model path comes from each model entry's `DFlash2_draft_model` field (`config.models` object list); speculative decoding is off when unset.
- Optional tuning field (omitted by default, using engine default): `spec_n_max` draft token cap (default 3, DFlash2 cap = block size 8). `/model/load` re-reads config each time, so changes take effect on hot-swap. Note: DFlash's `--spec-draft-conf-min` is not implemented in this engine (docs only), not exposed.

### 3.5 UMA memory (the critical Strix Halo fix)
- AMD's Windows driver maps HostVisible non-HostCached memory as write-combined: CPU reads at ~100MB/s, so an SSM checkpoint snapshot (150MiB) took 1.4s.
- Custom: prefer_host_memory forced on by default (HostCached GTT, type 0xe) + the reads_clean fast read path -> snapshot in 40ms (45x). `GGML_VK_PREFER_HOST_MEMORY=0` restores the old behavior for A/B comparison.

### 3.6 Context tiers
- `CTX_CHOICES = [4096, 16384, 65536, 262144]` (per-slot), `--parallel 3` (2 API + 1 CLI/Web); total engine ctx = tier x 3.
- Switching tier/model = restarting the engine subprocess (ctx is a load-time parameter); cross-tier cache compatibility is guaranteed by actual token counts and fingerprint checks.

### 3.7 Service details
- Concurrency gate: POST /v1/* passes a Semaphore(2) matching the slot budget; observable via `GET /queue`.
- Auxiliary requests (title generation etc.) must use `cache_prompt: false`, preventing LCP similarity from stealing an active session's slot.
- Streaming passthrough: the proxy never re-assembles SSE; deltas are parsed on the side for observability and persistence.
- Session persistence: X-Conversation-Id -> `.max/chat/<cid>/`, full-history merge (tail-match incremental append, wholesale replace on mismatch); atomic writes.
- Graceful shutdown: `POST /max/shutdown` (engine) -> same exit path as Ctrl+C (SSD flush); cross-process signals are unreliable on Windows, so shutdown always goes over HTTP.

## 4. Python package layout

```
ai_qwen_max/
├── __main__.py   entry: argparse + wiring + uvicorn (background thread) + CLI foreground
├── config.py     Config (.max/config.json, atomic writes, defaults are the production baseline)
├── gguf.py       GGUF header parsing (template/multimodal/max-output probing, stdlib only)
├── backend.py    Backend: process lifecycle / flag assembly / readiness probing / graceful shutdown
├── store.py      SessionStore/Session: session persistence (atomic writes + dialogue replay + media)
├── events.py     ApiEvents: /api/events ring buffer + SSE delta push
├── server.py     FastAPI: proxy + Max API + observability + web/ static serving (UI contract lives here)
├── llm.py        LLM: streaming client for the CLI track (SSE parsing / reasoning / interrupt)
└── cli.py        Cli: language/model/tier/effort selection → menu (chat / delete history / API log) / universal ESC back / loading spinner / title-bar state machine
```

Implementation notes (known pitfalls of this kind of integration, all handled here):
1. Disabling thinking requires explicitly injecting `enable_thinking:false` (the server's setdefault base would override the default)
2. Preserve tool_calls/name fields when normalizing messages (OpenAI clients depend on them for tool calling)
3. Session/config files must be written atomically via tmp+rename (the process may be killed at any time)
4. Detect streaming chunks via JSON parsing, not byte matching (SSE chunk boundaries are unstable)
5. The proxy must not do a pre-flight healthy() check (+3s per request); forward directly and return 502 on failure
6. Never tokenize full session lists (worst case 90s per session); get cache status from /cache/stats

## 5. Build / release

- `scripts/build.ps1`: NMake + Vulkan-only trimmed build (GGML_NATIVE, all unrelated backends off); the Web UI is built via npm and embedded into llama-server.exe as a C byte array.
- `scripts/build_exe.ps1`: PyInstaller onedir portable build (`dist/max/max.exe`); the entry stub is generated at packaging time; collects 9 engine DLL/EXEs.
- Engine source = submodule `vendor/llama.cpp` (ryzen-uma-vulkan branch = upstream strix-halo-vulkan baseline + Vulkan/UMA platform tuning; product layer applied at build time via `patches/qwenmax-server-layer.patch`).

## 6. Rejected technologies (do not reintroduce)

| Technology | Verdict | Reason |
|---|---|---|
| TurboQuant TQ4 | ❌ | Behavioral degradation (refuses tools), decode only +2% |
| Power profiles | ❌ | Touching the power plan gained nothing; in-process parameters suffice |
| SSD cache lossless compression | ❌ | q8_0 residual entropy is nearly full; deflate saves only 5-6% |
| MTP embedded head (Qwen3.8) | ❌ | Tied to xHigh reasoning; changing effort collapsed the acceptance rate and slowed output; replaced by the DFlash2 standalone draft model |
| prefill row-split coordination | ❌ | Layer assignment did not meet expectations |
| l-tile GEMM | ⏸ | Driver-dependent; -38% on the current driver, kept off (`GGML_VK_AMD_L_TILES=0`); retest after driver updates |
