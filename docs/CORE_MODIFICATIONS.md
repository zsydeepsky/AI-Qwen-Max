# 产品层核心修改技术文档（AI-Qwen-Max）

> 本文档阐述 **AI-Qwen-Max 产品层定制**（`patches/qwenmax-server-layer.patch` 叠加在引擎上的 server/chat 层改动）的技术深度版：动机、原理、核心算法摘抄，以及未来引擎升级后重新实现所需的全部要点。
>
> 定位（与其它文档的分工）：
> - [UPSTREAM.md](UPSTREAM.md) —— 产品层上游协作策略与逐项台账（哪项该上提 / 该删 / 该跟踪）
> - [ENGINE_PATCHES.md](ENGINE_PATCHES.md) —— 补丁清单与构建注意事项（A-F 补丁）
> - **本文档** —— 每个修改"为什么这么写、只在这个平台成立、代码长什么样"的技术深度版
>
> **引擎层**（C04 Vulkan/UMA 优化组、C05 ggml-alloc 修复）的台帐与平台背景已迁至引擎仓库
> [Ryzen-UMA-Vulkan-llama](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama) 的
> [CORE_MODIFICATIONS.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/CORE_MODIFICATIONS.md)（含可直接投递上游的英文 issue 素材），本文档不再重复。

---

## 1. C01 — SSD 两级 prompt 缓存（`--cache-ssd`）

### 1.1 动机

上游只有 RAM 层缓存（`--cache-ram`，默认 8GB）。本工程配置 48GB 仍装不下全部常驻会话，而统一内存下 RAM 与模型/KV 抢同一块内存。SSD 层把冷 prompt 落盘持久化：

- 释放 RAM（`evict_to_ssd` 低内存主动落盘）
- 跨进程重启仍命中（进程退出时 `flush_to_ssd` 全量落盘）
- 规避冷 TTFT（缓存命中 TTFT ~84ms，冷 prefill 数秒）

### 1.2 架构

```
RAM 层（server_prompt_cache，上游机制）
  ├── states: 热条目
  └── evict_to_ssd() / flush_to_ssd() / update() 钩子
SSD 层（server_prompt_cache_ssd，本工程独有）
  ├── index.bin（目录索引：entry id / token 数 / 文件大小 / 上次使用时间）
  └── state_<id>.bin（序列化的 prompt tokens + checkpoints + KV data_main/data_drft）
```

挂接点只有 `server_prompt_cache::load / update / evict_to_ssd / flush_to_ssd` 四处（这是 rebase 后核对的重点）。

### 1.3 关键设计决策

1. **模型指纹隔离**：缓存目录按模型指纹隔离（`desc|n_layer|n_embd|n_head|model path+size|build|cache_type_k/v` 哈希）。换模型不串缓存；格式/指纹不符则整目录清空重建。
2. **崩溃安全**：`index.bin` 与状态文件全部"写临时文件 + rename"原子提交。崩溃只丢最新一条，不损坏索引。
3. **LCP 匹配恢复**：`load_state` 对每个 SSD 条目算 token 级公共前缀，用 `f_keep`（命中占条目比例）与 `f_sim`（命中占新请求比例）双阈值挑最佳；比 RAM 层最优还强才用 SSD（避免 SSD 反噬 RAM 命中）。
4. **容量治理**：`make_room`（超限 LRU 淘汰最老）+ `cleanup_expired`（TTL 惰性清理，挂在 init/save/load 入口）+ `remove_contained`（新 prompt 完全包含旧条目时删旧，防止无限累积）。
5. **只存不删的驱逐**：`evict_to_ssd` 从 RAM 移出时**先持久化再删除**，内存压力下绝不丢缓存数据。

### 1.4 核心代码摘抄

保存状态（`server_prompt_cache_ssd::save_state`）——原子写 + 序列化 tokens/checkpoints/KV：

```cpp
// state_<id>.bin: [magic|fingerprint|n_tokens][tokens][n_ckpt][ckpt data x n][data_main][data_drft]
const auto path_state = (fs::path(path_dir) / ("state_" + std::to_string(id) + ".bin")).string();
// ...写临时文件 path_tmp...
w.write<uint32_t>(magic); w.write<uint64_t>(fingerprint);
w.write_blob(toks.data(), toks.size() * sizeof(llama_token));
w.write<uint32_t>((uint32_t) state.prompt.checkpoints.size());
for (const auto & ckpt : state.prompt.checkpoints) {
    w.write_blob(ckpt.data_tgt); w.write_blob(ckpt.data_dft); w.write_blob(ckpt.data_spec);
}
w.write_blob(state.data.main); w.write_blob(state.data.drft);
// ...fs::rename(path_tmp, path_state) 原子落盘
```

