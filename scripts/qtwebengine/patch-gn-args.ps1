# 往 QtWebEngine 的 Chromium 构建里注入 gn 参数（当前只注入 cc_wrapper）。
#
# 为什么需要它：QtWebEngine 的 src/core/CMakeLists.txt 自己拼 gnArgs（gnArgArg），
# 没有给外部留「额外 gn 参数」的通道，所以只能改这处 CMakeLists。改动是加一段带标记的
# list(APPEND gnArgArg ...)，可重复执行（先删旧块再插新块）。
#
# 为什么 cc_wrapper 在 Windows 上也成立（这一条是查过源码的，不是想当然）：
#   chromium/build/toolchain/win/toolchain.gni 里
#       } else if (toolchain_cc_wrapper != "" && toolchain_is_clang) {
#         cl_prefix = toolchain_cc_wrapper + " "
#   Windows 上 Chromium 用自带的 clang-cl（toolchain_is_clang 为真），而 Qt 没有任何地方
#   把 is_clang 关掉，所以 cc_wrapper="ccache" 会被拼成 `ccache <clang-cl.exe> ...`。
#   ccache 官方把 MSVC 列为 A 级、clang-cl 列为 B 级支持。
#   cc_wrapper.gni 里那句 "Probably doesn't work on windows" 是陈述句已过时，
#   真正决定行为的是上面那段 toolchain.gni。
#   代价：设了 cc_wrapper 后 show_includes 会从 /showIncludes:user 退回 /showIncludes
#   （源码里有注释说明，是绕 sccache 的老问题），.ninja_deps 变大、依赖解析变慢一点。
#
# 幂等与可断言：注入后断言 list(APPEND gnArgArg 的出现次数正好 +1；不带任何开关调用则只清除注入块。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceRoot,

    # 注入 cc_wrapper="ccache"（要求 ccache.exe 在构建进程的 PATH 上）
    [switch]$UseCcache,

    [string]$RelativeFile = 'src/core/CMakeLists.txt',

    # 锚点行（去掉首尾空白后比较）
    [string]$Anchor = 'append_toolchain_setup(gnArgArg)',

    [switch]$AllowMissing
)

$ErrorActionPreference = 'Stop'

$BEGIN = '# >>> qtwebengine-build gn args (managed by scripts/qtwebengine/patch-gn-args.ps1) >>>'
$END = '# <<< qtwebengine-build gn args <<<'

$target = Join-Path $SourceRoot $RelativeFile
if (-not (Test-Path -LiteralPath $target)) {
    if ($AllowMissing) {
        Write-Host "[gn-args] 目标文件不存在，按 AllowMissing 跳过：$target"
        exit 0
    }
    Write-Error "[gn-args] 目标文件不存在：$target（源码未取全？要跳过请设 SKIP_PATCH=1）"
    exit 1
}

$raw = [System.IO.File]::ReadAllText($target)
# 保留原文件的换行风格：把整份文件改成 CRLF 会让后续 diff 全红，没必要
$eol = if ($raw.Contains("`r`n")) { "`r`n" } else { "`n" }
$lines = $raw -split "`r`n|`n"

# 先摘掉上一次注入的块（幂等）
$kept = New-Object System.Collections.Generic.List[string]
$inside = $false
foreach ($line in $lines) {
    if ($line.Trim() -eq $BEGIN) { $inside = $true; continue }
    if ($line.Trim() -eq $END) { $inside = $false; continue }
    if (-not $inside) { $kept.Add($line) }
}

# 组装本次要注入的内容
$inject = New-Object System.Collections.Generic.List[string]
if ($UseCcache) {
    $inject.Add('        cc_wrapper="ccache"')
}

$anchorIdx = -1
for ($i = 0; $i -lt $kept.Count; $i++) {
    if ($kept[$i].Trim() -eq $Anchor) {
        if ($anchorIdx -ge 0) {
            Write-Error "[gn-args] 锚点 '$Anchor' 出现多次，拒绝猜测"
            exit 1
        }
        $anchorIdx = $i
    }
}

if ($inject.Count -eq 0) {
    if ($kept.Count -ne $lines.Count) {
        Write-Host '[gn-args] 无注入内容，已清除既有注入块'
        # 只在真的摘掉了块时才写回：无条件重写会让「文件未改动」名不副实，
        # 而且会把混合行尾或带 BOM 的文件整份改写
        [System.IO.File]::WriteAllText($target, ($kept -join $eol), (New-Object System.Text.UTF8Encoding($false)))
    } else {
        Write-Host '[gn-args] 无注入内容，文件未改动'
    }
    exit 0
}

if ($anchorIdx -lt 0) {
    if ($AllowMissing) {
        Write-Host "[gn-args] 未找到锚点 '$Anchor'，按 AllowMissing 跳过（源码版本可能已变）"
        exit 0
    }
    Write-Error "[gn-args] 未找到锚点 '$Anchor'：${RelativeFile} 结构变了，需要人工核对"
    exit 1
}

$before = ($kept | Where-Object { $_ -match 'list\(APPEND gnArgArg' }).Count

$result = New-Object System.Collections.Generic.List[string]
for ($i = 0; $i -lt $kept.Count; $i++) {
    $result.Add($kept[$i])
    if ($i -eq $anchorIdx) {
        $result.Add('')
        $result.Add($BEGIN)
        $result.Add('    list(APPEND gnArgArg')
        foreach ($l in $inject) { $result.Add($l) }
        $result.Add('    )')
        $result.Add($END)
    }
}

$after = ($result | Where-Object { $_ -match 'list\(APPEND gnArgArg' }).Count
if ($after -ne $before + 1) {
    Write-Error "[gn-args] 注入后 list(APPEND gnArgArg 数量为 $after，预期 $($before + 1)"
    exit 1
}

[System.IO.File]::WriteAllText($target, ($result -join $eol), (New-Object System.Text.UTF8Encoding($false)))
Write-Host "[gn-args] 已注入到 ${RelativeFile}（锚点后）："
foreach ($l in $inject) { Write-Host "[gn-args]   $($l.Trim())" }
exit 0
