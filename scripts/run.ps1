#Requires -Version 5.1
<#
  AI-Qwen-Max 引擎直启脚本 —— 绕过 Python 前端直接调起 llama-server（引擎调试用）。
  日常使用请运行 max（CLI + HTTP 前端，自动管理本脚本的全部参数）。

  用法：
    .\scripts\run.ps1                          # 默认模型
    .\scripts\run.ps1 -Model "D:\m\xxx.gguf"   # 指定模型（也可作位置参数）
    .\scripts\run.ps1 -NoMtp                   # A/B：临时关闭 MTP 投机解码
#>
param(
    [Parameter(Position = 0)][string]$Model = '',
    [switch]$NoMtp,     # A/B：关闭 MTP 投机解码（对照基线）
    [int]$Lv = 3,       # 日志级别：4 = slot 级 trace（缓存/checkpoint 决策轨迹）
    [string]$LogFile = ''  # 诊断：日志落盘（-Lv 4 时强烈建议，如 logs\trace.log）
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent   # 仓库根（本脚本在 scripts\ 下）
$server = Join-Path $root 'build\bin\llama-server.exe'
if (-not (Test-Path $server)) { throw "未找到 $server —— 请先运行 .\scripts\build.ps1" }

if (-not $Model) {
    throw "请指定模型：.\scripts\run.ps1 -Model <模型.gguf 的完整路径>"
}
if (-not (Test-Path $Model)) { throw "未找到模型：$Model" }
Write-Host "模型：$Model"

$threads = 16    # Zen5 全核（功耗 profile 已废弃：不修改系统电源计划，见 docs/DESIGN.md）
$ubatch  = 4096

# ---- 参数与 ai_qwen_max/backend.py 保持一致 ----
$flags = @(
    '--model', $Model,
    '--host', '127.0.0.1',
    '--port', '8080',
    '--ctx-size', '49152',          # 16384 档 × 3 slot
    '--n-gpu-layers', '999',
    '--threads', "$threads",
    '--ubatch-size', "$ubatch",
    '--flash-attn', 'on',
    # K8V8 锚定（2026-08-16 定稿）：strix-halo 基线已消除量化 KV 的 prefill 惩罚；
    # 单一 KV 布局供 SSD/RAM 缓存复用；指纹含 cache_type_k/v。
    '--cache-type-k', 'q8_0',
    '--cache-type-v', 'q8_0',
    '--parallel', '3',              # 2 路 API + 1 路 CLI/Web
    '--slot-prompt-similarity', '0.5',
    '--ctx-checkpoints', '128',     # SSM 状态快照数（混合架构回滚依赖 checkpoint）
    '--verbosity', "$Lv",
    # 第 1 层：前缀缓存（RAM 池 → SSD 持久层，跨重启恢复）
    '--cache-ram', '49152',
    '--cache-reuse', '256',
    '--cache-ssd', '65536',
    '--cache-ssd-path', (Join-Path $root '.max\cache-ssd'),
    '--cache-ssd-ttl-hours', '24'
)

# ---- 第 2 层：投机解码（模型需内嵌 nextn 头；A3B 等无 MTP 模型自动跳过需手动 -NoMtp） ----
if (-not $NoMtp) {
    $flags += @('--spec-type', 'draft-mtp')
    Write-Host 'MTP：已启用（nextn 头）'
}

if ($LogFile) {
    $logDir = Split-Path $LogFile -Parent
    if ($logDir -and -not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
    $flags += @('--log-file', $LogFile)
    Write-Host "日志：$LogFile"
}

# ---- 引擎定制开关（详见 docs/ENGINE_PATCHES.md）----
# HostCached GTT 映射：SSM checkpoint 快照 1.4s → 32-40ms（45×），warm TTFT 6.2→2.0s
$env:GGML_VK_PREFER_HOST_MEMORY = '1'
# fp16 注意力累加（上游保守强制 F32）：prefill +9%
$env:RYZENUMA_FA_F16ACC = '1'
# l-tile GEMM：驱动版本相关！2026-07 驱动 (32.0.31035.1003) 上回退 -38%，保持 '0'；
# 驱动更新后用 llama-bench -p 2048 重新 A/B
$env:GGML_VK_AMD_L_TILES = '0'

Write-Host '引擎直起 -> http://127.0.0.1:8080/v1/chat/completions  （Ctrl+C 退出）'
& $server @flags
