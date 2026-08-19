#Requires -Version 5.1
<#
  AI-Qwen-Max 构建脚本
  目标：裁剪构建 llama-server —— 只为本机（Ryzen AI Max+ 395 / gfx1151）+ Vulkan + 原生指令。
  放弃通用化：关闭一切无关后端，只用 NMake Makefiles + Release + GGML_NATIVE。

  引擎源码位于 vendor/llama.cpp（git submodule，fork 自 Nathanw1014/llama.cpp 的
  strix-halo-vulkan 分支）。引擎仓库 = 纯平台层（Ryzen/UMA/Vulkan 优化，分支
  ryzen-uma-vulkan）；本项目的产品层定制（SSD 缓存 / 生成段 checkpoint /
  retokenize 治愈 / qwen3_coder 容错 / SEH / shutdown 端点）以补丁形式在
  构建前应用，补丁文件：patches/qwenmax-server-layer.patch。
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent   # 仓库根（本脚本在 scripts\ 下）
$llamaDir = Join-Path $root 'vendor\llama.cpp'
$buildDir = Join-Path $root 'build'

Write-Host '==== AI-Qwen-Max build ===='

# ---- 1. Vulkan SDK ----
$vulkanSdk = $env:VULKAN_SDK
if (-not $vulkanSdk -or -not (Test-Path (Join-Path $vulkanSdk 'Include'))) {
    $latest = Get-ChildItem 'C:\VulkanSDK' -Directory -ErrorAction SilentlyContinue |
        Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
    if ($latest) { $vulkanSdk = $latest.FullName }
}
if (-not $vulkanSdk -or -not (Test-Path (Join-Path $vulkanSdk 'Include'))) {
    throw "未找到 Vulkan SDK。请先安装：winget install KhronosGroup.VulkanSDK ，并重开终端使环境变量生效。"
}
$env:VULKAN_SDK = $vulkanSdk
Write-Host "[1/5] Vulkan SDK : $vulkanSdk"

# ---- 2. MSVC ----
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "未找到 vswhere：请安装 Visual Studio（含『使用 C++ 的桌面开发』工作负载）。" }
$vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsPath) { throw "未找到 MSVC C++ 工具链。" }
$vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "未找到 vcvars64.bat：$vcvars" }
Write-Host "[2/5] MSVC      : $vsPath"

# ---- 3. 源码（git submodule，ryzen-uma-vulkan 分支） ----
if (-not (Test-Path (Join-Path $llamaDir 'CMakeLists.txt'))) {
    Write-Host "[3/5] 初始化 submodule ..."
    git -C $root submodule update --init --recursive
    if ($LASTEXITCODE -ne 0) { throw 'submodule 初始化失败（请检查网络后重试）。' }
}
if (-not (Test-Path (Join-Path $llamaDir 'CMakeLists.txt'))) {
    throw "缺少引擎源码：$llamaDir（git submodule update --init --recursive）"
}
$pinnedCommit = git -C $llamaDir rev-parse HEAD
Write-Host "[3/5] 源码     : $llamaDir  (commit: $pinnedCommit)"

# ---- 3b. 应用产品层 patch（C01/C02/C03/C06/C09/C10，见 patches/） ----
# 引擎仓库只含平台层；产品定制以补丁叠加。幂等：已应用则跳过。
$patchPath = Join-Path $root 'patches\qwenmax-server-layer.patch'
if (-not (Test-Path $patchPath)) {
    throw "缺少产品层补丁：$patchPath"
}
git -C $llamaDir apply --check --reverse $patchPath 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[3b] 产品层 patch：已应用，跳过"
} else {
    Write-Host "[3b] 应用产品层 patch ..."
    git -C $llamaDir apply $patchPath
    if ($LASTEXITCODE -ne 0) {
        throw "产品层 patch 应用失败（引擎已升级？需用 scripts/refresh-patch.ps1 重新生成补丁）。"
    }
}

# ---- 4. CMake 配置（裁剪：只留 Vulkan + server + 原生指令） ----
$cmakeFlags = @(
    '-G "NMake Makefiles"',  # ninja(jobserver) 在受限环境会挂死，改用 MSVC 自带 nmake
    '-DCMAKE_BUILD_TYPE=Release',
    '-DGGML_VULKAN=ON',        # gfx1151 iGPU 后端
    '-DGGML_NATIVE=ON',        # 只为本机 CPU 编译（Zen5 AVX-512），放弃通用化
    # 关闭所有无关后端
    '-DGGML_CUDA=OFF',
    '-DGGML_HIP=OFF',
    '-DGGML_OPENCL=OFF',
    '-DGGML_SYCL=OFF',
    '-DGGML_CANN=OFF',
    '-DGGML_MUSA=OFF',
    '-DGGML_BLAS=OFF',
    '-DGGML_METAL=OFF',
    '-DGGML_RPC=OFF',
    # 只要 server
    '-DLLAMA_BUILD_COMMON=ON',
    '-DLLAMA_BUILD_TOOLS=ON',
    '-DLLAMA_BUILD_SERVER=ON',
    '-DLLAMA_BUILD_EXAMPLES=OFF',
    '-DLLAMA_BUILD_TESTS=OFF',
    '-DLLAMA_BUILD_APP=OFF',
    '-DLLAMA_BUILD_UI=ON',         # 官方 Web UI（tools/ui SvelteKit，npm ci+build 后嵌入二进制；离线时可关）
    '-DLLAMA_USE_PREBUILT_UI=OFF',
    '-DLLAMA_CURL=OFF',
    '-DLLAMA_OPENSSL=OFF'          # 纯 HTTP（本地回环），不引入 OpenSSL 依赖
) -join ' '

New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
# 用临时 .cmd 继承 vcvars 环境，避免 PowerShell 引号转义问题
$bat = @"
@echo off
call "$vcvars" >nul
cmake -S "$llamaDir" -B "$buildDir" $cmakeFlags
if errorlevel 1 exit /b 1
cmake --build "$buildDir" --target llama-server
if errorlevel 1 exit /b 1
"@
$batPath = Join-Path $buildDir '_build.cmd'
Set-Content -Path $batPath -Value $bat -Encoding ASCII
Write-Host "[4/5] 配置 + 编译中 ..."
& cmd.exe /c $batPath
if ($LASTEXITCODE -ne 0) { throw "构建失败（exit $LASTEXITCODE）。" }

# ---- 5. 产物 ----
$server = Join-Path $buildDir 'bin\llama-server.exe'
if (-not (Test-Path $server)) { throw "未找到产物：$server" }
Write-Host "[5/5] 完成。产物：$server"
