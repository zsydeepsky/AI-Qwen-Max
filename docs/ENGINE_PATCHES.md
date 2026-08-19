# 产品层补丁说明（vendor/llama.cpp @ ryzen-uma-vulkan 分支）

**简体中文** ｜ [English](ENGINE_PATCHES.en.md)

架构（2026-08-19 拆分）：引擎仓库 = **纯平台层**（[Ryzen-UMA-Vulkan-llama](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama)，`ryzen-uma-vulkan` 分支 = 上游 [Nathanw1014/llama.cpp](https://github.com/Nathanw1014/llama.cpp) `strix-halo-vulkan` 分支 + Vulkan/UMA 平台优化，可独立发行）。本文件列出的**产品层定制**以 `patches/qwenmax-server-layer.patch` 在构建时叠加（见 scripts/build.ps1 3b 步；引擎升级后用 scripts/refresh-patch.ps1 重新生成）。按功能域列出全部改动，便于 code review 与上游 rebase。

## A. SSD prompt cache（保留定制的主体，~700 行）

**文件**：`tools/server/server-task.h`、`tools/server/server-task.cpp`、`common/arg.cpp`、`common/common.h`、`tools/server/server.cpp`

- `server_prompt_cache_ssd`：RAM（上游 `server_prompt_cache`）→ SSD → 丢弃三级存储的 SSD 层。
  - 磁盘布局：`index.bin`（"SSDI" magic，v2，含 raw token 向量供 LCP 匹配）+ `state_<id>.bin`（"SSDC" magic：tokens + checkpoints + blobs）。
  - 原子写：tmp+rename（Windows rename 失败重试）；索引损坏不连坐整池。
  - 指纹：FNV-1a over 模型描述/层数（含 nextn）/embd/heads/模型路径/文件大小/build 版本/**cache_type_k/v**；不匹配整池清空。
- 三个挂接点：
  1. RAM 淘汰落盘：`server_prompt_cache::alloc()/update()` 驱逐前 `ssd->save_state()`
  2. load 择优：`load()` RAM 遍历后调 `ssd->load_state()`，SSD 严格胜出才恢复（one-shot，失败保留条目可重试）
  3. `destroy()`：teardown 前保存全部 idle slot prompt + `flush_to_ssd()`（上游不保存，是关键补齐）
- 参数：`--cache-ssd N`（0=关 -1=无限）、`--cache-ssd-path DIR`、`--cache-ssd-ttl-hours N`（TTL 懒清理）
- 端点：`GET /cache/stats`（RAM/SSD/heal 统计）、`POST /cache/evict?ram_target_mib=N`（主动驱逐，persist 不 drop）

## B. 生成段 checkpoint + BPE 治愈

**文件**：`tools/server/server-context.cpp`、`tools/server/server-task.h`

- `maybe_gen_checkpoint`：解码期每 `QWENMAX_GEN_CKPT_STEP=256` token 滚动快照（阈值交叉判断而非 modulo——MTP 多 token 接受会跳过模位置；保留 2 份段内快照）。**两条 stop 路径都挂**（plain sampling 与 MTP accept）。
- `maybe_final_checkpoint`：生成结束终拍，下一轮 n_past 直达末尾。
- `retokenize_with_cache`：文本级 LCP（UTF-8 边界回退，<256 字节放弃）+ 共享前缀缓存 token + 尾部重 tokenize；**detokenize 往返校验**失败则保留原 tokens。候选池 = 活跃 slots + RAM states（≤64），SSD 仅冷启动参与。治愈计数进 `/cache/stats`。
- 配套：`server_slot::n_decoded_ckpt_last`、`create_checkpoint` identical-跳过、restore 时 `id_task` 归属标记（防 min-step 规则反复重拍 ~150MiB 状态）。

## C. UMA / Vulkan 性能定制（引擎层，已迁至引擎仓库）

**文件**：`ggml/src/ggml-vulkan/ggml-vulkan.cpp`、`src/llama-graph.cpp`、`ggml/src/ggml-alloc.c`

> reads_clean 快路径、HostCached GTT、F16ACC、op stats、A/B 开关（V1-V10）与 ggml-alloc 零尺寸 view 修复（C05）均属**引擎仓库平台层**（独立于本 patch 分发行），详见
> [Ryzen-UMA-Vulkan-llama / CORE_MODIFICATIONS.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/CORE_MODIFICATIONS.md)。
> 引擎升级（rebase）时由引擎仓库按 V1-V10 逐项核对，本 patch 不包含这些改动。

## D. /max/shutdown 端点

**文件**：`tools/server/server.cpp`

`POST /max/shutdown` → 先答 `{"stopping":true}`，200ms 后 detached 线程 `llama_server_terminate()`。走与 Ctrl+C 完全相同的退出路径（→ clean_up → destroy → SSD 落盘）。动机：Windows 跨进程 CTRL_C_EVENT 不可靠，supervisor 走 HTTP。

## E. Bug 修复（上游/自身）

| 修复 | 位置 |
|---|---|
| `has_mtmd` 语义：模型级能力 ≠ 本 prompt 含媒体，~12 处守卫改 `find_next_media_chunk(0).first == nullptr` | server-common/task/context |
| prompt-cache load 恢复 slot.prompt.tokens 后 `has_mtmd` 须按 mctx 恢复 | server-context.cpp |
| qwen3_coder 工具调用解析：`</parameter>` 前缺换行容错 + `tool_choice=REQUIRED` 语法强制 | common/chat.cpp |
| 辅助请求 LCP 抢占：相似度接管加 `cache_prompt` 条件 + LRU 接管 `empty_base` | server-context.cpp |
| `remove_contained` 迭代器失效 | server-task.cpp |

> 注：ggml-alloc 零尺寸 view 修复（C05）属引擎层，见 [引擎仓库 CORE_MODIFICATIONS.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/CORE_MODIFICATIONS.md)；Web UI 标题 `cache_prompt: false`（C11）已于 2026-08-19 删除。

## F. 已剥离（历史尝试，勿复入）

> TurboQuant 与临时诊断均属**引擎层历史**，记录见引擎仓库；此处仅保留警示，勿复入。

- **TurboQuant TQ4**（GGML_TYPE_TURBO4_0 / GGML_OP_TURBO_WHT / turbo shaders / codec / FA 融合）：行为级降智已否决，代码已从分支中完全移除（GGML_TYPE_COUNT=43、GGML_OP_COUNT=101 与上游一致）。
- 各类 `_ReturnAddress`/fprintf 临时诊断。

## 移植/Rebase 注意事项

1. SSD 格式 v2 与上游 `server_tokens` 序列化 API 耦合，rebase 新上游需重验。
2. `prompt_cache_ssd` 成员必须声明在 `prompt_cache` 之后（析构序，raw 指针）。
3. 缓存指纹必须含 KV 类型（不同 cache_type 状态不兼容）。
4. 媒体检查一律用 `find_next_media_chunk`，不要用 `has_mtmd`。
5. `--cache-ssd` 依赖 `--cache-ram` 开启，否则告警忽略。
