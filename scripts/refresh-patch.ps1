#Requires -Version 5.1
<#
  重新生成产品层补丁（引擎升级后使用）

  流程：引擎升级到新基线（fetch + checkout 新 commit）→ 在 vendor/llama.cpp 里
  手工重做/修复产品层定制（apply 旧补丁、解决冲突、恢复 C01/C02/C03/C06/C09/C10
  功能）→ 运行本脚本，把当前工作区相对上游基线的产品层差异重新导出为
  patches/qwenmax-server-layer.patch。

  引擎侧的基线引用：origin/strix-halo-vulkan（Nathanw1014/llama.cpp 的
  strix-halo-vulkan 分支）。生成前请确认该引用是最新的：
    git -C vendor/llama.cpp fetch origin
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$llamaDir = Join-Path $root 'vendor\llama.cpp'
$patchPath = Join-Path $root 'patches\qwenmax-server-layer.patch'

if (-not (Test-Path (Join-Path $llamaDir '.git'))) {
    throw "缺少引擎源码：$llamaDir（先 git submodule update --init）"
}

# 产品层文件清单 —— 与 build.ps1 的补丁应用范围一致
$files = @(
    'common/arg.cpp',
    'common/common.h',
    'common/chat.cpp',
    'tools/server/CMakeLists.txt',
    'tools/server/server-common.cpp',
    'tools/server/server-context.cpp',
    'tools/server/server-context.h',
    'tools/server/server.cpp',
    'tools/server/server-task.cpp',
    'tools/server/server-task.h',
    'tools/server/ssd-prompt-cache.cpp',
    'tools/server/ssd-prompt-cache.h'
)

git -C $llamaDir diff origin/strix-halo-vulkan -- $files > $patchPath
if ($LASTEXITCODE -ne 0) { throw 'git diff 失败（检查 origin/strix-halo-vulkan 引用是否存在）。' }

# 校验：补丁应能从纯化引擎干净应用
git -C $llamaDir checkout -q origin/strix-halo-vulkan -- $files
git -C $llamaDir rm -q --ignore-unmatch tools/server/ssd-prompt-cache.cpp tools/server/ssd-prompt-cache.h 2>$null
git -C $llamaDir apply --check $patchPath
if ($LASTEXITCODE -ne 0) {
    git -C $llamaDir checkout -q HEAD -- .
    throw "补丁校验失败：无法从纯化引擎应用。请修正产品层定制后重试。"
}
# 恢复工作区（补丁已应用）
git -C $llamaDir apply $patchPath
Write-Host "补丁已更新：$patchPath"
Write-Host "（$((Get-Content $patchPath | Measure-Object -Line).Lines) 行）"
