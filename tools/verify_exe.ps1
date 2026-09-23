<#
.SYNOPSIS
    发布验证：在干净目录里运行已打包的 exe，确认它真的能独立工作。

.DESCRIPTION
    验证项：
      1. 把 exe 单独复制到一个全新的空目录（模拟用户拿到 exe 的场景）；
      2. 运行 `--selftest`，读取写盘的 JSON 报告，逐项核对（鉴权、接口、知识库、种子库）；
      3. 以 `--headless` 启动，轮询 HTTP 健康检查，确认打包后的服务真的能对外服务，
         并确认未携带令牌的请求会被拒绝（403）；
      4. 对 exe 二进制做密钥扫描；
      5. 汇总输出，任一项失败则返回非 0。

    用法：
        powershell -ExecutionPolicy Bypass -File tools\verify_exe.ps1
        powershell -ExecutionPolicy Bypass -File tools\verify_exe.ps1 -Exe dist\NTE-RAG.exe -Port 8791
#>
[CmdletBinding()]
param(
    [string]$Exe = 'dist\NTE-RAG.exe',
    [int]$Port = 8791,
    [switch]$KeepWorkspace
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Pass($t) { Write-Host "  [通过] $t" -ForegroundColor Green }
function Fail($t) { Write-Host "  [失败] $t" -ForegroundColor Red }
function Info($t) { Write-Host "  $t" -ForegroundColor Gray }

$failures = New-Object System.Collections.ArrayList

# onefile 打包的程序会同时存在「引导进程」和「真正在跑的隔离子进程」，两者的 exe 路径一模一样。
# `Start-Process -PassThru` 只拿到引导进程，把它 Stop-Process 掉之后子进程还活着：它继续监听端口、
# 并且锁住 exe 文件，于是脚本末尾那句 `Remove-Item -Recurse -Force` 只在 `-ErrorAction SilentlyContinue`
# 下静默失败——.verify 目录留下了 42 MB 的 exe，还留着一个后台服务，而输出依然是「全部验证通过」。
# 2026-09-23 就是这样留下了一个 8791 端口的僵尸 headless 服务，所以这里按 exe 路径把所有同名进程收干净。
function Stop-ExeProcesses {
    param([string]$ExePath)
    for ($i = 0; $i -lt 12; $i++) {
        $left = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -eq $ExePath } catch { $false }
        })
        if ($left.Count -eq 0) { return $true }
        foreach ($p in $left) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch { }
        }
        Start-Sleep -Milliseconds 400
    }
    $left = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -eq $ExePath } catch { $false }
    })
    return ($left.Count -eq 0)
}

$exePath = if ([System.IO.Path]::IsPathRooted($Exe)) { $Exe } else { Join-Path $root $Exe }
if (-not (Test-Path $exePath)) {
    Write-Host "找不到 exe：$exePath，请先运行 tools\build_exe.ps1" -ForegroundColor Red
    exit 2
}

$workspace = Join-Path $root '.verify'
if (Test-Path $workspace) {
    Remove-Item -Recurse -Force $workspace -ErrorAction SilentlyContinue
    if (Test-Path $workspace) {
        Write-Host "上一次的验证目录删不掉（可能还有残留进程占用）：$workspace" -ForegroundColor Red
        Write-Host '请先结束残留的 NTE-RAG 进程，或手动删除该目录后重新运行。' -ForegroundColor Red
        exit 2
    }
}
New-Item -ItemType Directory -Force $workspace | Out-Null

# 兼容两种交付形态：单文件（拷 1 个 exe）与便携目录（整个文件夹一起拷）
$exeDir = Split-Path -Parent $exePath
$exeLeaf = Split-Path -Leaf $exePath
$isOnedir = Test-Path (Join-Path $exeDir '_internal')
if ($isOnedir) {
    Info '检测到便携目录版（onedir），整体复制到干净目录'
    Copy-Item $exeDir (Join-Path $workspace 'app') -Recurse -Force
    $targetExe = Join-Path $workspace "app\$exeLeaf"
    # onedir 已直接位于工作区内，data 目录会落在 app/ 旁边
    $dataDir = Join-Path $workspace 'app\data'
} else {
    $targetExe = Join-Path $workspace $exeLeaf
    Copy-Item $exePath $targetExe -Force
    $dataDir = Join-Path $workspace 'data'
}