匹配恢复（`server_prompt_cache_ssd::load_state`）——LCP 打分 + KV 回填：

```cpp
for (auto it = entries.begin(); it != entries.end(); ++it) {
    const size_t lcp_cur = ssd_common_prefix(it->tokens, tokens_new);
    const float f_keep_cur = float(lcp_cur) / it->tokens.size();   // 命中占条目
    const float f_sim_cur  = float(lcp_cur) / tokens_new.size();   // 命中占新请求
    // 双阈值打分，取 best；比 RAM 层最优差则放弃 SSD
}
// ...读回 tokens/checkpoints 后：
llama_state_seq_set_data_ext(ctx_tgt, data_main.data(), sz, id_slot, 0); // KV 回填
if (!data_drft.empty()) {
    llama_state_seq_set_data_ext(ctx_dft, data_drft.data(), sz, id_slot, 0); // draft 状态回填
}
```

### 1.5 平台相关性 / 上提评估

- **平台特化**：SSD 层在内存充裕的非 UMA 平台收益小（RAM cache 直接装得下）；在 UMA 平台 RAM 是稀缺共享资源，SSD 才划算。
- **上提**：上游方向一致但未实现（PR #26408 `--cache-disk` 关闭、issue #20697 开放）。上提前需按四原则把 `server_prompt_cache_ssd` 整体迁入新文件（如 `tools/server/cache-ssd.cpp`），只留 4 个钩子。当前状态：**留本地，上提待评估**。

---

## 2. C02 — 生成段 checkpoint + BPE 治愈

这是本工程缓存体系的两个"隐性支柱"：一个解决"长回复后缓存死"，一个解决"编辑/重渲染后缓存死"。二者独立，但都服务于同一个目标：**让 55× TTFT 缓存收益在真实交互中存活**。

### 2.1 生成段 checkpoint（`maybe_gen_checkpoint` / `maybe_final_checkpoint`）

**问题**：上游 `create_checkpoint` 只在处理 prompt 时拍照（用户消息边界 + prompt 尾部），生成（decode）阶段不拍照。而 hybrid/循环模型 decode 后序列头（`pos_min`）滚到生成内容上，下一轮请求的恢复过滤器（`pos_max > pos_next` 才有效）把所有 checkpoint 判为无效 → 只能回退到"生成起点之前的 prompt 尾部" → **整段上轮回复重新 prefill**（"cache dies after every long reply"）。

**机制**：

```text
第 1 轮： [prompt 历史] [上游在此拍照] [模型生成长回复..........]
                                        ↑ 每 QWENMAX_GEN_CKPT_STEP token 滚一张
                                        ↑ 结束时 maybe_final_checkpoint 收尾一张
第 2 轮： 从最终快照（= prompt + 生成内容）恢复，不再重算
```

三个实现要点：

1. **threshold crossing 而非 modulo**：MTP 投机解码一次验证接受多个 token，`n_decoded` 会跳过取模位置，所以用"差值超过 STEP"判断：
   ```cpp
   if (slot.n_decoded - slot.n_decoded_ckpt_last < QWENMAX_GEN_CKPT_STEP) return;
   slot.n_decoded_ckpt_last = slot.n_decoded;
   ```
2. **滚动淘汰**：只保留最新 `QWENMAX_GEN_CKPT_KEEP` 张生成段快照（`n_tokens > n_prompt_task` 判定为生成段），老的最先删。
3. **两条 decode 路径都要调**：普通采样分支和投机（MTP）接受分支——MTP 开启时普通分支提前返回，漏调则整条链失效。
4. **`create_checkpoint` 跳过优化**：最新快照已是同任务、同 `pos_max`、同 token 数（例如刚从快照恢复还没生成新 token）时跳过拍照，省 ~150MiB 读回：
   ```cpp
   if (newest.id_task == id_task && newest.pos_max == pos_max &&
       newest.n_tokens == (int64_t)(slot.prompt.n_tokens() - n_tokens_cur)) return;
   ```

**成本**：每张快照 ~150MiB 显存→RAM 读回（hybrid 模型），step 256 时约 0.4% decode 吞吐。

**为什么平台特化**：RAM 管够 + 需要"长回复后下一轮免重算"的极端会话场景。大众设备上额外几百 MB RAM + 读回停顿可能挤垮推理栈。**上提评估：不普适，留本地。**

### 2.2 BPE 治愈（`retokenize_with_cache`）

**问题**（BPE 增量不变量）：同一段文本存在两种合法分词——

```text
整体重新分词（贪心合并）："充满了"（1 token）
逐步生成产出（已产出 token 不回头重合并）："充满" + "了"（2 token）
```

