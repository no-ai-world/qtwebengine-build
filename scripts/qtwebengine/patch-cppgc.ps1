# 应用 v8/cppgc 补丁：MSVC 14.44（VS 2022 17.14）编 Qt 6.8.3 的 V8 会报
#
#   error C2352: 'cppgc::internal::MarkingStateBase::MarkNoPush':
#                a call of a non-static member function requires an object
#
# 参考手册（WebEngineMP4Build_6.8.3.md）用 `git apply` 打这个补丁，但那份 diff 只有
# 增删两行、没有上下文行，行号一漂就失效；这里改为按「整条语句」精确替换，只依赖那一行
# 的内容而不依赖行号。两者语义等价（都是取基类成员），因此即便上游某天自己修好了，
# 本脚本找不到目标模式也只是安静跳过，不会把树改坏。
#
# 幂等：已修好则跳过；目标文件不存在且 -AllowMissing 时跳过；其余异常一律非零退出。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceRoot,

    [string]$RelativeFile = 'src/3rdparty/chromium/v8/src/heap/cppgc/marking-state.h',

    # 上游若已修复、文件改版，用这个开关把「找不到目标」降级为跳过
    [switch]$AllowMissing
)

$ErrorActionPreference = 'Stop'

$target = Join-Path $SourceRoot $RelativeFile
if (-not (Test-Path -LiteralPath $target)) {
    if ($AllowMissing) {
        Write-Host "[patch] 目标文件不存在，按 AllowMissing 跳过：$target"
        exit 0
    }
    Write-Error "[patch] 目标文件不存在：$target（源码或子模块未取全？要跳过补丁请设 SKIP_PATCH=1）"
    exit 1
}

$text = Get-Content -LiteralPath $target -Raw -Encoding UTF8

$brokenCall = 'MutatorMarkingState::BasicMarkingState::MarkNoPush(header);'
$fixedCall = 'this->BasicMarkingState::MarkNoPush(header);'
$brokenStmt = "return $brokenCall"
$fixedStmt = "return $fixedCall"

if ($text.Contains($fixedStmt)) {
    Write-Host '[patch] 已是修补后形态，跳过'
    exit 0
}

# 用 .NET 的序号匹配数数，避免正则把 :: 之类当元字符
$hits = ([regex]::Matches($text, [regex]::Escape($brokenStmt))).Count
if ($hits -eq 0) {
    Write-Host '[patch] 未找到待修补语句（上游可能已自行修复），跳过'
    exit 0
}
if ($hits -gt 1) {
    Write-Error "[patch] 待修补语句出现 $hits 次，预期 1 次；拒绝猜测，请人工确认"
    exit 1
}

$updated = $text.Replace($brokenStmt, $fixedStmt)
if ($updated -eq $text) {
    Write-Error '[patch] 替换未生效（内容未变化）'
    exit 1
}

# Chromium 源码无 BOM；用无 BOM 的 UTF8 写回，行尾与其余内容原样保留。
# 走 .NET 而不是 Set-Content -Encoding：后者在 Windows PowerShell 5.1 上没有 utf8NoBOM
# （本脚本可能被 5.1 调用），而带 BOM 的改动会被 Chromium 的 lint 视作污染。
[System.IO.File]::WriteAllText($target, $updated, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "[patch] 已修补 $RelativeFile"
Write-Host "[patch]   - $brokenStmt"
Write-Host "[patch]   + $fixedStmt"
exit 0
