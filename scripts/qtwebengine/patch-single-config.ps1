<#
.SYNOPSIS
    Narrow the QtWebEngine module build to a single CMake configuration.

.DESCRIPTION
    A module build passes no -G, so CMake picks its Windows default generator,
    "Visual Studio 17 2022" - a multi-config generator. The installed Qt then forces
    CMAKE_CONFIGURATION_TYPES to "RelWithDebInfo;Debug" through its
    QtBuildInternalsExtra.cmake (verified: configure prints "Building for multiple
    configurations: RelWithDebInfo;Debug.").

    QtWebEngine creates one gn/ninja build tree per configuration, and every one of
    them is a dependency of the WebEngineCore target, so building the tree compiles
    BOTH configurations. Measured on the first successful compile: ninja reached
    [8038/29705] on .../RelWithDebInfo/AMD64 in 56 minutes while the Debug tree's gn
    generation had already run and its ninja sat queued behind it. That doubles both
    the hours and the disk for a runtime that only ever installs one configuration
    (Debug output carries CMAKE_DEBUG_POSTFIX and cannot be laid over PySide6).

    This patch re-forces CMAKE_CONFIGURATION_TYPES to the one configuration we ask
    for, right after the Qt package has had its say and before the project is
    generated. The value comes from -DQTWE_BUILD_CONFIGURATION on the configure
    command line, so build.cmd's BUILD_TYPE stays the single source of truth; with
    the define absent the block does nothing and the old behaviour is preserved.
    build.cmd's :assert_config fails the round in seconds if the narrowing did not
    take effect, so this can never cost a multi-hour build silently.

    Idempotent, marker-delimited.

.PARAMETER SourceRoot
    QtWebEngine source checkout (the directory containing CMakeLists.txt).

.PARAMETER AllowMissing
    Exit 0 instead of failing when the file or the anchor is absent.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceRoot,

    [switch]$AllowMissing
)

$ErrorActionPreference = 'Stop'

$beginMarker = '# --- qtwebengine-build: single configuration ---'
$endMarker = '# --- end qtwebengine-build: single configuration ---'

$target = Join-Path $SourceRoot 'CMakeLists.txt'
if (-not (Test-Path -LiteralPath $target)) {
    if ($AllowMissing) { exit 0 }
    Write-Error "[single-config] 目标文件不存在：$target（源码未取全？要跳过补丁请设 SKIP_PATCH=1）"
    exit 1
}

# 锚点必须在 QtBuildInternalsExtra.cmake 之后：那个文件正是在 find_package 里把
# CMAKE_CONFIGURATION_TYPES FORCE 成 RelWithDebInfo;Debug 的，早于它就会被覆盖。
$anchor = 'find_package(Qt6 6.5 CONFIG REQUIRED COMPONENTS BuildInternals Core)'

$block = @(
    $beginMarker
    'if(DEFINED QTWE_BUILD_CONFIGURATION)'
    '    set(CMAKE_CONFIGURATION_TYPES "${QTWE_BUILD_CONFIGURATION}" CACHE STRING "" FORCE)'
    'endif()'
    $endMarker
) -join "`n"

$content = Get-Content -LiteralPath $target -Raw
$newline = if ($content.Contains("`r`n")) { "`r`n" } else { "`n" }
$block = $block.Replace("`n", $newline)

if ($content.Contains($beginMarker)) {
    # 已经打过：整块替换，保证内容与当前脚本一致（幂等）。
    $pattern = [regex]::Escape($beginMarker) + '.*?' + [regex]::Escape($endMarker)
    # 用 MatchEvaluator 而不是替换串：块里有 ${...} 和 $ 字符，替换串会把它们当反向引用。
    $updated = [regex]::Replace($content, $pattern, { param($m) $block }, 'Singleline')
    if ($updated -eq $content) {
        Write-Host '[single-config] 已注入且内容一致，跳过'
        exit 0
    }
    Set-Content -LiteralPath $target -Value $updated -NoNewline
    Write-Host '[single-config] 已更新注入块'
    exit 0
}

$lines = $content -split "`r?`n"
$hits = @($lines | Where-Object { $_.Trim() -eq $anchor })
if ($hits.Count -ne 1) {
    if ($AllowMissing) { exit 0 }
    Write-Error "[single-config] 锚点出现 $($hits.Count) 次，预期 1 次；拒绝猜测，请人工确认：$anchor"
    exit 1
}

$out = New-Object System.Collections.Generic.List[string]
$inserted = $false
foreach ($line in $lines) {
    $out.Add($line)
    if (-not $inserted -and $line.Trim() -eq $anchor) {
        $out.Add('')
        foreach ($bl in ($block -split "`r?`n")) { $out.Add($bl) }
        $inserted = $true
    }
}

if (-not $inserted) {
    if ($AllowMissing) { exit 0 }
    Write-Error "[single-config] 未能插入注入块（锚点匹配但循环未命中）"
    exit 1
}

Set-Content -LiteralPath $target -Value ($out -join $newline) -NoNewline
Write-Host '[single-config] 已注入到 CMakeLists.txt（锚点后）：'
Write-Host '[single-config]   set(CMAKE_CONFIGURATION_TYPES "${QTWE_BUILD_CONFIGURATION}" ... FORCE)'
exit 0
