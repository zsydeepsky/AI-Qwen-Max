# AI-Qwen-Max 设计文档

**简体中文** ｜ [English](DESIGN.en.md)

本文档是项目的设计基线，记录架构决策与已验证的技术结论（含被否决项）。

## 1. 目标与非目标

**目标函数**：单位能耗下的最大推理能力（tok/s per Watt），在 Strix Halo 统一内存平台上运行 Qwen 家族 GGUF 模型。

**非目标**（明确放弃）：
- 多硬件/多平台支持（只做 Windows + gfx1151 + Vulkan）
- GUI 客户端（CLI + 轻量 web/ 静态页足够）
- 通用量化方案（TurboQuant TQ4 已验证并否决）
- 修改系统电源计划（功耗 profile 已验证无价值并移除；只做进程内计算效率优化）

## 2. 三层架构

```
L3  用户入口     CLI（cli.py）/ web/ 静态界面（index.html 入口，GET / 由前端直接返回，可扩展 html/js/css）
L2  服务前端     ai_qwen_max 包（FastAPI :8080）
                 ├─ 会话管理（.max/chat/<sid>/，原子写）
                 ├─ OpenAI 兼容反代（/v1/*，并发闸，X-Conversation-Id 落盘）
                 ├─ Max 管理 API（/model/load /chat/* /cache/* /status ...）
                 └─ 观测流（/api/events SSE，环形缓冲 300 条）
L1  推理引擎     llama-server（:8081 子进程，vendor/llama.cpp ryzen-uma-vulkan 分支）
                 K8V8 / RAM+SSD 两级 prompt cache / MTP / checkpoint / retokenize
```

前端与引擎之间只有 HTTP（OpenAI 兼容 + 少量扩展端点），引擎可独立直启调试（scripts/run.ps1）。

## 3. 关键设计决策（定稿）

### 3.1 KV 量化：K8V8 锚定
- K=q8_0、V=q8_0。strix-halo 基线（baf0025）消除了量化 KV 的 coopmat1 FA re-dequant 惩罚，量化 KV 不再损失 prefill 速度。
- **TQ4（TurboQuant V 侧 4.125bpv）已否决**：decode 仅 +2%，但实测导致模型拒绝工具调用（行为级降智）。教训：量化验收必须做行为级测试（工具调用/长上下文），短对话贪心冒烟不够。
- 单一 KV 布局是 SSD/RAM 缓存复用的前提；缓存指纹包含 cache_type_k/v。

### 3.2 缓存：RAM → SSD → 丢弃 三级
- RAM 池（默认 48GB，`--cache-ram`）：多会话 prompt cache，LRU + 相似度接管。
- SSD 池（默认 64GB，`--cache-ssd`）：RAM 淘汰/引擎退出时落盘，跨重启恢复；TTL 默认 24h；索引原子写（tmp+rename）。
- 指纹校验：模型描述/层数/embd/heads/路径/大小/build 版本/KV 类型任一不匹配 → 整池清空。
- 低内存主动驱逐：`POST /cache/evict?ram_target_mib=N`（persist 不 drop）。

### 3.3 生成段 checkpoint + BPE 治愈（缓存命中的两大保镖）
- 混合架构（SSM+attention）回滚依赖 checkpoint 而非 KV shift。上游只在 prompt 期建 checkpoint → 上一轮回复在下轮被整体重算。定制：停止路径统一终拍——正常收尾（decode 循环）与用户中断（CANCEL）都在释放 slot 前存一张覆盖 prompt+生成内容的快照；无解码期滚动快照（省 ~0.4% decode 吞吐）。
- 贪心生成逐 token 输出与重渲染整段 tokenize 的 BPE 边界分歧会击穿 token 级 LCP。定制：retokenize_with_cache（文本级 LCP + detokenize 往返校验），候选池 = 活跃 slot + RAM states（SSD 仅冷启动时参与）。

### 3.4 投机解码：DFlash2（独立草稿模型，取代 MTP）
- 曾用 MTP（模型内嵌 nextn 头，`--spec-type draft-mtp`）：Qwen3.8 的 MTP 层绑定 xHigh 思考，改动 reasoning effort 后接受率雪崩，不仅无增益还拖慢输出 —— 已整体移除。
- 现用 `--spec-type draft-dflash --spec-draft-model <draft.gguf>`：草稿模型路径由每个模型对象的 `DFlash2_draft_model` 字段配置（`config.models` 对象列表），未配置则关闭投机解码。
- 可选调优字段（缺省不传，用引擎默认）：`spec_n_max` 草稿 token 上限（默认 3，DFlash2 上限=block size 8）。`/model/load` 每次重读 config，改字段后热换即时生效。注：DFlash 的 `--spec-draft-conf-min` 本引擎未实现（仅文档提及），暂不暴露。