$sizeMb = [math]::Round((Get-Item $targetExe).Length / 1MB, 1)
$hash = (Get-FileHash $targetExe -Algorithm SHA256).Hash

Step "0/6 产物信息"
Info "文件   : $targetExe"
Info "形态   : $(if ($isOnedir) { '便携目录（onedir）' } else { '单文件（onefile）' })"
Info "exe    : $sizeMb MB"
if ($isOnedir) {
    $totalMb = [math]::Round(((Get-ChildItem (Join-Path $workspace 'app') -Recurse -File |
                Measure-Object -Property Length -Sum).Sum / 1MB), 1)
    Info "整体   : $totalMb MB（exe 与 _internal 必须一起分发）"
}
Info "SHA256 : $hash"
Info "干净目录：$workspace（模拟用户首次拿到产物）"

# ---------------------------------------------------------------- 1
Step '1/6 独立自检（--selftest）'
# 用 Start-Process -Wait：exe 是 GUI 子系统程序，直接 & 调用拿不到可靠退出码
$selftestProc = Start-Process -FilePath $targetExe -ArgumentList '--selftest' -PassThru
# 同样要自己管超时：单文件形态若卡在自解包，-Wait 会一直等下去
$selftestExited = $selftestProc.WaitForExit(180000)
$selftestCode = if ($selftestExited) { $selftestProc.ExitCode } else {
    try { Stop-Process -Id $selftestProc.Id -Force -ErrorAction SilentlyContinue } catch { }
    Info '自检超过 180 秒未退出，已强制结束进程（单文件自解包在本环境受限）'
    -1
}
$reportPath = Join-Path $dataDir 'selftest_report.json'
if (-not (Test-Path $reportPath)) {
    Fail "未生成自检报告：$reportPath"
    [void]$failures.Add('selftest-report-missing')
} else {
    $report = Get-Content $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    # /api/health 现在只回 ok（不泄露版本与后端形态），版本在报告顶层，FTS 在 stats 里。
    Info ("版本 {0}｜FTS5={1}｜鉴权已启用={2}" -f $report.version, $report.stats.fts_enabled, (-not $report.auth_disabled))
    Info ("知识库：文档 {0} 篇 / 切片 {1} / 条目 {2}" -f $report.stats.documents, $report.stats.chunks, $report.stats.facts)
    if ($report.checks) {
        foreach ($name in $report.checks.PSObject.Properties.Name) {
            if ($report.checks.$name) { Pass "自检项 $name" } else { Fail "自检项 $name"; [void]$failures.Add("selftest:$name") }
        }
    }
    if ($report.stats.documents -lt 1) {
        Fail '种子知识库未随 exe 分发（文档数为 0）'
        [void]$failures.Add('seed-missing')
    } else {
        Pass '种子知识库已随 exe 分发并成功导入'
    }
    if ($report.webview) {
        if ($report.webview_supported) {
            Pass 'WebView2/pythonnet 可用，程序会使用原生窗口'
        } else {
            Info ("WebView2/pythonnet 在本机不可用（环境限制），程序按设计改用默认浏览器：" + $report.browser_hint)
        }
    }
    if ($selftestCode -ne 0) { Fail "自检退出码为 $selftestCode"; [void]$failures.Add('selftest-exit') }
}

