# JJ象棋 自动教练 —— 一键启动（PowerShell）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 -Side black -Movetime 1500
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 -Depth 20      # 想固定深度时
#
# 它会：
#   1. 清掉上次可能残留的进程
#   2. 检查引擎和识别模型是否就位
#   3. 自动找两个 Python（隔离环境跑识别，系统 Python 跑浮窗）
#   4. 启动后台识别 + 置顶浮窗
#   5. 关闭浮窗窗口 -> 自动停止全部进程
#
# 关于引擎强度（2026-09-29 实测）：
#   pikafish 的 `go depth N` 是"搜到 N 层就收工"，depth 14 只要 0.05 秒，
#   等于没搜；而 movetime 1000ms 能到深度 23。所以默认用 -Movetime。
#   另外引擎自带的 Hash 只有 16MB、Threads 只有 1，都太小，这里补上默认值。
param(
    [ValidateSet("red", "black")]
    [string]$Side = "red",          # 你执红还是执黑
    [int]$Movetime = 3000,          # 引擎每步思考毫秒数（实测 3000ms 约到深度 22，1000ms 只有 18）
    [int]$Depth = 0,                # >0 时改用固定深度。pikafish 的 depth 很浅，一般别用
    [int]$HashMB = 1024,            # 引擎哈希表 MB（引擎默认只有 16；实测 512 已饱和，再大不涨）
    [int]$Threads = 0,              # 引擎线程数，0 = 按 CPU 核数自动
    [double]$Interval = 0.25,       # 采样间隔秒
    [int]$Stable = 2,               # 画面连续稳定几帧才认定局面
    [ValidateSet("onnx", "template")]
    [string]$Backend = "onnx",      # 识别后端：onnx=整板分类（默认），template=老的模板匹配
    [switch]$NoLearn                # 关闭在线样本积累（仅 template 后端有效）
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Proj = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutDir = Join-Path $Proj "out"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

Say ""
Say "=== JJ象棋 自动教练 ===" "White"
Say "项目目录: $Proj"
Say ""

# ---------- 0. 清掉上次的残留 ----------
$stale = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*auto_coach*" -or $_.CommandLine -like "*hud.py*" }
if ($stale) {
    Say "清理上次残留进程 $($stale.Count) 个..." "Yellow"
    $stale | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

# ---------- 1. 前置检查 ----------
$engine = Join-Path $Proj "engine\pikafish.exe"
if (-not (Test-Path $engine)) {
    Say "缺少引擎: $engine" "Red"
    Say "  下载方式:  gh release download -R official-pikafish/Pikafish -D engine" "Yellow"
    Say "  解压后把 Pikafish-Windows-x86-64-universal.exe 改名成 pikafish.exe" "Yellow"
    exit 1
}

# 识别前置：按后端各查各的
if ($Backend -eq "onnx") {
    # 默认走整板 ONNX 分类
    $model = Join-Path $Proj "models\layout_nano.onnx"
    if (-not (Test-Path $model)) {
        Say "缺少整板识别模型: $model" "Red"
        Say "  下载（需要能访问 HuggingFace，直连不通会自动换 hf-mirror）:" "Yellow"
        Say "    python download_models.py" "Yellow"
        Say "  或者回退到老的模板匹配后端：-Backend template" "Yellow"
        exit 1
    }
} else {
    # 老后端需要开局自举出来的模板
    $tpl = Join-Path $Proj "out\templates.npz"
    if (-not (Test-Path $tpl)) {
        Say "缺少识别模板: $tpl" "Red"
        Say "  先在开局画面运行一次:" "Yellow"
        Say "    python grid_classify.py --build --learn" "Yellow"
        Say "  或者改用整板分类后端：-Backend onnx" "Yellow"
        exit 1
    }
}

# ---------- 2. 找 Python ----------
# 识别进程需要 cv2 / numpy / mss / onnxruntime，负责抓图 + 识别 + 引擎；
# 浮窗需要 tkinter（标准库自带）。多数情况同一个 Python 就够。
# 识别依赖装在独立虚拟环境、浮窗另用解释器时，用环境变量分别指定：
#   $env:JJCHESS_ID_PY / $env:JJCHESS_UI_PY
$cands = @()
if ($env:JJCHESS_ID_PY) { $cands += $env:JJCHESS_ID_PY }
if ($env:JJCHESS_UI_PY) { $cands += $env:JJCHESS_UI_PY }
foreach ($n in @("python", "python3", "py")) {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) { $cands += $c.Source }
}
foreach ($v in @("313", "312", "311", "310")) {
    $cands += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe")
}

# 能 import 就认，避免只看路径猜错解释器
function Test-PyMod($exe, $code) {
    try {
        & $exe -c $code 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch { return $false }
}

$idPy = $null
$uiPy = $null
foreach ($p in ($cands | Where-Object { $_ } | Select-Object -Unique)) {
    if (-not (Test-Path $p)) { continue }
    # WindowsApps 下的 python.exe 是微软商店的存根，跑起来会弹应用商店，跳过
    if ($p -like "*\WindowsApps\*") { continue }
    if (-not $idPy -and (Test-PyMod $p "import cv2, numpy, mss, onnxruntime")) { $idPy = $p }
    if (-not $uiPy -and (Test-PyMod $p "import tkinter"))                    { $uiPy = $p }
    if ($idPy -and $uiPy) { break }
}

if (-not $idPy) {
    Say "没找到装了识别依赖的 Python（cv2 / numpy / mss / onnxruntime）" "Red"
    Say "  装依赖: <你的python> -m pip install opencv-python numpy mss onnxruntime" "Yellow"
    Say "  或指定: `$env:JJCHESS_ID_PY = '...\python.exe'" "Yellow"
    exit 1
}
if (-not $uiPy) {
    Say "没找到带 tkinter 的 Python（浮窗需要它）" "Red"
    Say "  或指定: `$env:JJCHESS_UI_PY = '...\python.exe'" "Yellow"
    exit 1
}

Say "识别引擎 Python: $idPy" "Green"
Say "浮窗     Python: $uiPy" "Green"
Say ""

# ---------- 3. 启动 ----------
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

$coachArgs = @("app\auto_coach.py", "--side", $Side, "--backend", $Backend,
               "--movetime", "$Movetime", "--interval", "$Interval", "--stable", "$Stable")
if ($Depth -gt 0)   { $coachArgs += @("--depth", "$Depth") }
if ($HashMB -gt 0)  { $coachArgs += @("--hash-mb", "$HashMB") }
if ($Threads -gt 0) { $coachArgs += @("--threads", "$Threads") }
if ($NoLearn)       { $coachArgs += "--no-learn" }

$log  = Join-Path $OutDir "coach.log"
$errl = Join-Path $OutDir "coach.err.log"

Say "启动识别进程（日志: $log）..." "Cyan"
# 必须带 -u（无缓冲）：Python 的 stdout 重定向到文件时是块缓冲的，
# 不加的话日志要等进程退出才落盘，出问题时"检查日志"看到的是空文件。
$coach = Start-Process -FilePath $idPy -ArgumentList (@("-u") + $coachArgs) `
    -WorkingDirectory $Proj -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $log -RedirectStandardError $errl

Start-Sleep -Seconds 2

Say "启动浮窗..." "Cyan"
$hud = Start-Process -FilePath $uiPy -ArgumentList "app\hud.py" `
    -WorkingDirectory $Proj -PassThru

Say ""
Say "已启动（识别后端 $Backend）。" "Green"
if ($Depth -gt 0) {
    Say "  · 引擎：固定深度 $Depth（pikafish 的 depth 很浅，想要棋力请改用 -Movetime）" "Yellow"
} else {
    Say "  · 引擎：每步思考 $Movetime ms，Hash $HashMB MB" "White"
}
Say "  · 浮窗在屏幕右上角，可拖动，按 Esc 或关闭窗口即停" "White"
Say "  · 认不准时它显示「识别不确定」，不会乱出招" "White"
Say "  · 轮对方走时它会说明，不会给出用不上的建议" "White"
Say "  · 想临时调参数：改 config\tune.json 保存即可，下一轮生效，不用重启" "White"
Say ""
Say "（关闭浮窗窗口，这里会自动收尾）" "DarkGray"

# ---------- 4. 等任一进程退出，然后收尾 ----------
try {
    while ($true) {
        if ($coach.HasExited) { Say ""; Say "识别进程已退出，检查日志: $errl" "Yellow"; break }
        if ($hud.HasExited)   { Say ""; Say "浮窗已关闭。" "Gray"; break }
        Start-Sleep -Seconds 1
    }
}
finally {
    foreach ($p in @($coach, $hud)) {
        if ($p -and -not $p.HasExited) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Say "已停止全部进程。" "Yellow"
}
