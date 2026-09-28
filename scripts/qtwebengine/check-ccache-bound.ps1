<#
.SYNOPSIS
    Prove - in minutes instead of after five hours - that ccache reaches the compiler.

.DESCRIPTION
    A build round gets a 300 minute budget. If cc_wrapper never binds, that whole budget
    is compiled and then thrown away, which is exactly what happened before: args.gn
    carried cc_wrapper="ccache", 8038 targets were compiled, and the cache held 391
    bytes because Chromium only wires cc_wrapper for clang toolchains while Qt's MSVC
    build has is_clang=false (see patch-msvc-ccache.ps1).

    This script closes that hole in the prepare phase. It runs the GN generation step for
    the core module (about four minutes; the build phase then finds it up to date) and
    reads the ninja files GN wrote: the CC/CXX rules must name the wrapper.

    Naming the target is the fiddly part. QtWebEngine builds GN_TARGET as
    "core_${config}_${arch}" (src/core/CMakeLists.txt), and the generator here is
    Ninja Multi-Config, where the usable target is "<name>:<config>" - a plain
    "runGn_core_RelWithDebInfo_AMD64" answers "ninja: error: unknown target ..., did you
    mean 'runGn_core_RelWithDebInfo_AMD64:RelWithDebInfo'?". So candidates are tried in
    order and ninja's own suggestion is followed; a `--target help` lookup is the last
    resort. Not being able to tell is NOT a failure - see exit code 2.

    Exit codes:
      0 = the wrapper is in the generated rules (bound)
      1 = the rules were generated and no wrapper is in them (hard failure)
      2 = could not generate / could not tell (caller treats it as a warning; the
          build-step watcher reads the same rules again and can stop a dead round)

.PARAMETER BuildDir
    CMake build directory (BUILD_DIR of build.cmd).

.PARAMETER BuildType
    Configuration to generate for (BUILD_TYPE of build.cmd).

.PARAMETER GnTarget
    Explicit CMake target that runs GN generation. Empty (default) derives candidates
    from runGn_core_<BuildType>_<arch>.

.PARAMETER Wrapper
    The wrapper name that must appear in the generated rules.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BuildDir,

    [string]$BuildType = 'RelWithDebInfo',
    [string]$GnTarget = '',
    [string]$Wrapper = 'ccache'
)

$ErrorActionPreference = 'Continue'
$script:tried = New-Object System.Collections.Generic.HashSet[string]
$script:attempts = 0
$script:gnLog = Join-Path ([System.IO.Path]::GetTempPath()) ("ccache-bound-gn-{0}.log" -f $PID)

function Invoke-GnTarget {
    param([string]$Target)
    if (-not $script:tried.Add($Target)) { return $false }
    if ($script:attempts -ge 8) { return $false }
    $script:attempts++
    Write-Host "[ccache-bound] GN 生成：cmake --build --target $Target --config $BuildType（约四分钟）"
    # 与构建阶段用的是同一个 target；生成完之后构建阶段会看到它已是最新，不浪费时间
    # 注意：Tee-Object 会把输入继续往下传，若直接接在管道尾上，函数返回值就会被
    # cmake 的输出污染（非空数组恒为真），于是失败的尝试也会被当成成功。先接住再输出。
    $output = & cmake --build $BuildDir --config $BuildType --target $Target *>&1
    $output | Tee-Object -FilePath $script:gnLog -Append | Out-Null
    $ok = ($LASTEXITCODE -eq 0)
    if ($ok) { return $true }
    Write-Host "[ccache-bound] target '$Target' 没能跑通（cmake 退出码 $LASTEXITCODE）"
    return $false
}

function Get-SuggestedTarget {
    # ninja 会直接说出它认得的名字（Ninja Multi-Config 是 "<name>:<config>"）
    $hint = Select-String -LiteralPath $script:gnLog -Pattern "did you mean '([^']+)'" -AllMatches -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value } | Select-Object -Last 1
    return $hint
}

if (-not (Test-Path -LiteralPath (Join-Path $BuildDir 'CMakeCache.txt'))) {
    Write-Host "[ccache-bound] 没有 CMakeCache.txt：$BuildDir（未 configure？）"
    exit 2
}

