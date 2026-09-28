<#
.SYNOPSIS
    Make Chromium's cc_wrapper reach the MSVC toolchain, so ccache is actually called.

.DESCRIPTION
    Chromium builds the compiler command line out of cl_prefix
    (chromium/build/toolchain/win/toolchain.gni), and it only ever fills cl_prefix when
    the toolchain is clang:

        } else if (toolchain_cc_wrapper != "" && toolchain_is_clang) {
          cl_prefix = toolchain_cc_wrapper + " "
        } else {
          cl_prefix = ""
        }

    patch-gn-args.ps1 does inject cc_wrapper="ccache" into the core module's args.gn -
    verified present in the build log - but Qt's Windows QtWebEngine build sets
    is_clang=false (args.gn: is_clang=false, is_msvc=true), so the value is dropped on
    the floor and ccache is never invoked. Measured on that configuration: 8038 compiled
    targets, a ccache directory of 391 bytes, and no "Cacheable calls" in
    `ccache --show-stats` at all.

    That is the whole reason the kiwi-style recipe (one round builds part of the tree,
    the cache carries it into the next round) could not work here while the same recipe
    works for kiwi browser's CI: kiwi builds Chromium for Android on Linux, i.e. clang,
    and it wraps the compiler itself through CC/CXX. ccache does support MSVC - its
    support table grades MSVC as level A - so the only missing piece was this condition.

    The patch drops the toolchain_is_clang test, so cc_wrapper is prefixed for MSVC too.
    With cc_wrapper empty (USE_CCACHE=0) nothing changes: cl_prefix stays empty.
    Idempotent and anchored on the exact upstream fragment, so a moved or rewritten
    upstream fails loudly instead of being patched by accident.

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

$relative = 'src/3rdparty/chromium/build/toolchain/win/toolchain.gni'
$target = Join-Path $SourceRoot $relative

$original = '} else if (toolchain_cc_wrapper != "" && toolchain_is_clang) {'
$patched = '} else if (toolchain_cc_wrapper != "") {'

if (-not (Test-Path -LiteralPath $target)) {
    if ($AllowMissing) {
        Write-Host "[msvc-ccache] 目标文件不存在，按 AllowMissing 跳过：$target"
        exit 0
    }
    Write-Error "[msvc-ccache] 目标文件不存在：$target（源码未取全？要跳过补丁请设 SKIP_PATCH=1）"
    exit 1
}

$content = [System.IO.File]::ReadAllText($target)
$newline = if ($content.Contains("`r`n")) { "`r`n" } else { "`n" }

if (-not $content.Contains($original)) {
    if ($content.Contains($patched)) {
        Write-Host '[msvc-ccache] 已打过（cc_wrapper 对 MSVC 工具链也生效），跳过'
        exit 0
    }
    if ($AllowMissing) {
        Write-Host '[msvc-ccache] 未找到锚点，按 AllowMissing 跳过（上游可能已改）'
        exit 0
    }
    Write-Error "[msvc-ccache] 未找到锚点：$original （$relative 结构变了，需要人工核对）"
    exit 1
}

$lines = $content -split "`r?`n"
$hits = @($lines | Where-Object { $_.Contains($original) })
if ($hits.Count -ne 1) {
    if ($AllowMissing) { exit 0 }
    Write-Error "[msvc-ccache] 锚点出现 $($hits.Count) 次，预期 1 次；拒绝猜测"
    exit 1
}

# 缩进跟随原行：上游是 4 空格（toolchain.gni 在 if/else 链里多缩进一层）
$indent = ($hits[0] -replace '^(\s*).*$', '$1')

$out = New-Object System.Collections.Generic.List[string]
foreach ($line in $lines) {
    if ($line.Contains($original)) {
        # 注释留在前一行：保持 if/else 链的形状不变，只把条件放宽
        $out.Add("$indent# qtwebengine-build: also wrap the MSVC toolchain - ccache supports cl.exe,")
        $out.Add("$indent# and without this the injected cc_wrapper is silently ignored (is_clang=false).")
        $out.Add("$indent$patched")
    } else {
        $out.Add($line)
    }
}

[System.IO.File]::WriteAllText($target, ($out -join $newline), (New-Object System.Text.UTF8Encoding($false)))

# 断言落盘结果：新条件正好一处，旧条件一处不剩
$after = [System.IO.File]::ReadAllText($target)
$newCount = ([regex]::Matches($after, [regex]::Escape($patched))).Count
$oldCount = ([regex]::Matches($after, [regex]::Escape($original))).Count
if ($newCount -ne 1 -or $oldCount -ne 0) {
    Write-Error "[msvc-ccache] 落盘校验失败：新条件 $newCount 处（预期 1），旧条件 $oldCount 处（预期 0）"
    exit 1
}

Write-Host "[msvc-ccache] 已修补 $relative："
Write-Host '[msvc-ccache]   cc_wrapper 现在对 MSVC（is_clang=false）工具链也生效'
exit 0
