<#
.SYNOPSIS
    构建「异环 RAG」单文件便携 exe。

.DESCRIPTION
    完整流程（任一步失败即中止）：
      1. 检查虚拟环境与依赖是否齐备；
      2. 生成图标（如缺失）；
      3. 生成 Windows 版本资源（版本号取自 app/__init__.py，两份 .spec 的 version= 引用它）；
      4. 构建前密钥门禁：tools/secret_scan.py 扫描源码，发现开发期密钥即中止；
      5. PyInstaller 打包（tools/pyinstaller_runner.py，自动适配本机沙箱临时目录问题）；
      6. 构建后密钥门禁：在生成的 exe 二进制里搜索 .env 中真实密钥的精确指纹；
      7. 输出产物大小与 SHA256，并再次确认 dist 目录里没有 .env。

    用法：
        powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
        powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1 -SkipTests

    说明：本脚本按 **Windows PowerShell 5.1** 编写（干净 Windows 上通常没有 pwsh 7，
    所以文档与用法里都写 powershell）。
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipSecretScan
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`n[失败] $text" -ForegroundColor Red; exit 1 }
function Ok($text)   { Write-Host "[完成] $text" -ForegroundColor Green }

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Fail "未找到虚拟环境：$python`n请先创建环境并安装依赖（见 README 的开发说明）。"
}

Step '1/7 检查依赖'
& $python -c "import fastapi, uvicorn, httpx, bs4, lxml, trafilatura, ddgs, webview, PyInstaller; print('依赖检查通过')"
if ($LASTEXITCODE -ne 0) { Fail '依赖不完整，请执行 tools\pip_runner.py install -r requirements-dev.txt' }

Step '2/7 生成图标'
& $python (Join-Path $root 'tools\make_icon.py')
if ($LASTEXITCODE -ne 0) { Fail '图标生成失败' }

# 版本资源：不生成的话 Windows 属性面板里产品名/版本是空的。
# 每次构建都重新生成，保证和 app/__init__.py 的 __version__ 一致（不提交写死的副本）。
Step '3/7 生成 Windows 版本资源'
& $python (Join-Path $root 'tools\make_version_info.py')
if ($LASTEXITCODE -ne 0) { Fail '版本资源生成失败' }

if (-not $SkipSecretScan) {
    Step '4/7 构建前密钥扫描（源码）'
    & $python (Join-Path $root 'tools\secret_scan.py')
    if ($LASTEXITCODE -ne 0) { Fail '源码中发现疑似密钥，已中止构建' }
} else {
    Step '4/7 构建前密钥扫描（已按参数跳过）'
}

# 数据目录隔离必须在**任何** `python -m app.main` 调用之前生效：
# app.main 启动时会按 paths.data_dir() 建目录、读配置，不钉住这个变量就会碰到
# 用户真实的 %APPDATA%\NTE-RAG（下面收尾的 `--version` 也会走到那里）。
$env:NTE_RAG_DATA_DIR = Join-Path $root '.build_selftest'
New-Item -ItemType Directory -Force $env:NTE_RAG_DATA_DIR | Out-Null

if (-not $SkipTests) {
    Step '4.5/7 启动自检'
    $selftestLog = Join-Path $env:NTE_RAG_DATA_DIR 'selftest_stderr.log'
    # 刻意不使用管道：本机沙箱下「管道 + 原生程序」会触发访问拒绝，
    # 自检报告同时会写入 $env:NTE_RAG_DATA_DIR\selftest_report.json，便于后续核对。
    #
    # 但日志输出必须从 PowerShell 眼里挪走：自检会正常写 INFO 日志到 stderr，而
    # PowerShell 5.1 把原生程序的 stderr 包成 NativeCommandError。实测「管道」和
    # `2> 文件` 两种写法都会让**整个构建成功却以退出码 1 结束**（2026-09-23 真实踩到：
    # 产物与验证全过，构建脚本自己返回 1，调用方会误判成失败；单独 `> 日志` 只挡 stdout）。
    # 所以用 cmd 把 fd2 接到文件，PowerShell 完全看不到 stderr，同时又留下完整日志。
    $selftestCmd = '"' + $python + '" -m app.main --selftest 2>"' + $selftestLog + '"'
    cmd.exe /c $selftestCmd
    $selftestCode = $LASTEXITCODE
    $reportPath = Join-Path $env:NTE_RAG_DATA_DIR 'selftest_report.json'
    if ($selftestCode -ne 0) {
        Write-Host "[警告] 源码自检未完全通过（详见 $reportPath），继续打包。" -ForegroundColor Yellow
        if (Test-Path $selftestLog) {
            Write-Host '--- 自检 stderr（末尾 20 行）---' -ForegroundColor DarkGray
            Get-Content $selftestLog -Tail 20 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        }
    } else {
        Ok '源码自检通过'
    }
}

