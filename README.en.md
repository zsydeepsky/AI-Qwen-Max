# AI-Qwen-Max

[简体中文](README.md) ｜ **English**

A high-efficiency local Qwen inference service for the **AMD Ryzen AI Max+ 395 (Strix Halo / gfx1151) unified-memory platform**.

One CLI window + an OpenAI-compatible HTTP API + a lightweight web UI, backed by a llama.cpp engine deeply customized for Strix Halo: K8V8 KV quantization, a two-tier RAM→SSD prompt cache, MTP speculative decoding, generation-phase checkpoints, and cache-aware retokenization. There is exactly one objective function — **maximum inference capability per unit of energy (tok/s per Watt)**.

> Hardware constraint: this project deliberately does not generalize. Windows + Strix Halo (Ryzen AI Max 395/395+) + Vulkan only. For anything else, use upstream [llama.cpp](https://github.com/ggml-org/llama.cpp).

## Quick Start

```powershell
# 0) Prerequisites: Visual Studio (C++ desktop workload), Vulkan SDK, Python 3.10+, Node.js (builds the embedded Web UI)
winget install Microsoft.VisualStudio.2022.Community KhronosGroup.VulkanSDK Python.Python.3.12 OpenJS.NodeJS.LTS

# 1) Clone (with the engine submodule)
git clone --recursive https://github.com/zsydeepsky/AI-Qwen-Max.git
cd AI-Qwen-Max

# 2) Build the engine (first build ~15-30 min, includes the npm Web UI build)
.\scripts\build.ps1

# 3) Install frontend dependencies
pip install -e .

# 4) Download a model (example)
.\scripts\download-model.ps1 -Repo unsloth/Qwen3.8-27B-GGUF -File Qwen3.8-27B-UD-Q5_K_XL.gguf -OutPath models\

# 5) Start (after pip install -e ., the `max` command is equivalent)
python -m ai_qwen_max            # interactive mode: pick model -> pick context tier -> chat
python -m ai_qwen_max --serve    # headless service mode
```

Once running:
- Chat directly in the CLI (startup flow: language → frontend port → model → context tier → thinking effort → menu; ESC universally interrupts/goes back)
- Open `http://127.0.0.1:8080` in a browser — the web chat UI (static directory rooted at `web/index.html`, freely extensible with html/js/css)
- OpenAI-compatible endpoint: `http://127.0.0.1:8080/v1/chat/completions`
- Management API: `http://127.0.0.1:8080/help`

## Architecture

```
web UI / CLI ─────────┐
                      ├─► ai_qwen_max (FastAPI :8080) ──► llama-server (dynamic-port subprocess)
external OpenAI clients ──┘  session mgmt / proxy / observability     │
                                                                       ▼
                                                           vendor/llama.cpp (qwenmax branch)
                                                           Strix Halo tuned engine (submodule)
```

```
.max/                        runtime instance directory (auto-created)
├── config.json              configuration (model list / ports / cache pools / thinking effort)
├── chat/<sid>/              sessions: messages.json + dialogue.txt + media/
├── cache-ssd/               SSD prompt-cache pool (survives restarts, 24h TTL)
└── llama-server.log
```

## Core capabilities (why it is fast)

| Layer | Mechanism | Effect |
|---|---|---|
| KV quantization | K8V8 (q8_0/q8_0) anchored | -50% KV size; the strix-halo baseline already eliminates the prefill penalty of quantized KV |
| Prefix cache | Two-tier RAM pool (48GB) -> SSD pool (64GB); eviction spills to disk, restored across restarts | Warm TTFT after restart 6.2s -> 2.0s |
| Speculative decoding | Native Qwen MTP (nextn head) via draft-mtp | Lowest energy per token |
| Generation checkpoints | Rolling snapshot every 256 tokens during decode + final snapshot at generation end | Interruptions / multi-turn chats no longer recompute the whole prefill |
| BPE heal | Cache-aware retokenize (text-level LCP + detokenize round-trip check) | Token-boundary divergence between greedy generation and re-rendering no longer busts the cache |
| UMA memory | HostCached GTT mapping + reads_clean fast path | SSM checkpoint snapshot 1.4s -> 40ms (45x) |
| Attention | FA f16 accumulation (QWENMAX_FA_F16ACC) | prefill +9% |

Engine customization details: [docs/ENGINE_PATCHES.en.md](docs/ENGINE_PATCHES.en.md) | Design document: [docs/DESIGN.en.md](docs/DESIGN.en.md)

## Engine & Credits

The inference engine [vendor/llama.cpp](vendor/llama.cpp) is a git submodule, based on:

- The `strix-halo-vulkan` branch of **[Nathanw1014/llama.cpp](https://github.com/Nathanw1014/llama.cpp)** (Strix Halo Vulkan optimizations — the baseline of this project's customization)
- Upstream [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp)
- Related project [Nathanw1014/strix-halo-llamacpp](https://github.com/Nathanw1014/strix-halo-llamacpp)

Our custom patches live on the `qwenmax` branch (full upstream history preserved, so following upstream updates stays easy). All code is MIT licensed.

### Syncing upstream updates

```powershell
cd vendor\llama.cpp
git remote add upstream https://github.com/Nathanw1014/llama.cpp.git   # first time only
git fetch upstream strix-halo-vulkan
git merge upstream/strix-halo-vulkan        # resolve conflicts, then re-run .\scripts\build.ps1
```

## Tools

| File | Purpose |
|---|---|
| `scripts/build.ps1` | Build the engine (MSVC + Vulkan-only trimmed build) |
| `scripts/run.ps1` | Start the engine directly (debugging; everyday use goes through `max` / `python -m ai_qwen_max`) |
| `scripts/build_exe.ps1` | PyInstaller portable packaging (`dist/max/max.exe`) |
| `scripts/download-model.ps1` | Download models from Hugging Face |
| `scripts/bench.py` | Benchmarks: TTFT / decode / multi-session cache isolation / energy (tok/s/W) |

## License

MIT ([LICENSE](LICENSE)). The engine submodule remains under its original license.
