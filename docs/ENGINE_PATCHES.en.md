# Product-Layer Patch Notes (vendor/llama.cpp @ ryzen-uma-vulkan branch)

[简体中文](ENGINE_PATCHES.md) ｜ **English**

Architecture (split 2026-08-19): the engine repo is a **pure platform layer**
([Ryzen-UMA-Vulkan-llama](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama), branch `ryzen-uma-vulkan` = the `strix-halo-vulkan` branch of upstream [Nathanw1014/llama.cpp](https://github.com/Nathanw1014/llama.cpp) + Vulkan/UMA platform tuning, distributable standalone). The **product-layer customizations** listed here are applied at build time via `patches/qwenmax-server-layer.patch` (see scripts/build.ps1 step 3b; regenerate with scripts/refresh-patch.ps1 after engine upgrades). All changes are listed by functional area for code review and upstream rebases.

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

- `maybe_final_checkpoint`: unified end-of-stop snapshot so the next turn's n_past goes straight to the end. Hooked into **both stop paths** — normal stop (decode loop `process_token`) and user interrupt (`SERVER_TASK_TYPE_CANCEL` handler, before `slot.release()`). The interrupt also snapshots, so a cut-off reply stays reusable. No more rolling in-decode snapshots.
- `retokenize_with_cache`: text-level LCP (UTF-8 boundary fallback, gives up beyond 256 bytes) + shared-prefix cached tokens + tail re-tokenization; **detokenize round-trip verification** — on failure the original tokens are kept. Candidate pool = active slots + RAM states (<=64); SSD joins only on cold start. Heal counters feed into `/cache/stats`.
- Accompanying: `create_checkpoint` identical-skip, `id_task` ownership marking on restore (prevents the min-step rule from re-snapshotting a ~150MiB state repeatedly).

## C. UMA / Vulkan performance customizations (engine layer, moved to the engine repo)

**Files**: `ggml/src/ggml-vulkan/ggml-vulkan.cpp`, `src/llama-graph.cpp`, `ggml/src/ggml-alloc.c`

> The reads_clean fast path, HostCached GTT, F16ACC, op stats, A/B switches (V1-V10) and the ggml-alloc zero-sized-view fix (C05) all belong to the **engine repo platform layer** (shipped independently of this patch). See
> [Ryzen-UMA-Vulkan-llama / CORE_MODIFICATIONS.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/CORE_MODIFICATIONS.md).
> The engine repo reconciles them against upstream V1-V10 on engine upgrades; this patch does not contain them.

## D. /max/shutdown endpoint

**Files**: `tools/server/server.cpp`

`POST /max/shutdown` -> replies `{"stopping":true}` first, then 200ms later a detached thread calls `llama_server_terminate()`. Same exit path as Ctrl+C (-> clean_up -> destroy -> SSD flush). Rationale: cross-process CTRL_C_EVENT is unreliable on Windows; supervisors speak HTTP.

## E. Bug fixes (upstream / our own)

| Fix | Location |
|---|---|
| `has_mtmd` semantics: model-level capability != this prompt containing media; ~12 guards changed to `find_next_media_chunk(0).first == nullptr` | server-common/task/context |
| prompt-cache load must restore `has_mtmd` from mctx after restoring slot.prompt.tokens | server-context.cpp |
| qwen3_coder tool-call parsing: newline tolerance before `</parameter>` + `tool_choice=REQUIRED` grammar enforcement | common/chat.cpp |
| auxiliary-request LCP preemption: similarity takeover gains a `cache_prompt` condition + LRU takeover `empty_base` | server-context.cpp |
| `remove_contained` iterator invalidation | server-task.cpp |

> Note: the ggml-alloc zero-sized-view fix (C05) belongs to the engine layer — see [engine repo CORE_MODIFICATIONS.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/CORE_MODIFICATIONS.md); the Web-UI title `cache_prompt: false` (C11) was removed on 2026-08-19.

## F. Removed (historical experiments, do not reintroduce)

> TurboQuant and the temporary diagnostics are **engine-layer history** (recorded in the engine repo); kept here only as a warning, do not reintroduce.

- **TurboQuant TQ4** (GGML_TYPE_TURBO4_0 / GGML_OP_TURBO_WHT / turbo shaders / codec / FA fusion): behavioral quality regression, rejected; fully stripped from the branch (GGML_TYPE_COUNT=43, GGML_OP_COUNT=101 match upstream).
- Assorted `_ReturnAddress`/fprintf temporary diagnostics.

## Porting / rebase notes

1. SSD format v2 couples with the upstream `server_tokens` serialization API; re-verify when rebasing onto a new upstream.
2. `prompt_cache_ssd` members must be declared after `prompt_cache` (destruction order, raw pointers).
3. The cache fingerprint must include the KV types (states are incompatible across cache_types).
4. Media checks always use `find_next_media_chunk`, never `has_mtmd`.
5. `--cache-ssd` depends on `--cache-ram` being enabled; otherwise the warning is ignored.