### 3.5 UMA 内存（Strix Halo 关键修复）
- AMD Windows 驱动的 HostVisible 非 HostCached 内存映射为 write-combined，CPU 读 ~100MB/s，SSM checkpoint 快照（150MiB）耗时 1.4s。
- 定制：prefer_host_memory 默认强制 true（HostCached GTT，type 0xe）+ reads_clean 读快路径 → 快照 40ms（45×）。`GGML_VK_PREFER_HOST_MEMORY=0` 可回退对照。

### 3.6 上下文档位
- `CTX_CHOICES = [4096, 16384, 65536, 262144]`（per-slot），`--parallel 3`（2 API + 1 CLI/Web），引擎总 ctx = 档位 × 3。
- 切档/切模型 = 重启引擎子进程（ctx 是加载期参数）；跨档缓存兼容性由实际 token 数与指纹校验保证。

### 3.7 服务化细节
- 并发闸：POST /v1/* 过 Semaphore(2)，与 slot 预算匹配；`GET /queue` 观测。
- 辅助请求（标题生成等）必须 `cache_prompt: false`，防 LCP 相似度抢占活跃会话的 slot。
- 流式透传：反代不重组 SSE，旁路解析 delta 供观测与落盘。
- 会话落盘：X-Conversation-Id → `.max/chat/<cid>/`，全量历史 merge（尾部匹配增量追加，不匹配整体替换）；原子写。
- 优雅退出：`POST /max/shutdown`（引擎）→ 与 Ctrl+C 相同退出路径（SSD 落盘）；Windows 跨进程信号不可靠，一律走 HTTP。

## 4. Python 包结构

```
ai_qwen_max/
├── __main__.py   入口：argparse + 组装 + uvicorn(后台线程) + CLI 前台
├── config.py     Config（.max/config.json，原子写，默认值即生产基线）
├── gguf.py       GGUF 头解析（模板/多模态/输出上限探测，纯标准库）
├── backend.py    Backend：进程生命周期 / 参数拼装 / ready 探测 / 优雅退出
├── store.py      SessionStore/Session：会话持久化（原子写 + dialogue 回放 + media）
├── events.py     ApiEvents：/api/events 环形缓冲 + SSE 差量推送
├── server.py     FastAPI：反代 + Max API + 观测 + web/ 静态服务（界面契约在此）
├── llm.py        LLM：CLI 轨流式客户端（SSE 解析 / reasoning 处理 / 中断）
└── cli.py        Cli：语言/模型/档位/思考强度选择 → 功能选单（对话/删历史/API 日志）/ ESC 全局后退 / 载入 spinner / 标题栏状态机
```

实现要点（同类集成的已知坑，均已在本版处理）：
1. 关思考必须显式注入 `enable_thinking:false`（服务端 setdefault 基底会覆盖缺省）
2. 消息规整时完整保留 tool_calls/name 字段（OpenAI 客户端依赖它们做工具调用）
3. 会话/配置文件一律 tmp+rename 原子写（进程随时可能被终止）
4. stream 流型探测用 JSON 解析，不做字节匹配（SSE 分片边界不稳定）
5. 反代不做前置 healthy() 探测（每请求 +3s 延迟），直接转发、失败即 502
6. 会话列表不做全量 tokenize（最坏每会话 90s），缓存状态走 /cache/stats

## 5. 构建/发布

- `scripts/build.ps1`：NMake + Vulkan-only 裁剪构建（GGML_NATIVE，关闭全部无关后端），Web UI 经 npm 构建后 C 字节数组嵌入 llama-server.exe。
- `scripts/build_exe.ps1`：PyInstaller onedir 绿色版（`dist/max/max.exe`），入口 stub 打包时生成，收集 9 个引擎 DLL/EXE。
- 引擎源码 = submodule `vendor/llama.cpp`（ryzen-uma-vulkan 分支 = 上游 strix-halo-vulkan 基线 + Vulkan/UMA 平台优化；产品层以 `patches/qwenmax-server-layer.patch` 构建时叠加）。

## 6. 已否决技术清单（勿复入）

| 技术 | 结论 | 原因 |
|---|---|---|
| TurboQuant TQ4 | ❌ | 行为级降智（拒用工具），decode 仅 +2% |
| 功耗 profile | ❌ | 修改电源计划无收益，进程内参数已足够 |
| SSD 缓存无损压缩 | ❌ | q8_0 残差熵近满，deflate 仅省 5-6% |
| MTP 内嵌头（Qwen3.8） | ❌ | 绑定 xHigh 思考，改 effort 后接受率雪崩反而拖慢输出；已由 DFlash2 独立草稿模型取代 |
| prefill row-split 协同 | ❌ | 层数指派未达收益预期 |
| l-tile GEMM | ⏸ | 驱动版本相关，当前驱动 -38% 保持关闭（`GGML_VK_AMD_L_TILES=0`），驱动更新后重测 |
