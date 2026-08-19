# 产品层上游协作台账（AI-Qwen-Max）

本文档管理 **AI-Qwen-Max 产品层定制**（`patches/qwenmax-server-layer.patch` 叠加在引擎上的 server/chat 层改动）与上游 llama.cpp 的关系。
**引擎层**（ggml-vulkan / llama-graph / ggml-alloc 的平台定制）的上游协作见引擎仓库 [Ryzen-UMA-Vulkan-llama](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama) 的 [UPSTREAM.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/UPSTREAM.md)；其技术详解见该仓库 [CORE_MODIFICATIONS.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/CORE_MODIFICATIONS.md)。

本文档定义产品层开发原则、上提候选清单与 rebase 操作要点。补丁清单见 [ENGINE_PATCHES.md](ENGINE_PATCHES.md)；各修改的原理详解见 [CORE_MODIFICATIONS.md](CORE_MODIFICATIONS.md)。

## 一、下游私有功能开发原则（2026-08-19 定稿）

目标：对随时变化的上游保持**轻量合并债**——合并成本 = O(钩子数量)，而非 O(定制面积)。

1. **重叠 → 用上游**：与上游功能大体重叠/类似时，直接采用上游实现，不自己造。
   已确认案例：reasoning-budget 文本注入（上游 `--reasoning-budget` + `--reasoning-budget-message` 已实现，LM Studio 用的就是它）→ 本地软注入已删、完全切上游（R01）。
2. **独有 → 扩展新文件**：上游完全没有的功能，写成"扩展"形态——核心逻辑放新文件，
   仅用少数简单 API 钩子接入上游，禁止把大段代码塞进上游文件。
   案例：SSD 两级缓存（上游仅 RAM 层 `--cache-ram`；磁盘持久化只有未合并的 PR #26408 / issue #20697）。
3. **私有端点 → wrap**：私有 HTTP 端点写在新文件里，内部只 wrap 上游已有能力
   （如 `POST /max/shutdown` 内部走上游信号式 shutdown 路径），不新增上游没有的执行链路。
4. **值得上提 → 记录并推动**：凡有普适价值的改动（bug 修复、性能优化、通用功能）登记到第二节清单，
   尽早推送上游；**上提合并后，本地删除该实现、改用上游版本**，从源头消除合并债。

配套规则：
- 扩展文件头标注 `qwenmax extension — do not merge upstream`，并注明挂接点。
- 钩子点选择上游稳定、低频改动的接口；行为开关用 `RYZENUMA_*` / `GGML_VK_*` env 变量。
- 引擎升级（rebase）后按第三节"钩子清单"逐点核对（通常每点 ≤10 分钟）。
- 已放弃/已被上游取代的本地实现，登记在第四节防止复活。

## 二、上提候选清单（产品层）

状态：🔵 未开始 ｜ 🟡 整理中 ｜ 🟢 已上提/PR 中 ｜ ⚫ 已合并（本地已删）

| # | 内容 | 位置 | 上游现状 | 建议 | 状态 |
|---|---|---|---|---|---|
| 1 | SSD 两级 prompt 缓存（`--cache-ssd`） | `tools/server/` + `common/` | 上游仅 `--cache-ram`；PR #26408（`--cache-disk`，UMA offload）已关闭未合并；issue #20697 开放 | 大件；对齐 #26408 设计后上提，或至少贡献设计 | 🟡 |
| 2 | 生成段 checkpoint + BPE 治愈（`retokenize_with_cache`） | `server-context/task` | 上游有 `--ctx-checkpoints`（SWA/循环模型） | 先与上游 ctx-checkpoints 对照，删重叠、留独有 | 🟡 |
| 3 | `POST /max/shutdown`（HTTP 触发干净退出） | `tools/server/server.cpp` | 上游仅信号式 shutdown | **判定私有、不上提**（无鉴权 shutdown 是公网攻击面）；仅本地 loopback 使用 | 🔵 |
| 4 | `has_mtmd` 语义修复（per-prompt 媒体检查） | server-common/task/context | 上游两层面均为 `!has_mtmd`（未吸收） | 独有保留；断言部分剥离 SSD 依赖后可作小 PR | 🔵 |
| 5 | qwen3_coder 工具调用解析容错 | `common/chat.cpp` | 官方 master 已重构通用 parser；strix-halo-vulkan 仍旧 parser | 待 strix-halo 同步 master 后重核 | 🔵 |
| 6 | 辅助请求 LCP 抢占条件修正 | `server-context.cpp` | 待核 | 核对上游后上提 | 🔵 |
| 7 | `remove_contained` 迭代器失效修复 | `server-task.cpp` | 上游无此方法 | **无上提价值**（本地新增类自身 bug），随 C01 处理 | 🔵 |
| 8 | Windows SEH crash logger | `server-context.cpp` | 上游无 VEH 机制 | Windows 通用，可作小 PR（平台无关方案偏好，价值中等） | 🔵 |

