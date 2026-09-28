# 把自建 QtWebEngine 的运行时文件铺进目标目录。
#
# 两个用途共用一份文件清单，因此清单只写在这里一处：
#   1) 铺进 PySide6 安装目录（开发机 / 打包机）：-Destination <site-packages>\PySide6
#   2) 铺进空目录做成待分发的暂存树（CI）：      -Destination <dist>\qtwebengine-... -Create
#
# 为什么需要替换：PySide6 轮子里的 QtWebEngine 是 Qt 开源二进制，不含 H.264/AAC，
# 所以 <video> 放不了 MP4；自建产物与轮子同为 Qt 6.8.3 + MSVC 2022 x64，按文件名
# 覆盖即可被 QtWebEngineCore.pyd 直接加载（.pyd 只按 DLL 名解析，不做版本校验）。
#
# 版本一致性是硬条件：轮子的 Qt 版本与自建 Qt 版本不同就必须拒绝，否则是运行期崩溃
# 而不是加载期报错，那种失败最难查。脚本读 DLL 版本资源来判，不靠人记。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Source,

    [Parameter(Mandatory = $true)]
    [string]$Destination,

    # 目标目录不存在时创建（CI 的暂存树用）
    [switch]$Create,

    # 覆盖前先把旧文件备份到 <Destination>\_webengine-backup\（PySide6 原地覆盖时默认做）
    [switch]$NoBackup
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Source)) {
    Write-Error "[install] 源目录不存在：$Source"
    exit 1
}

# 必须存在的条目：缺任何一个，WebEngine 都起不来
$requiredFiles = @(
    'bin\Qt6WebEngineCore.dll',
    'bin\QtWebEngineProcess.exe',
    'resources\icudtl.dat',
    'resources\qtwebengine_resources.pak'
)
# 存在才拷的条目：QtWebEngineWidgets/Quick 是同一份构建的一部分，版本必须一起换；
# resources 下的其余 pak / snapshot 与 translations 也随版本走
$optionalFiles = @(
    'bin\Qt6WebEngineWidgets.dll',
    'bin\Qt6WebEngineQuick.dll',
    'bin\Qt6WebEngineQuickDelegatesQml.dll'
)

$missing = @()
foreach ($rel in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $Source $rel))) { $missing += $rel }
}
if ($missing.Count -gt 0) {
    Write-Error "[install] 源目录缺少必需文件：$($missing -join ', ')。源应为 CMake 安装前缀（含 bin\ 与 resources\）"
    exit 1
}

$targetExists = Test-Path -LiteralPath $Destination
if (-not $targetExists) {
    if (-not $Create) {
        Write-Error "[install] 目标目录不存在：$Destination（要新建请加 -Create）"
        exit 1
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
}

# 原地覆盖已装好的 PySide6 时才做版本核对与备份；暂存树没有可核对的对象
$overlay = Test-Path -LiteralPath (Join-Path $Destination 'Qt6WebEngineCore.dll')
if ($overlay) {
    $srcVer = (Get-Item -LiteralPath (Join-Path $Source 'bin\Qt6WebEngineCore.dll')).VersionInfo.FileVersion
    $dstVer = (Get-Item -LiteralPath (Join-Path $Destination 'Qt6WebEngineCore.dll')).VersionInfo.FileVersion
    Write-Host "[install] 版本核对：自建 $srcVer → 目标 $dstVer"
    if ($srcVer -and $dstVer) {
        $a = ($srcVer -split '\.')[0..1] -join '.'
        $b = ($dstVer -split '\.')[0..1] -join '.'
        if ($a -ne $b) {
            Write-Error "[install] 版本不一致：自建 Qt $a，目标 Qt $b。请让 PySide6 版本与自建版本一致"
            exit 1
        }
    }
    if (-not $NoBackup) {
        $backup = Join-Path $Destination '_webengine-backup'
        New-Item -ItemType Directory -Path $backup -Force | Out-Null
        foreach ($rel in $requiredFiles + $optionalFiles) {
            $f = Join-Path $Destination (Split-Path $rel -Leaf)
            $candidate = if ($rel -like 'resources\*') { Join-Path $Destination $rel } else { $f }
            if (Test-Path -LiteralPath $candidate) {
                Copy-Item -LiteralPath $candidate -Destination (Join-Path $backup (Split-Path $candidate -Leaf)) -Force
            }
        }
        Write-Host "[install] 旧文件已备份到 $backup（回滚即从该目录拷回）"
    }
} else {
    Write-Host "[install] 目标不是已安装的 PySide6（未找到 Qt6WebEngineCore.dll），按暂存树处理"
}

$copied = @()

# 只有 bin\ 下的文件平铺到目标根（PySide6 的 DLL 与 exe 都在包根）。
# resources\ 下的东西不能平铺——PySide6 放在 resources\ 子目录里，平铺过去 WebEngine
# 反而找不到；它们由下面的整目录拷贝统一处理。
foreach ($rel in $requiredFiles + $optionalFiles) {
    if ($rel -notlike 'bin\*') { continue }
    $src = Join-Path $Source $rel
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Warning "[install] 跳过（源不存在）：$rel"
        continue
    }
    $dst = Join-Path $Destination (Split-Path $rel -Leaf)
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $copied += $dst
}

# resources\ 整目录（pak、snapshot、locales）与 translations\qtwebengine_* 随版本走
$resSrc = Join-Path $Source 'resources'
if (Test-Path -LiteralPath $resSrc) {
    $resDst = Join-Path $Destination 'resources'
    New-Item -ItemType Directory -Path $resDst -Force | Out-Null
    Get-ChildItem -LiteralPath $resSrc -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $resDst $_.Name) -Force
        $copied += (Join-Path $resDst $_.Name)
    }
    $locSrc = Join-Path $resSrc 'locales'
    if (Test-Path -LiteralPath $locSrc) {
        $locDst = Join-Path $resDst 'locales'
        New-Item -ItemType Directory -Path $locDst -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $locSrc '*') -Destination $locDst -Recurse -Force
    }
}

$trSrc = Join-Path $Source 'translations'
if (Test-Path -LiteralPath $trSrc) {
    $trDst = Join-Path $Destination 'translations'
    New-Item -ItemType Directory -Path $trDst -Force | Out-Null
    Get-ChildItem -LiteralPath $trSrc -Filter 'qtwebengine*' | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $trDst $_.Name) -Recurse -Force
        $copied += (Join-Path $trDst $_.Name)
    }
}

Write-Host "[install] 完成，共铺入 $($copied.Count) 项 → $Destination"
$core = Join-Path $Destination 'Qt6WebEngineCore.dll'
if (Test-Path -LiteralPath $core) {
    $item = Get-Item -LiteralPath $core
    $hash = (Get-FileHash -LiteralPath $core -Algorithm SHA256).Hash.ToLower()
    Write-Host ('[install] Qt6WebEngineCore.dll  {0:N1} MB  sha256={1}' -f ($item.Length / 1MB), $hash)
}
exit 0