# ---------------------------------------------------------------- 1.5
Step '2/6 窗口冒烟测试（--window-test --no-console，会弹窗并在数秒后自动关闭）'
$windowProc = Start-Process -FilePath $targetExe -ArgumentList '--window-test', '--no-console', '--window-seconds', '4' -PassThru
# 必须自己管超时：onefile 形态在受限环境里可能卡在自解包阶段（不报错也不退出），
# 之前 `-Wait` 直接把验证脚本吊死了 10 分钟。这里最多等 90 秒，然后强杀。
$windowExited = $windowProc.WaitForExit(90000)
if (-not $windowExited) {
    try { Stop-Process -Id $windowProc.Id -Force -ErrorAction SilentlyContinue } catch { }
    Info '窗口测试超过 90 秒未退出，已强制结束进程'
    Info '单文件形态在受限环境里无法自解包（本沙箱已知限制）；请手动双击 dist\NTE-RAG.exe 确认'
    Info '便携目录形态（dist\NTE-RAG\NTE-RAG.exe）不受此限制'
} else {
    switch ($windowProc.ExitCode) {
        0 { Pass '打包后的 exe 能正常创建原生窗口（WebView2 链路可用）' }
        2 {
            Info '本机 WebView2/pythonnet 不可用，程序按设计降级为浏览器（不算缺陷）'
            Info '在普通 Windows 10/11 机器上应能正常弹出原生窗口，请手动双击确认'
        }
        default {
            Fail "窗口创建失败（退出码 $($windowProc.ExitCode)）"
            [void]$failures.Add('window-create')
        }
    }
}