## 三、钩子清单（rebase 后逐点核对）

| 钩子点 | 本地功能 | 说明 |
|---|---|---|
| `server_prompt_cache::alloc/update/load/destroy` | SSD 层 save/load/teardown | 上游改动此三类时优先检查（见 ENGINE_PATCHES.md 注意事项）|
| `llama_server::terminate` / 信号 shutdown 路径 | `/max/shutdown` wrap | 钩子应只剩路由注册 |
| `process_single_task` / decode 路径 | 生成段 checkpoint（`maybe_gen_checkpoint`/`maybe_final_checkpoint`） | 上游重构 server 任务循环时重点核对 |
| `server_slot` 结构 | `n_decoded`/`n_decoded_ckpt_last` 字段 | 上游改 slot 字段时核对（曾与上游 `n_remaining()` 方法同名冲突） |
| `server_tokens::get_tokens/set_token` | has_mtmd 断言修复（C03） | 上游断言改动时核对 |

## 四、已确认的重复（本地实现应删除，改用上游）

| 功能 | 上游对应 | 决策 |
|---|---|---|
| reasoning-budget 软注入（soft nudge） | `--reasoning-budget` / `--reasoning-budget-message` | ✅ 已删除（2026-08-19），完全切上游；effort→预算映射留 Python 层（R02） |

## 五、审计备忘

- 对比基准：引擎 fork `origin/strix-halo-vulkan` + 官方 master。上提前需重新核对。
- C03/C06 审计（2026-08-19）：C03（has_mtmd 断言）上游两层面均未吸收（仍是 `!has_mtmd`）→ 独有保留；C06 关键发现——官方 master 已删除 `common_chat_params_init_qwen3_coder`（重构为通用机制），strix-halo-vulkan 仍是旧 parser → 保留待上游同步后重核。详见第六节。
- C08/C09/C10 审计（2026-08-19）：三层均未吸收——C08（remove_contained）上游无此方法，本地自身 bug 修复，随 C01，**无上提价值**；C09（SEH crash logger）上游无 VEH 机制，Windows 通用，可作小 PR；C10（/max/shutdown）上游无任何 shutdown 端点（仅 `llama_server_terminate` 函数）——**判定私有、不上提**：HTTP 无鉴权暴露 shutdown 是巨大安全隐患（公网可一键杀服务），本地 loopback 私有用法成立、上提即炸。详见第六节。
- C07/C11 已删除（2026-08-19）：主会话守护（`get_available_slot` cache_prompt 条件 + 标题生成 `cache_prompt=false`）判定"现在用不上"→ 代码回退 + 台账清除。
- **上提评估三维（经验教训，2026-08-19）**：候选上提的每一项必须同时满足——① 改动小（或拆解后小）；② **动机普适**（该动机在通用多用户/多设备上游同样成立，而不是把单用户场景的客制化概念包装成普适外表）；③ **不扩大攻击面**（新 HTTP 端点/新默认行为不得在无鉴权下暴露能力）。任一维度不满足即判定为私有，rebase 保留即可，不上提。评估时先问"上游维护者会问为什么"，答不出普适理由即不上提。

## 六、核心修改逐项台账（供逐项核对 / rebase 后重做）

