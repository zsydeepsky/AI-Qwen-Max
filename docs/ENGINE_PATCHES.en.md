# Engine Customization Notes (vendor/llama.cpp @ qwenmax branch)

[简体中文](ENGINE_PATCHES.md) ｜ **English**

The `qwenmax` branch = one customization commit on top of the `strix-halo-vulkan` branch of upstream [Nathanw1014/llama.cpp](https://github.com/Nathanw1014/llama.cpp) (baseline `baf0025de`). All changes are listed by functional area for code review and upstream rebases.

## A. SSD prompt cache (the bulk of the customization, ~700 lines)

**Files**: `tools/server/server-task.h`, `tools/server/server-task.cpp`, `common/arg.cpp`, `common/common.h`, `tools/server/server.cpp`

- `server_prompt_cache_ssd`: the SSD tier of the RAM (upstream `server_prompt_cache`) -> SSD -> drop storage hierarchy.
  - Disk layout: `index.bin` ("SSDI" magic, v2, includes raw token vectors for LCP matching) + `state_<id>.bin` ("SSDC" magic: tokens + checkpoints + blobs).
  - Atomic writes: tmp+rename (with retry on Windows rename failure); a corrupted index does not doom the whole pool.
  - Fingerprint: FNV-1a over model description / layer count (incl. nextn) / embd / heads / model path / file size / build version / **cache_type_k/v**; any mismatch clears the pool.
- Three hook points:
  1. RAM eviction spill: `server_prompt_cache::alloc()/update()` calls `ssd->save_state()` before eviction
  2. Load best-pick: after the RAM walk, `load()` calls `ssd->load_state()`; SSD wins only if strictly better (one-shot; failed entries are kept for retry)
  3. `destroy()`: save all idle-slot prompts before teardown + `flush_to_ssd()` (upstream saves nothing — the critical gap this closes)
- Flags: `--cache-ssd N` (0=off, -1=unlimited), `--cache-ssd-path DIR`, `--cache-ssd-ttl-hours N` (lazy TTL cleanup)
- Endpoints: `GET /cache/stats` (RAM/SSD/heal stats), `POST /cache/evict?ram_target_mib=N` (proactive eviction, persist not drop)

## B. Generation-phase checkpoints + BPE heal

**Files**: `tools/server/server-context.cpp`, `tools/server/server-task.h`

- `maybe_gen_checkpoint`: rolling snapshot every `QWENMAX_GEN_CKPT_STEP=256` tokens during decode (threshold-crossing check instead of modulo — MTP multi-token accepts skip modular positions; 2 in-flight snapshots kept). Hooked into **both stop paths** (plain sampling and MTP accept).
- `maybe_final_checkpoint`: final snapshot when generation ends, so the next turn's n_past goes straight to the end.
- `retokenize_with_cache`: text-level LCP (UTF-8 boundary fallback, gives up beyond 256 bytes) + shared-prefix cached tokens + tail re-tokenization; **detokenize round-trip verification** — on failure the original tokens are kept. Candidate pool = active slots + RAM states (<=64); SSD joins only on cold start. Heal counters feed into `/cache/stats`.
- Accompanying: `server_slot::n_decoded_ckpt_last`, `create_checkpoint` identical-skip, `id_task` ownership marking on restore (prevents the min-step rule from re-snapshotting a ~150MiB state repeatedly).

## C. UMA / Vulkan performance customizations

**Files**: `ggml/src/ggml-vulkan/ggml-vulkan.cpp`, `src/llama-graph.cpp`

- **reads_clean fast path**: `vk_device_struct.reads_clean` (atomic) + `vk_command_pool::owner_device`; UMA reads skip the per-tensor barrier+submit+fence when no submissions are in flight — a plain memcpy.
- **memtypes**: `ggml_vk_find_memory_properties/create_buffer` gained `exclude_flags`; `prefer_host_memory` forced on by default (three-step HostCached GTT chain), fixing ~100MB/s WC readback on AMD Windows. `GGML_VK_PREFER_HOST_MEMORY=0` restores the old behavior.
- **f16acc**: `llama-graph.cpp build_attn_mha`; the `QWENMAX_FA_F16ACC` env skips the upstream-forced F32 accumulation (prefill +9%).
- **op stats**: wall-time accumulation around `build_graph` enqueue; stderr histogram every 10s.
- **A/B switches**: `GGML_VK_AMD_L_TILES` (l-tile), `GGML_VK_GDN_CPU`; one-shot dumps: memtypes / FA path / GDN dims.

## D. /max/shutdown endpoint

**Files**: `tools/server/server.cpp`

`POST /max/shutdown` -> replies `{"stopping":true}` first, then 200ms later a detached thread calls `llama_server_terminate()`. Same exit path as Ctrl+C (-> clean_up -> destroy -> SSD flush). Rationale: cross-process CTRL_C_EVENT is unreliable on Windows; supervisors speak HTTP.

## E. Bug fixes (upstream / our own)

| Fix | Location |
|---|---|
| `has_mtmd` semantics: model-level capability != this prompt containing media; ~12 guards changed to `find_next_media_chunk(0).first == nullptr` | server-common/task/context |
| prompt-cache load must restore `has_mtmd` from mctx after restoring slot.prompt.tokens | server-context.cpp |
| zero-sized view not triggering buffer flush -> NULL data crash | ggml-alloc.c |
| qwen3_coder tool-call parsing: newline tolerance before `</parameter>` + `tool_choice=REQUIRED` grammar enforcement | common/chat.cpp |
| auxiliary-request LCP preemption: similarity takeover gains a `cache_prompt` condition + LRU takeover `empty_base` | server-context.cpp |
| `remove_contained` iterator invalidation | server-task.cpp |
| Web UI title-generation request `cache_prompt: false` | tools/ui chat.service.ts |

## F. Removed (historical experiments, do not reintroduce)

- **TurboQuant TQ4** (GGML_TYPE_TURBO4_0 / GGML_OP_TURBO_WHT / turbo shaders / codec / FA fusion): behavioral quality regression, rejected; fully stripped from the branch (GGML_TYPE_COUNT=43, GGML_OP_COUNT=101 match upstream).
- Assorted `_ReturnAddress`/fprintf temporary diagnostics.

## Porting / rebase notes

1. SSD format v2 couples with the upstream `server_tokens` serialization API; re-verify when rebasing onto a new upstream.
2. `prompt_cache_ssd` members must be declared after `prompt_cache` (destruction order, raw pointers).
3. The cache fingerprint must include the KV types (states are incompatible across cache_types).
4. Media checks always use `find_next_media_chunk`, never `has_mtmd`.
5. `--cache-ssd` depends on `--cache-ram` being enabled; otherwise the warning is ignored.