上一轮的回复是**逐步生成**的 token 流；下一轮重新渲染对话时整段文本被**整体重新分词**。两者序列不同 → 缓存复用的 token 级 LCP 在第一个边界断开（通常在上轮回复 ~100 token 处）→ 缓存 miss → 整个历史重新 prefill。

**机制**（slot 选择前执行）：

```text
1. 新请求 detokenize 成文本 text_new
2. 候选：live slots → RAM cache states（新→旧，最多 64）→ SSD（仅冷启动时）
   每个 detokenize 成文本，与 text_new 做【字符级】LCP
3. 选最长公共文本前缀的候选（best_lcp >= 256 字节才有意义）
4. 采纳候选的前 k 个 token（它们正是产生 KV 状态的原始 token）
   + 对剩余文本重新 common_tokenize，拼接成新 token 流
5. 安全校验：新流 detokenize 必须与 text_new 完全一致（语义零漂移，只对齐切痕）
6. 通过后替换 task.tokens，并重算 message_spans
```

核心代码摘抄（文本 LCP + 前缀对齐）：

```cpp
const std::string text_new = task.tokens.detokenize(ctx_tgt, true);
// ...收集 candidates（live slots / RAM / SSD 冷启动）...
const server_tokens * best = nullptr; size_t best_lcp = 0;
for (const auto * cand : candidates) {
    const std::string text_cand = cand->detokenize(ctx_tgt, true);
    size_t lcp = 0;
    while (lcp < text_cand.size() && lcp < text_new.size() && text_cand[lcp] == text_new[lcp]) lcp++;
    while (lcp > 0 && (uint8_t) text_new[lcp] >= 0x80 && (uint8_t) text_new[lcp] < 0xC0) lcp--; // 回退到 UTF-8 字符边界
    if (lcp > best_lcp) { best_lcp = lcp; best = cand; }
}
// 采纳 best 的前 k 个 token（k 个 token detokenize 字节数 <= best_lcp），尾部重 tokenize
llama_tokens merged(toks.begin(), toks.begin() + k);
merged.insert(merged.end(), tail.begin(), tail.end());   // tail = common_tokenize(text_new.substr(prefix_bytes))
// 安全网：必须 decode 回同一文本
if (healed.detokenize(ctx_tgt, true) != text_new) return; // 失败则保持原 tokens
```

三个设计细节（都是"保住命中"的取舍）：

- **SSD 候选只在冷启动参与**：live slot 的 token 流**就是** KV 状态，是最优治愈目标；SSD 是外来流，冷启动后仍让 SSD 赢会破坏与 live slot 的对齐。`candidates.empty()` 才拉 SSD。
- **64 上限 + 提前退出**：控制每请求的 detokenize 成本。
- **detokenize 一致性校验**：对齐只允许发生在"文本不变"的前提下，任何语义漂移直接回退原 tokens。

**为什么上游没有 / 上提风险**：上游同样存在此 bug（BPE 增量不变量与平台无关），但未处理——推测是收益场景不同（上游缓存非生死线）。上提风险集中在：① 每请求全量 detokenize 多个候选的 CPU 开销（长上下文被放大）；② 侵入核心路径（改写所有请求的 token 流，维护背书成本高）；③ 依赖 SSD 体系（剥离后价值缩水）。**上提评估：普适但侵入性大，留本地。**

---

## 3. C04 / C05（引擎层）— 已迁至引擎仓库

> **C04**（Vulkan / UMA 优化组 V1-V10：reads_clean 快路径、HostCached GTT、F16ACC 等，含英文 issue 素材）与 **C05**（ggml-alloc 零尺寸 view 修复）属于引擎仓库平台层，其技术详解与 rebase 重挂清单见
> [Ryzen-UMA-Vulkan-llama / CORE_MODIFICATIONS.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/CORE_MODIFICATIONS.md)。
> 产品层不涉及这两项，引擎升级时引擎仓库自行核对。

---

## 4. R01 — reasoning-budget 软注入（soft nudge）— ✅ 已删除（2026-08-19）

> 本节为技术资料存档。软注入已整体删除、完全切换上游 `--reasoning-budget` / `--reasoning-budget-message`（见 docs/UPSTREAM.md R01 台账）。以下内容保留供 rebase 学习参考。

### 4.1 与上游的关系（历史记录）

- **核心**（预算耗尽 → 强制注入结束序列/消息）：与上游 `--reasoning-budget` / `--reasoning-budget-message` **重复**，已删除本地核心、改用上游。
- **原独有价值**：`REASONING_BUDGET_SOFT_INJECT` 状态——在预算**耗尽前**（默认消费到 80%）软注入一段提示，让模型自己收尾，思考截断不唐突。硬注入是"掐断"，软注入是"提醒"。**实际使用结论：对 Qwen3.6 有效、对 Qwen3.8 效果不大**，故放弃软注入，改走上游"标签截断前注入"让模型自然收尾。

