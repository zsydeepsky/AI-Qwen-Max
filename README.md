# AI-Qwen-Max

**简体中文** ｜ [English](README.en.md)

面向 **AMD Ryzen AI Max+ 395（Strix Halo / gfx1151）统一内存平台**的高能效本地 Qwen 推理服务。

一个 CLI 窗口 + OpenAI 兼容 HTTP API + 轻量 Web 界面，背后是一套为 Strix Halo 深度定制的 llama.cpp 引擎：K8V8 KV 量化、RAM→SSD 两级 prompt cache、MTP 投机解码、生成段 checkpoint 与 cache-aware retokenize。目标函数只有一个——**单位能耗下的最大推理能力（tok/s per Watt）**。

> 硬件约束：本工程不做通用化。只支持 Windows + Strix Halo（Ryzen AI Max 395/395+）+ Vulkan。其他平台请绕道 [llama.cpp](https://github.com/ggml-org/llama.cpp) 本体。

## 快速开始

```powershell
# 0) 依赖：Visual Studio（C++ 桌面开发）、Vulkan SDK、Python 3.10+、Node.js（构建内嵌 Web UI）
winget install Microsoft.VisualStudio.2022.Community KhronosGroup.VulkanSDK Python.Python.3.12 OpenJS.NodeJS.LTS

# 1) 拉取（含引擎 submodule）
git clone --recursive https://github.com/zsydeepsky/AI-Qwen-Max.git
cd AI-Qwen-Max

# 2) 构建引擎（首次约 15~30 分钟，含 npm 构建 Web UI）
.\scripts\build.ps1

# 3) 安装前端依赖
pip install -e .

# 4) 下载模型（示例）
.\scripts\download-model.ps1 -Repo unsloth/Qwen3.8-27B-GGUF -File Qwen3.8-27B-UD-Q5_K_XL.gguf -OutPath models\

# 5) 启动（pip install -e . 后 max 命令等效）
python -m ai_qwen_max            # 交互模式：选模型 → 选档位 → 对话
python -m ai_qwen_max --serve    # 纯服务模式
```

启动后：
- CLI 直接对话（启动流程：语言 → 前端端口 → 模型 → 档位 → 思考强度 → 功能选单；ESC 全局中断/返回）
- 浏览器打开 `http://127.0.0.1:8080` —— Web 聊天界面（`web/index.html` 为入口的静态目录，可扩展 html/js/css）
- OpenAI 兼容端点：`http://127.0.0.1:8080/v1/chat/completions`
- 管理 API：`http://127.0.0.1:8080/help`

## 架构

```
web 界面 / CLI ──┐
                     ├─► ai_qwen_max（FastAPI :8080）──► llama-server（动态端口子进程）
外部 OpenAI 客户端 ──┘        会话管理/反代/观测              │
                                                             ▼
                                                  vendor/llama.cpp（ryzen-uma-vulkan 分支）
                                                  Ryzen-UMA-Vulkan-llama 引擎（submodule）
```

```
.max/                        运行时实例目录（自动创建）
├── config.json              配置（模型清单/端口/缓存池/思考档位）
├── chat/<sid>/              会话：messages.json + dialogue.txt + media/
├── cache-ssd/               SSD prompt-cache 池（跨重启，TTL 24h）
└── llama-server.log
```

## 核心能力（为什么快）

| 层 | 机制 | 效果 |
|---|---|---|
| KV 量化 | K8V8（q8_0/q8_0）锚定 | KV 体积 -50%，strix-halo 基线已消除量化 KV 的 prefill 惩罚 |
| 前缀缓存 | RAM 池（48GB）→ SSD 池（64GB）两级，驱逐落盘 + 跨重启恢复 | 重启后 warm TTFT 6.2s → 2.0s |
| 投机解码 | Qwen 原生 MTP（nextn 头）draft-mtp | 每 token 能耗最低路径 |
| 生成段 checkpoint | 解码期每 256 token 滚动快照 + 生成结束终拍 | 中断/多轮对话不再全量重算 prefill |
| BPE 治愈 | cache-aware retokenize（文本级 LCP + detokenize 校验） | 贪心生成与重渲染的 token 边界分歧不再击穿缓存 |
| UMA 内存 | HostCached GTT 映射 + reads_clean 快路径 | SSM checkpoint 快照 1.4s → 40ms（45×） |
| 注意力 | FA f16 累加（QWENMAX_FA_F16ACC） | prefill +9% |

引擎定制明细：[docs/ENGINE_PATCHES.md](docs/ENGINE_PATCHES.md) ｜ 设计文档：[docs/DESIGN.md](docs/DESIGN.md)

## 引擎与致谢

推理引擎 [vendor/llama.cpp](vendor/llama.cpp) 是 git submodule，基于：

- **[Nathanw1014/llama.cpp](https://github.com/Nathanw1014/llama.cpp)** 的 `strix-halo-vulkan` 分支（Strix Halo Vulkan 优化，本项目定制的基线）
- 上游 [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp)
- 相关项目 [Nathanw1014/strix-halo-llamacpp](https://github.com/Nathanw1014/strix-halo-llamacpp)

在 `ryzen-uma-vulkan` 分支上构建本项目的产品层（SSD 缓存 / 生成段 checkpoint / retokenize / qwen3_coder 容错等以 `patches/qwenmax-server-layer.patch` 在构建时叠加；引擎仓库本身只含 Ryzen/UMA/Vulkan 平台优化，可独立发行）。保留完整上游历史，便于跟进上游更新。全部代码遵循 MIT License。

### 同步上游更新

```powershell
cd vendor\llama.cpp
git remote add upstream https://github.com/Nathanw1014/llama.cpp.git   # 首次
git fetch upstream strix-halo-vulkan
git merge upstream/strix-halo-vulkan        # 解决冲突后重新 .\scripts\build.ps1
```

## 工具

| 文件 | 用途 |
|---|---|
| `scripts/build.ps1` | 编译引擎（MSVC + Vulkan-only 裁剪构建） |
| `scripts/run.ps1` | 引擎直启（调试用；日常走 `max` / `python -m ai_qwen_max`） |
| `scripts/build_exe.ps1` | PyInstaller 绿色版打包（`dist/max/max.exe`） |
| `scripts/download-model.ps1` | HF 模型下载 |
| `scripts/bench.py` | 基准：TTFT / decode / 多会话缓存隔离 / 能耗（tok/s/W） |

## License

MIT（[LICENSE](LICENSE)）。引擎 submodule 各自遵循其原始 License。