Step '5/7 PyInstaller 打包（单文件 + 便携目录两种形态）'
$env:TMP = Join-Path $root 'piptmp'
$env:TEMP = $env:TMP
New-Item -ItemType Directory -Force $env:TMP | Out-Null

# 正在运行的程序会锁住 dist 里的 exe，导致 PyInstaller 覆盖失败（WinError 5 拒绝访问）。
# 这里先结束旧实例，避免出现难以理解的报错。
$running = @(Get-Process -Name 'NTE-RAG' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host ("  发现 {0} 个正在运行的 NTE-RAG 实例，先结束它们以免占用产物文件..." -f $running.Count) -ForegroundColor Yellow
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 1500
}
Remove-Item -Recurse -Force (Join-Path $root 'build'), (Join-Path $root 'dist') -ErrorAction SilentlyContinue

# 单文件版：交付最方便，但启动时需要把自身解包到临时目录
& $python (Join-Path $root 'tools\pyinstaller_runner.py') --clean --noconfirm 'NTE-RAG.spec'
if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller 单文件打包失败' }
$exe = Join-Path $root 'dist\NTE-RAG.exe'
if (-not (Test-Path $exe)) { Fail "未生成预期产物：$exe" }
Ok '单文件版打包完成'

# 便携目录版：不需要自解包，兼容性更好、启动更快
& $python (Join-Path $root 'tools\pyinstaller_runner.py') --noconfirm 'NTE-RAG-onedir.spec'
if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller 便携目录打包失败' }
$dirExe = Join-Path $root 'dist\NTE-RAG\NTE-RAG.exe'
if (-not (Test-Path $dirExe)) { Fail "未生成预期产物：$dirExe" }
Ok '便携目录版打包完成'

# 打成 zip，方便整体拷走。
# 注意：这里传的是**目录本身**而不是 `目录\*`。传 `\*` 时压缩包里没有顶层文件夹，
# 用户解压会把 2600 多个文件直接倒进当前目录（实测就是这个后果）；传目录本身
# 才会在压缩包里留下 NTE-RAG\ 这一层。
$zip = Join-Path $root 'dist\NTE-RAG-onedir.zip'
Compress-Archive -Path (Join-Path $root 'dist\NTE-RAG') -DestinationPath $zip -Force
Ok ("便携目录已压缩：" + $zip)

Step '6/7 构建后密钥扫描（exe 二进制）'
if (-not $SkipSecretScan) {
    & $python (Join-Path $root 'tools\secret_scan.py') --dist $exe
    if ($LASTEXITCODE -ne 0) { Fail '单文件 exe 中发现疑似密钥，请检查打包内容' }
    & $python (Join-Path $root 'tools\secret_scan.py') --dist $dirExe
    if ($LASTEXITCODE -ne 0) { Fail '便携目录 exe 中发现疑似密钥，请检查打包内容' }
}

Step '7/7 产物核对'
$stray = Get-ChildItem -Path (Join-Path $root 'dist') -Recurse -Force -ErrorAction SilentlyContinue |
         Where-Object { $_.Name -eq '.env' -or $_.Name -like '*.key' -or $_.Name -eq 'config.json' }
if ($stray) {
    $names = ($stray | ForEach-Object { $_.FullName }) -join "`n"
    Fail "dist 目录中出现了不应分发的文件：`n$names"
}

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash
$dirSize = [math]::Round(((Get-ChildItem (Join-Path $root 'dist\NTE-RAG') -Recurse -File |
            Measure-Object -Property Length -Sum).Sum / 1MB), 1)
# 压缩包大小要单独取：以前这里把「解压后的目录体积」当成了压缩包体积，
# 于是 40.6 MB 的 zip 被显示成 92.5 MB，容易让人以为分发体积很大。
$zipSize = [math]::Round((Get-Item $zip).Length / 1MB, 1)
$version = & $python -m app.main --version
Write-Host ''
Write-Host '单文件版（推荐分发）' -ForegroundColor Green
Write-Host "  产物   : $exe"
Write-Host "  体积   : $size MB"
Write-Host "  SHA256 : $hash"
Write-Host '便携目录版（兼容性更好，无需自解包）' -ForegroundColor Green
Write-Host "  产物   : $dirExe（同目录 _internal\\ 必须一起拷贝）"
Write-Host "  解压体积: $dirSize MB"
Write-Host "  压缩包 : $zip（$zipSize MB）"
Write-Host "版本     : $version"
Write-Host ''
Write-Host '提示：两种形态都无需安装依赖。若希望数据跟随程序（便携模式），在 exe 同目录建一个空的 portable.flag 文件。' -ForegroundColor DarkGray