### 4.2 状态机（`common/reasoning-budget.cpp`）

```
IDLE -> COUNTING（<think> 开始）
  COUNTING: 剩余 token <= soft_threshold（= budget * (1 - soft_ratio)）且未注入过
            -> SOFT_INJECT：逐 token 强制 soft_tokens（nudge 消息）
  SOFT_INJECT 完成 -> 回到 COUNTING 继续倒计时
  COUNTING: 预算耗尽 -> FORCING（强制结束序列）-> DONE
```

核心摘抄：

```cpp
} else if (!ctx->soft_injected && !ctx->soft_tokens.empty() && ctx->remaining <= ctx->soft_threshold) {
    // soft nudge point: consumed >= soft_ratio * budget, still thinking
    ctx->state = REASONING_BUDGET_SOFT_INJECT; ctx->soft_pos = 0; ctx->end_matcher.reset();
}
case REASONING_BUDGET_SOFT_INJECT:
    forced = ctx->soft_tokens[ctx->soft_pos];
    ctx->soft_pos++;
    if (ctx->soft_pos >= ctx->soft_tokens.size()) {
        ctx->soft_injected = true;
        // nudge 完成后：预算若已耗尽 -> FORCING，否则回 COUNTING
    }
```

配套（`common/arg.cpp`）：`--reasoning-budget-soft-message` / `--reasoning-budget-soft-ratio`（默认 0.8）。effort → 预算映射在 Python 层（`ai_qwen_max/backend.py` 的 `EFFORT_THINK_PCT`），不在 C++ 补丁内。

### 4.3 上提评估（历史记录）

软注入曾作为独有薄扩展上提候选 #2，**已随 R01 切换关闭**。effort → 预算映射保留在 Python 层（`ai_qwen_max/backend.py` 的 `EFFORT_THINK_PCT` + `config.py` 的 `default_reasoning_budget_injection`，计划按模型拆分配置注入命令）。

---

## 5. 其余小项速查

| 项 | 一句话原理 | 平台相关性 | 状态 |
|---|---|---|---|
| C03 has_mtmd 语义修复 | mmproj 加载后 `has_mtmd` 变模型级标志，断言改判实际媒体存在性 | 多模态 + 缓存体系的必要修整 | 待核上游 |
| C06 qwen3_coder 解析容错 | 容忍 `</parameter>` 前缺换行；REQUIRED 时强制工具调用 | 无（模型输出格式适配） | 待核上游 |
| C08 remove_contained 迭代器修复 | 迭代中删除的 UB 修复（本地新代码自身 bug） | 无 | 随 C01 迁移 |
| C09 SEH crash logger | Windows 下 WER 静默吞进程，挂 VectoredExceptionHandler 打印崩溃栈 | Windows 通用 | 可作小 PR |
| C10 /max/shutdown | detached 线程 200ms 后调 `llama_server_terminate()`，HTTP wrap 信号式退出 | 无 | 私有，不上提（无鉴权 shutdown 安全隐患） |

---

## 6. rebase 重做总纲（如何用本文档）

当上游大版本更新导致必须 rebase / 重建本工程时，按此顺序：

1. **先重建缓存支柱**（收益最大、依赖最深）：按 §1 重建 SSD 层（迁新文件 + 4 钩子）、按 §2.1 重建生成段 checkpoint、按 §2.2 重建 BPE 治愈。这三者相互依赖（治愈依赖缓存候选，checkpoint 依赖治愈对齐），必须成套验证。
2. **平台性能**：引擎层（C04 Vulkan / C05 ggml-alloc）由引擎仓库自行核对（见其 CORE_MODIFICATIONS.md §3 rebase 重挂清单）；本工程产品层不涉及，无需处理。
3. **思考预算**：已完全使用上游 `--reasoning-budget` / `--reasoning-budget-message`（软注入已于 2026-08-19 删除），无需重建；注入命令在配置键 `default_reasoning_budget_injection` 设置。
4. **小项**：按 §5 表逐项过（多数是独立小改动，低风险）。
5. **每步验证**：跑 `scripts/bench.py` 同参对比 + 缓存命中 TTFT（~84ms 基准），确认重建未破坏缓存链路。

> 与 [UPSTREAM.md](UPSTREAM.md) 六、逐项台账配合使用：台账回答"这项现在该不该上提"，本文档回答"这项重做时该怎么写"。
