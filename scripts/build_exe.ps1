# AI-Qwen-Max 绿色版打包：PyInstaller onedir
# 产出 dist\max\max.exe（顶层启动文件）+ dist\max\_internal\（Python 运行时 + llama 引擎全家桶）
# 用法：.\scripts\build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # 仓库根（本脚本在 scripts\ 下）

if (-not (Test-Path "build\bin\llama-server.exe")) {
    Write-Host "缺少 build\bin\llama-server.exe —— 请先运行 .\scripts\build.ps1"
    exit 1
}

# llama-server 运行所需文件（llama-bench / *.map 不需要）
$llamaFiles = @(
    "build\bin\llama-server.exe",
    "build\bin\llama-server-impl.dll",
    "build\bin\llama-common.dll",
    "build\bin\llama.dll",
    "build\bin\ggml.dll",
    "build\bin\ggml-base.dll",
    "build\bin\ggml-cpu.dll",
    "build\bin\ggml-vulkan.dll",
    "build\bin\mtmd.dll"
)

$addBin = @()
foreach ($f in $llamaFiles) { $addBin += "--add-binary"; $addBin += "$f;llama" }

# PyInstaller 入口 stub（运行 python -m ai_qwen_max 等价入口，打包后即为 max.exe）
$stubDir = ".pyinstaller-build"
New-Item -ItemType Directory -Force $stubDir | Out-Null
$stub = Join-Path $stubDir "max_entry.py"
Set-Content -Path $stub -Encoding UTF8 -Value @(
    "from ai_qwen_max.__main__ import main"
    "if __name__ == '__main__':"
    "    raise SystemExit(main())"
)

$pyiArgs = @(
    "--noconfirm", "--clean",
    "--onedir", "--console",
    "--name", "max",
    "--workpath", $stubDir,
    "--distpath", "dist",
    "--collect-all", "fastapi",
    "--collect-all", "uvicorn",
    "--collect-all", "httpx",
    "--hidden-import", "prompt_toolkit"
) + $addBin + @(
    "--add-data", "web;web",
    $stub
)

python -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "打包完成：dist\max\max.exe（绿色单文件夹版）"
Write-Host "  - 顶层：max.exe（启动文件）"
Write-Host "  - _internal\：Python 运行时 + llama 引擎（勿动）"
Write-Host "  - 首次运行自动创建 .max\（config / chat / cache-ssd）"
