#Requires -Version 5.1
<#
  下载 Qwen3.8-27B 的 GGUF 到 models\。
  默认走 HuggingFace 直链（curl 断点续传）；装好 huggingface_hub 也可用 huggingface-cli。

  用法：
    .\scripts\download-model.ps1 -Repo "unsloth/Qwen3.8-27B-GGUF" -File "Qwen3.8-27B-UD-Q5_K_XL.gguf"

  说明：具体仓库名/文件名以 HuggingFace 上的 GGUF 发布为准。
#>
param(
    [Parameter(Mandatory = $true)][string]$Repo,   # HuggingFace 仓库，如 Qwen/Qwen3.8-27B-GGUF
    [Parameter(Mandatory = $true)][string]$File,   # 文件名，如 Qwen3.8-27B-Q4_K_M.gguf
    [string]$OutPath = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent   # 仓库根（本脚本在 scripts\ 下）
if (-not $OutPath) { $OutPath = Join-Path $root "models\$File" }
$dir = Split-Path $OutPath -Parent
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$hfCli = Get-Command huggingface-cli -ErrorAction SilentlyContinue
if ($hfCli) {
    Write-Host "huggingface-cli download $Repo $File -> $dir"
    & huggingface-cli download $Repo $File --local-dir $dir
    if ($LASTEXITCODE -ne 0) { throw 'huggingface-cli 下载失败。' }
} else {
    $url = "https://huggingface.co/$Repo/resolve/main/$File"
    Write-Host "curl 下载 $url -> $OutPath"
    & curl.exe -L --fail --retry 3 -C - -o $OutPath $url
    if ($LASTEXITCODE -ne 0) { throw "下载失败。可改用：pip install huggingface_hub 后重跑本脚本（支持断点续传）。" }
}
Write-Host "完成：$OutPath"