> 依据：`patches/qwenmax-server-layer.patch`（产品层全部差异，见 ENGINE_PATCHES.md）。
> 上游对照基准：fork `origin/strix-halo-vulkan`；状态栏按四原则标注：`重叠`（用上游）/ `独有`（扩展形态）/ `待核`（需逐版核对上游）。
> 引擎层（C04 Vulkan / C05 ggml-alloc）台账已迁至引擎仓库，见其 [UPSTREAM.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/UPSTREAM.md) 与 [CORE_MODIFICATIONS.md](https://github.com/zsydeepsky/Ryzen-UMA-Vulkan-llama/blob/ryzen-uma-vulkan/CORE_MODIFICATIONS.md)。

### C01 SSD 两级 prompt 缓存（`--cache-ssd`）— 产品层最大定制，散落上游文件

**修改文件与方法：**
- `common/arg.cpp` — 新增 3 个参数：`--cache-ssd N`（MiB，env `LLAMA_ARG_CACHE_SSD`，0 禁用 / -1 不限）、`--cache-ssd-ttl-hours N`、`--cache-ssd-path DIR`
- `common/common.h` — `common_params` 新增 `cache_ssd_mib / cache_ssd_ttl_hours / cache_ssd_path`
- `tools/server/server-task.h` — 新增 `class server_prompt_cache_ssd`；`server_prompt_cache` 新增 `ssd` 指针成员；entry 结构（纯 token id，供 LCP）
- `tools/server/server-task.cpp`（大量新增）：
  - `server_prompt_cache_ssd` 构造/析构、`make_fingerprint`（模型指纹哈希）、`init`（读 index.bin，指纹/格式不符则整目录清空）、`save_index`（tmp+rename 原子写）、`remove_entry`、`cleanup_expired`（TTL 惰性清理）、`make_room`（按容量限制 LRU 淘汰）、`save_state`（tokens+checkpoints+KV data_main/data_drft 序列化，原子写）、`load_state`（LCP 匹配 → 恢复 tokens/checkpoints/KV，经 `llama_state_seq_set_data_ext`）、`remove_contained`、`contains / size / n_tokens / n_entries / entries_view`
  - `server_prompt_cache::load(...)` — 增加 `empty_base` 参数（空槽/禁用缓存时跳过 RAM LCP）；RAM 命中不足时回退 SSD
  - `server_prompt_cache::update()` — 挂 eviction 钩子（已含→跳过；新内容→`remove_contained`；容量满→最老落盘）
  - `server_prompt_cache::evict_to_ssd(target_mib)` — 低内存主动落盘（只存不删）
  - `server_prompt_cache::flush_to_ssd()` — 销毁时全量落盘
- `tools/server/server-context.cpp`：
  - 构造器 — SSD 初始化：模型指纹（desc/n_layer/n_embd/n_head/模型路径与大小/build + cache_type_k/v），要求先启用 `--cache-ram`；`server_context_impl::cache_stats_json()`；`server_context_impl::evict_ram_to_ssd(target_mib)`；`~server_context_impl`（destroy）— 保存 slot prompt + `flush_to_ssd()`
- `tools/server/server-context.h` — handler `get_cache_stats` / `post_cache_evict`；`ctx_server` 由 const 改非 const
- `tools/server/server.cpp` — 注册 `GET /cache/stats`、`POST /cache/evict`
- SSD 核心类已独立为 `tools/server/ssd-prompt-cache.h/.cpp`（新文件扩展，merge 时永不冲突）

**原因与意图：** Strix Halo UMA 下 RAM 与模型/KV 抢内存（默认 `--cache-ram 48GB` 无法全常驻）；SSD 层把冷 prompt 落盘持久化，跨进程重启仍命中，规避冷 TTFT。
**上游关系：** 独有。上游只有 RAM 层（`--cache-ram`）；磁盘缓存上游 PR #26408 已关闭、issue #20697 开放，方向一致但未合并。
**核对/重做要点：** 核心类已在独立文件（`ssd-prompt-cache.h/.cpp`），上游文件只留 `ssd` 指针 + 4 个钩子（load/update/evict/flush），合并债 O(钩子)。上提候选 #1。

### C02 生成段 checkpoint（滚动/收尾）+ BPE 治愈（`retokenize_with_cache`）

**修改文件与方法：**
- `tools/server/server-context.cpp`：
  - `maybe_gen_checkpoint(slot)` — 生成途中按 `QWENMAX_GEN_CKPT_STEP` 滚动存 checkpoint（两条 decode 路径都调用）；`maybe_final_checkpoint(slot)` — 生成结束收尾存
  - `create_checkpoint` — checkpoint 内容与最新一份相同时跳过（以 id_task 归属标记避免 min-step 重快照；hybrid 快照约 150MiB 成本）
  - `server_slot::n_decoded_ckpt_last` — 新字段
  - `server_context_impl::retokenize_with_cache(task)` — 文本级 LCP 治愈：新请求与缓存候选做 **detokenize 后文本** 前缀比对 → 前缀 token 复用 + 尾部重新 `common_tokenize` → 再 detokenize 校验一致性；失败回退原 tokens
- env：`QWENMAX_GEN_CKPT_STEP` / `QWENMAX_GEN_CKPT_KEEP`
- `tools/server/server-task.h` — `task_params::message_delimiters`；decode 后保留 delimiters，供 `retokenize_with_cache` 重算 `message_spans`

**原因与意图：** ① 同 slot 多次生成时，从生成起点 checkpoint 恢复，长回复局部编辑不再全量重算 KV；② 缓存复用靠 token 级 LCP，但 `common_tokenize` 倾向多字符合并，同一文本的两次 tokenize 可能不同 → 命中失效；用文本级 LCP 治愈。
**上游关系：** 部分重叠。上游有 `--ctx-checkpoints`/`--checkpoint-min-step`（面向 SWA/循环模型上下文）；"生成段滚动 checkpoint" 与 "文本级 BPE 治愈" 为独有。上提候选 #2。
**核对/重做要点：** 先对照上游 `--ctx-checkpoints` 实现删重叠；独有部分重写时优先放新文件，挂 `create_checkpoint`/decode 路径钩子。

### C03 has_mtmd 语义修复（断言级）+ find_next_media_chunk 守卫 — 2026-08-19 已审计

**修改文件与方法：**
- `tools/server/server-common.cpp` — `server_tokens::get_tokens()` / `server_tokens::set_token()`：断言从 `!has_mtmd` 改为 `map_idx_to_media.empty()`（实际媒体存在性）
- `tools/server/server-context.cpp` — cache 复用 / raw token 追加 / checkpoint / SSD save/load / `retokenize_with_cache` 入口：一律改用 `find_next_media_chunk(0)` 检测实际媒体
- `tools/server/server-task.cpp` — SSD 侧 `save_state/load_state/remove_contained/contains/ssd_common_prefix` 同款守卫

**原因与意图：** mmproj 加载后 `has_mtmd` 变成模型级能力标志，不再代表"当前 token 流是否有媒体"；SSD 恢复的纯文本流会被断言炸掉。改为按实际媒体判定。
**审计结论（2026-08-19）：** 上游两层面（strix-halo-vulkan、官方 master）的 `get_tokens`/`set_token` 断言**仍是 `GGML_ASSERT(!has_mtmd)`**，未吸收；`find_next_media_chunk` 上游仍存在且签名一致。
**核对/重做要点：** 断言部分（server-common.cpp）小且独立，可作小 PR 候选（需剥离 SSD 依赖）；守卫部分大多挂在 SSD/checkpoint/治愈路径上，随 C01/C02 迁移。上提候选 #4。

### C06 qwen3_coder 工具调用解析容错 — 2026-08-19 已审计

**修改文件与方法：**
- `common/chat.cpp` — qwen3_coder parser：`</parameter>` 前容忍缺换行（`"\n</parameter>\n"` 与 `"</parameter>\n"` 双分支）；tool_choice=REQUIRED 时强制生成工具调用序列（含 `<think>` 可选段，完全禁止自然语言前言）

**原因与意图：** Qwen3-Coder 输出格式漂移，上游 parser 过严 → 工具调用解析失败 / REQUIRED 时模型编造答案不调用工具。
**审计结论（2026-08-19）：** 关键发现——**官方 master 已删除 `common_chat_params_init_qwen3_coder`**（XML 工具解析重构为通用机制，改用 `</param>` + CDATA，不再要求换行前缀）；strix-halo-vulkan 仍是旧 parser（严格 `\n</parameter>\n`，未覆盖我们的修复）。→ 修复点 1（容忍缺换行）在新机制下可能天然解决。
**核对/重做要点：** 对 strix-halo-vulkan 保留（挂载点仍在）；**待 strix-halo-vulkan 同步官方 master 后重核**——届时确认新 parser 是否天然容忍缺换行、REQUIRED 是否仍允许前言，再决定删/迁。上提候选 #5（暂缓，等上游同步）。

### C08 `remove_contained` 迭代器失效修复

**修改文件与方法：**
- `tools/server/server-task.cpp` — `server_prompt_cache_ssd::remove_contained`：先拷贝 iterator 再 `remove_entry`，避免迭代中删除导致 UB

**原因与意图：** 本地新增代码自身的 bug 修复（迭代器失效）。
**上游关系：** 独有（上游无此方法）。
**审计结论（2026-08-19）：** 上游两层面均无 `remove_contained`（`server_prompt_cache_ssd` 为本地类）→ 本地代码自身 bug 修复，独有保留。**无上提价值**，随 C01 处理即可。
**核对/重做要点：** 若 C01 重构时整体迁入新文件，随迁即可。

### C09 Windows SEH crash logger

**修改文件与方法：**
- `tools/server/server-context.cpp` — 新增 `qwenmax_crash_log`（`AddVectoredExceptionHandler`）：记录异常码/地址/module/RVA/32 帧栈回溯到 stderr + `OutputDebugStringA`

**原因与意图：** 无调试器运行，WER 静默吞进程，无法定位现场崩溃（access violation / stack overflow 等）。
**上游关系：** 独有；Windows 场景有普适价值。
**审计结论（2026-08-19）：** 上游两层面均无 `AddVectoredExceptionHandler`/崩溃日志机制 → 独有保留。Windows 通用价值成立，但上游偏好平台无关方案，上提价值中等。
**核对/重做要点：** 独立小功能；可考虑上提小 PR（放新文件 wrap `AddVectoredExceptionHandler`）。

### C10 `POST /max/shutdown`

**修改文件与方法：**
- `tools/server/server.cpp` — 新增 `handle_max_shutdown`：detached 线程 200ms 后调 `llama_server_terminate()`（信号式 shutdown 的 HTTP wrap）；注册 `POST /max/shutdown`

**原因与意图：** 管理端需 HTTP 触发干净退出；外部无法发 Ctrl+C 信号；200ms 延迟先返回响应再退出。
**上游关系：** 独有（wrap 上游已有能力，无新增执行链路）。
**审计结论（2026-08-19）：** 上游两层面均无 shutdown HTTP 端点 → **判定为私有、不上提**。HTTP server 默认无鉴权，shutdown 端点公网可达即一键杀服务（DoS + 管理面暴露），巨大安全隐患。仅限本机 supervisor（max.py）经 loopback 私有调用。
**核对/重做要点：** 保留为私有端点（仅监听 loopback）；若未来上提，必须先解决鉴权或绑定回环地址的设计，否则不满足上提评估三维（见第五节）。

### R01 reasoning-budget 软注入（soft nudge）— ✅ 已删除，完全切上游

**切换决策（2026-08-19，用户确认）：** 实际使用发现软注入对 Qwen3.6 有效、对 Qwen3.8 效果不大；改用上游"预算耗尽时在 `</think>` 前注入文本"（`--reasoning-budget-message`），让模型以为自己是自然收尾，效果更好。注入命令后续按模型分别配置（Python 层 `config.py` 的 `default_reasoning_budget_injection` 键，`.max/config.json` 可覆盖）。
**执行结果：** 引擎侧 7 文件恢复上游 + 4 文件删软注入参数；Python 侧 backend.py 改用 `--reasoning-budget-message`、llm.py `think_budget_kwargs` 改用 `reasoning_budget_message`。构建 + 冒烟验证通过。

### R02 effort→预算绑定（Python 层，不在 C++ 补丁内）

**修改文件与方法：**
- `ai_qwen_max/config.py` — 默认 `"reasoning_effort": "low"`；注释档位 max_output 百分比 3%/10%/30%
- `ai_qwen_max/backend.py` — `EFFORT_THINK_PCT`：effort → max_output 预算百分比映射（第 83 行）
- `ai_qwen_max/llm.py` — 模板支持 `reasoning_effort` 时经 `chat_template_kwargs` 绑定；不支持时降级 system-message 指令

**原因与意图：** 用户级 effort 档位（off/low/medium/xHigh）映射思考预算，与 CLI 7 档上下文档位同层。
**上游关系：** 无关（前端 Python 层）。
**核对/重做要点：** rebase 不影响；但若上游 `--reasoning-budget-message` 语义变化需同步映射。注入文本统一走配置键 `default_reasoning_budget_injection`（计划按模型拆分配置）。
