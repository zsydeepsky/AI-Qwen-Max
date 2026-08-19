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

# ssd-prompt-cache.* 是引擎剥离后的工作区新增文件（untracked）；git diff 默认
# 不包含 untracked 文件，先用 intent-to-add 标记让 diff 把它作为新文件导出。
git -C $llamaDir add -N tools/server/ssd-prompt-cache.cpp tools/server/ssd-prompt-cache.h

# 导出 diff。注意：PowerShell 5.1 的 `>` 重定向 native 命令输出默认写 UTF-16LE，
# git apply 无法解析，必须显式以 UTF-8 无 BOM 落盘。
$diffText = & git -C $llamaDir diff origin/strix-halo-vulkan -- $files
if ($LASTEXITCODE -ne 0) { throw 'git diff 失败（检查 origin/strix-halo-vulkan 引用是否存在）。' }
if ($null -eq $diffText -or ($diffText -is [string] -and [string]::IsNullOrEmpty($diffText)) -or ($diffText -is [System.Array] -and $diffText.Count -eq 0)) {
    throw 'git diff 为空：工作区相对基线无差异，请检查 $files 清单。'
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($patchPath, (($diffText -join "`n") + "`n"), $utf8NoBom)

# 校验：补丁应能从纯化引擎干净应用
# tracked 文件恢复为 origin 版本；ssd-prompt-cache.* 在 origin 中不存在
# （引擎剥离产品层后的工作区新增文件），物理删除后由 patch 重新创建。
$tracked = $files | Where-Object { $_ -notmatch '^tools/server/ssd-prompt-cache' }
git -C $llamaDir checkout -q origin/strix-halo-vulkan -- $tracked
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $llamaDir 'tools\server\ssd-prompt-cache.cpp'), (Join-Path $llamaDir 'tools\server\ssd-prompt-cache.h')
# 清理 intent-to-add 残留：此前 add -N 让 index 持有空条目，git apply 会把它
# 当作"新文件已存在"，导致新文件 diff 报 No valid patches。校验前必须先 reset。
git -C $llamaDir reset -q -- tools/server/ssd-prompt-cache.cpp tools/server/ssd-prompt-cache.h
git -C $llamaDir apply --check $patchPath
if ($LASTEXITCODE -ne 0) {
    Write-Error "补丁校验失败：无法从纯化引擎应用。请修正产品层定制后重试。"
    Write-Error "工作区当前为纯化状态（tracked 已还原、ssd 文件已删除），不再自动还原以免误毁未导出修改。"
    Write-Error "恢复命令：git -C vendor\llama.cpp apply $patchPath"
    exit 1
}
# 恢复工作区（补丁已应用）
git -C $llamaDir apply $patchPath
# 清理 intent-to-add 残留，恢复 untracked 工作区状态（与引擎剥离后的常态一致）
git -C $llamaDir reset -q -- tools/server/ssd-prompt-cache.cpp tools/server/ssd-prompt-cache.h
Write-Host "补丁已更新：$patchPath"
Write-Host "（$((Get-Content $patchPath | Measure-Object -Line).Lines) 行）"
