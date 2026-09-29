# 把 CMake 安装前缀里的 QtWebEngine 运行时文件铺成一个待分发目录（staging tree）。
#
# 这是打包的前提：build.cmd 的 :package 阶段调它，CI 的「归档运行时」再把该目录压成
# zip + sha256 并发 Release。清单只写在这里一处——required 缺一个就失败，optional 存在才拷。
#
# 布局：bin\ 下的 DLL/exe 平铺到目标根，resources\ 与 translations\ 按子目录保留。
# 平铺是刻意的：PySide6 的包根就是这个样子（QtWebEngineCore.pyd 只按 DLL 名解析），
# 所以解压之后可以直接把根下文件与 resources\、translations\ 覆盖进 PySide6。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Source,        # CMake 安装前缀（cmake --install 的落点，bin\ 下有 DLL）

    [Parameter(Mandatory = $true)]
    [string]$Destination,   # 待分发目录

    # 目标目录不存在时创建
    [switch]$Create
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Source)) {
    Write-Error "[stage] 源目录（安装前缀）不存在：$Source"
    exit 1
}
$binSrc = Join-Path $Source 'bin'

# 必须存在的条目：缺任何一个，这套运行时都不完整
$requiredBin = @(
    'Qt6WebEngineCore.dll',
    'QtWebEngineProcess.exe'
)
$requiredResources = @(
    'resources\icudtl.dat',
    'resources\qtwebengine_resources.pak'
)
# 存在才拷的条目：Widgets/Quick 是同一份构建的一部分，版本必须一起换
$optionalBin = @(
    'Qt6WebEngineWidgets.dll',
    'Qt6WebEngineQuick.dll',
    'Qt6WebEngineQuickDelegatesQml.dll'
)

$missing = @()
foreach ($f in $requiredBin) {
    if (-not (Test-Path -LiteralPath (Join-Path $binSrc $f))) { $missing += "bin\$f" }
}
foreach ($r in $requiredResources) {
    if (-not (Test-Path -LiteralPath (Join-Path $Source $r))) { $missing += $r }
}
if ($missing.Count -gt 0) {
    Write-Error "[stage] 安装前缀缺少必需文件：$($missing -join ', ')"
    exit 1
}

if (-not (Test-Path -LiteralPath $Destination)) {
    if (-not $Create) {
        Write-Error "[stage] 目标目录不存在：$Destination（要新建请加 -Create）"
        exit 1
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
}

$copied = @()

# bin\ 下的文件平铺到目标根
foreach ($f in $requiredBin + $optionalBin) {
    $src = Join-Path $binSrc $f
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Warning "[stage] 跳过（源不存在）：bin\$f"
        continue
    }
    $dst = Join-Path $Destination $f
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $copied += $dst
}

# resources\：文件 + 名字里带 locales 的子目录（语言包在不同 Qt 版本里位置不同）
$resSrc = Join-Path $Source 'resources'
if (Test-Path -LiteralPath $resSrc) {
    $resDst = Join-Path $Destination 'resources'
    New-Item -ItemType Directory -Path $resDst -Force | Out-Null
    Get-ChildItem -LiteralPath $resSrc -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $resDst $_.Name) -Force
        $copied += (Join-Path $resDst $_.Name)
    }
    Get-ChildItem -LiteralPath $resSrc -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like '*locales*' } | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $resDst $_.Name) -Recurse -Force
            $copied += (Join-Path $resDst $_.Name)
        }
}

# translations\qtwebengine*（Qt 6.8.3 的语言包在 translations\qtwebengine_locales\）
$trSrc = Join-Path $Source 'translations'
if (Test-Path -LiteralPath $trSrc) {
    $trDst = Join-Path $Destination 'translations'
    New-Item -ItemType Directory -Path $trDst -Force | Out-Null
    Get-ChildItem -LiteralPath $trSrc -Filter 'qtwebengine*' | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $trDst $_.Name) -Recurse -Force
        $copied += (Join-Path $trDst $_.Name)
    }
}

Write-Host "[stage] 完成，共铺入 $($copied.Count) 项 → $Destination"

# 尺寸/sha256 只是留证据，铺入本身到上面已经成功；这段不该把成功的打包判成失败
$core = Join-Path $Destination 'Qt6WebEngineCore.dll'
if (Test-Path -LiteralPath $core) {
    $item = Get-Item -LiteralPath $core
    $hash = $null
    try { $hash = (Get-FileHash -LiteralPath $core -Algorithm SHA256).Hash.ToLower() } catch { }
    if ($hash) {
        Write-Host ('[stage] Qt6WebEngineCore.dll  {0:N1} MB  sha256={1}' -f ($item.Length / 1MB), $hash)
    } else {
        Write-Host ('[stage] Qt6WebEngineCore.dll  {0:N1} MB' -f ($item.Length / 1MB))
    }
}
exit 0