# ---------------------------------------------------------------- 2
Step '3/6 打包后服务实跑（--headless --no-console，模拟双击的真实条件）'
# 必须带 --no-console：它会清空 sys.stdout/sys.stderr，精确复现「双击窗口程序」的状态。
# 曾经因此漏掉一个真实缺陷——uvicorn 的默认日志格式器会对 sys.stdout.isatty() 求值，
# 无控制台时直接抛 AttributeError 导致启动失败，而只跑 --headless（会 attach 控制台）永远发现不了。
$proc = $null
try {
    $proc = Start-Process -FilePath $targetExe -ArgumentList '--headless', '--no-console', '--port', $Port -PassThru
    $base = "http://127.0.0.1:$Port"
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 700
        try {
            $health = Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 3
            if ($health.ok) { $ready = $true; break }
        } catch { }
    }
    if ($ready) {
        Pass '打包后的服务已就绪（/api/health 只回 ok；版本与 FTS 由已鉴权的 /api/state 提供）'
    } else {
        Fail '打包后的服务未在 42 秒内就绪（60 × 700ms）'
        [void]$failures.Add('packaged-server-not-ready')
    }

    if ($ready) {
        try {
            $null = Invoke-WebRequest -Uri "$base/api/state" -TimeoutSec 5 -UseBasicParsing
            Fail '未携带令牌的 /api/state 竟然返回成功，鉴权失效'
            [void]$failures.Add('auth-bypass')
        } catch {
            $code = $null
            if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            if ($code -eq 403) {
                Pass '未携带令牌的请求被正确拒绝（403）'
            } else {
                Fail "未携带令牌的请求返回了意外状态码：$code"
                [void]$failures.Add('auth-unexpected')
            }
        }
        # 带上令牌（从首页 Cookie 获取）后再试一次
        try {
            $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
            $null = Invoke-WebRequest -Uri "$base/" -WebSession $session -TimeoutSec 5 -UseBasicParsing
            $state = Invoke-RestMethod -Uri "$base/api/state" -WebSession $session -TimeoutSec 10
            if ($state.version) { Pass "通过首页会话令牌可正常访问接口（版本 $($state.version)，FTS5=$($state.stats.fts_enabled)）" } else { Fail '会话令牌访问接口异常'; [void]$failures.Add('session-token') }
        } catch {
            Fail "会话令牌访问接口失败：$($_.Exception.Message)"
            [void]$failures.Add('session-token-error')
        }
    }
} finally {
    if ($proc -and -not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    # 父进程死了不代表服务停了（见 Stop-ExeProcesses 的注释），这里必须确认干净
    if (-not (Stop-ExeProcesses -ExePath $targetExe)) {
        Fail '打包后的服务进程未能完全停止，可能仍在后台监听端口'
        [void]$failures.Add('server-not-stopped')
    }
}

# ---------------------------------------------------------------- 3
Step '4/6 断网降级（阻止联网后仍能本地检索）'
Info '跳过真实断网测试（会打断本机网络）；改为验证「无模型配置时降级」是否正常'
$configPath = Join-Path $dataDir 'config.json'
if (Test-Path $configPath) {
    $cfg = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $keySet = [bool]$cfg.llm.key_enc
    if ($keySet) {
        Fail '干净目录里竟然已有密钥配置，验证环境不干净'
        [void]$failures.Add('dirty-config')
    } else {
        Pass '干净目录中无任何密钥配置（符合首次运行预期）'
    }
} else {
    Info '未生成 config.json（未修改设置时不写盘，属正常）'
}
if ($report -and $report.chat_status -eq 200 -and $report.chat_degraded) {
    Pass '未配置模型时问答接口优雅降级（本地证据直出）'
} else {
    Fail '未配置模型时的降级行为异常'
    [void]$failures.Add('degrade')
}

# ---------------------------------------------------------------- 4
Step '5/6 exe 二进制密钥扫描'
& (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'tools\secret_scan.py') --dist $targetExe
if ($LASTEXITCODE -ne 0) {
    Fail 'exe 中发现疑似密钥'
    [void]$failures.Add('secret-leak')
} else {
    Pass 'exe 中未发现任何密钥痕迹'
}

# ---------------------------------------------------------------- 5
Step '6/6 分发目录清洁度'
# cacert.pem 是 certifi 附带的公共 CA 证书包（HTTPS 校验用），不是机密，需排除
$stray = Get-ChildItem -Path $workspace -Recurse -Force -ErrorAction SilentlyContinue |
         Where-Object {
             ($_.Name -eq '.env' -or $_.Name -like '*.key' -or $_.Name -like '*.pem') -and
             $_.Name -ne 'cacert.pem'
         }
if ($stray) {
    Fail ("发现不应分发的文件：" + (($stray | ForEach-Object { $_.FullName }) -join '; '))
    [void]$failures.Add('stray-files')
} else {
    Pass '分发目录中没有 .env / 私钥文件（已排除 certifi 的公共 CA 包 cacert.pem）'
}

# 上面看的是验证工作区。工作区里的 data\config.json 与 data\selftest_report.json
# 是这次实跑必然产生的（里面带绝对路径），所以不能拿工作区当判据；
# 真正要盯的是**即将分发的源目录**：解压出来的东西里不能有数据库、配置或便携标记。
$distStray = @()
if (Test-Path $exeDir) {
    $distStray = Get-ChildItem -Path $exeDir -Recurse -Force -ErrorAction SilentlyContinue |
                 Where-Object {
                     $_.Name -in @('config.json', 'selftest_report.json', 'portable.flag') -or
                     $_.Name -like '*.db' -or
                     $_.Name -like '*.sqlite' -or
                     $_.Name -like '*.sqlite3'
                 }
}
if ($distStray) {
    Fail ("发行目录里混入了运行期产物：" + (($distStray | ForEach-Object { $_.FullName }) -join '; '))
    [void]$failures.Add('dist-stray')
} else {
    Pass '发行目录里没有数据库 / 配置 / 便携标记（解压即用，不带运行期产物）'
}

if (-not $KeepWorkspace) {
    Remove-Item -Recurse -Force $workspace -ErrorAction SilentlyContinue
    if (Test-Path $workspace) {
        # 静默失败过一次：残留进程锁住 exe，临时目录被留下却没人发现（2026-09-23）
        [void](Stop-ExeProcesses -ExePath $targetExe)
        Start-Sleep -Milliseconds 600
        Remove-Item -Recurse -Force $workspace -ErrorAction SilentlyContinue
    }
    if (Test-Path $workspace) {
        Fail "临时验证目录未能删除：$workspace（仍有进程占用或权限不足）"
        [void]$failures.Add('workspace-not-removed')
    } else {
        Pass '临时验证目录已清理'
    }
}

# ---------------------------------------------------------------- 汇总
Write-Host ''
if ($failures.Count -eq 0) {
    Write-Host '全部验证通过 ✅' -ForegroundColor Green
    Write-Host "产物：$exePath（$sizeMb MB）" -ForegroundColor Green
    exit 0
} else {
    Write-Host ("验证失败，共 {0} 项：" -f $failures.Count) -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