# 架构从构建树里读（src/core/<config>/<arch>/），读不到再退回常见写法
$coreRoot = Join-Path $BuildDir 'src/core'
$arch = ''
$configDir = Join-Path $coreRoot $BuildType
if (Test-Path -LiteralPath $configDir) {
    $dirs = @(Get-ChildItem -LiteralPath $configDir -Directory -ErrorAction SilentlyContinue)
    if ($dirs.Count -ge 1) { $arch = $dirs[0].Name }
}

$names = New-Object System.Collections.Generic.List[string]
if ($GnTarget) { $names.Add($GnTarget) }
if ($arch) { $names.Add("runGn_core_${BuildType}_$arch") }
foreach ($a in @('AMD64', 'x64', 'ARM64')) { $names.Add("runGn_core_${BuildType}_$a") }
$names.Add('runGn_WebEngineCore')

# Ninja Multi-Config 的目标名带 :<config>，两种写法都排在前面
$candidates = New-Object System.Collections.Generic.List[string]
foreach ($n in ($names | Select-Object -Unique)) {
    $candidates.Add("${n}:$BuildType")
    $candidates.Add($n)
}

$generated = $false
$usedTarget = ''
$pending = New-Object System.Collections.Generic.List[string]
foreach ($c in $candidates) { $pending.Add($c) }

while ($pending.Count -gt 0 -and -not $generated) {
    $target = $pending[0]
    $pending.RemoveAt(0)
    if ($script:tried.Contains($target)) { continue }
    if (Invoke-GnTarget -Target $target) {
        $generated = $true
        $usedTarget = $target
        break
    }
    # ninja 的建议要插到队首：不然它会排在剩下所有猜测之后，甚至轮不到
    $hint = Get-SuggestedTarget
    if ($hint -and -not $script:tried.Contains($hint)) {
        Write-Host "[ccache-bound] ninja 建议的目标名：'$hint'，先试它"
        $pending.Insert(0, $hint)
    }
}

if (-not $generated) {
    # 最后再问一次 CMake 有哪些 runGn_* 目标（上游改了命名也还能兜住）
    Write-Host '[ccache-bound] 候选 target 都不通，向 cmake 要一次目标列表'
    $help = & cmake --build $BuildDir --target help 2>&1
    $found = @($help | Select-String -Pattern 'runGn_\S+' -AllMatches |
        ForEach-Object { $_.Matches } | ForEach-Object { $_.Value.TrimEnd(':') } |
        Where-Object { $_ -match 'core' } | Select-Object -Unique)
    foreach ($target in $found) {
        foreach ($variant in @("${target}:$BuildType", $target)) {
            if (Invoke-GnTarget -Target $variant) { $generated = $true; $usedTarget = $variant; break }
        }
        if ($generated) { break }
    }
}

if (-not $generated) {
    Write-Host '[ccache-bound] GN 生成没能跑通，本检查跳过（视为「无法判断」）；完整输出见：'
    Write-Host "  $script:gnLog"
    Get-Content -LiteralPath $script:gnLog -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
    exit 2
}

Write-Host "[ccache-bound] GN 生成完成（target $usedTarget）"

if (-not (Test-Path -LiteralPath $coreRoot)) {
    Write-Host "[ccache-bound] 没有 $coreRoot，无法判断"
    exit 2
}

$ninjaFiles = @(Get-ChildItem -LiteralPath $coreRoot -Recurse -Filter '*.ninja' -File -ErrorAction SilentlyContinue)
if ($ninjaFiles.Count -eq 0) {
    Write-Host "[ccache-bound] $coreRoot 下没有 .ninja 文件，无法判断"
    exit 2
}

$hit = $null
foreach ($f in $ninjaFiles) {
    $m = Select-String -LiteralPath $f.FullName -Pattern $Wrapper -SimpleMatch -List -ErrorAction SilentlyContinue
    if ($m) { $hit = $m; break }
}

if (-not $hit) {
    Write-Host "[ccache-bound] 扫了 $($ninjaFiles.Count) 个 .ninja 文件，没有一处提到 '$Wrapper'"
    Write-Host '[ccache-bound] 说明 cc_wrapper 没被拼进编译器命令行：ccache 一次都不会被调用'
    Write-Host '[ccache-bound] 检查 patch-msvc-ccache.ps1（win/toolchain.gni）与 patch-gn-args.ps1（args.gn）'
    exit 1
}

Write-Host "[ccache-bound] 已确认：$($hit.Path)"
Write-Host "[ccache-bound]   $($hit.Line.Trim())"
exit 0
