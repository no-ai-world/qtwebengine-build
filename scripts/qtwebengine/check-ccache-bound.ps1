<#
.SYNOPSIS
    Prove - in minutes instead of after five hours - that ccache reaches the compiler.

.DESCRIPTION
    A build round gets a 300 minute budget. If cc_wrapper never binds, that whole budget
    is compiled and then thrown away, which is exactly what happened before: args.gn
    carried cc_wrapper="ccache", 8038 targets were compiled, and the cache held 391
    bytes because Chromium only wires cc_wrapper for clang toolchains while Qt's MSVC
    build has is_clang=false (see patch-msvc-ccache.ps1).

    This script closes that hole in the prepare phase. It runs the GN generation step
    for the core module (about four minutes; the build phase then finds it up to date)
    and reads the ninja files GN wrote: the CC/CXX rules must name the wrapper. It is
    the strongest cheap evidence available before a single source file is compiled.

    QtWebEngine names that CMake target runGn_core_<config>_<arch> (GN_TARGET is
    "core_${config}_${arch}" in src/core/CMakeLists.txt), so the architecture is read
    from the build tree and the name is derived; extra candidates and a `--target help`
    lookup cover a rename. Not being able to tell is NOT a failure - see exit code 2.

    Exit codes:
      0 = the wrapper is in the generated rules (bound)
      1 = the rules were generated and no wrapper is in them (hard failure)
      2 = could not generate / could not tell (caller treats it as a warning; the
          end-of-round ccache check and the build-step watcher still cover this case)

.PARAMETER BuildDir
    CMake build directory (BUILD_DIR of build.cmd).

.PARAMETER BuildType
    Configuration to generate for (BUILD_TYPE of build.cmd).

.PARAMETER GnTarget
    Explicit CMake target that runs GN generation. Empty (default) derives
    runGn_core_<BuildType>_<arch> from the build tree.

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

if (-not (Test-Path -LiteralPath (Join-Path $BuildDir 'CMakeCache.txt'))) {
    Write-Host "[ccache-bound] 没有 CMakeCache.txt：$BuildDir（未 configure？）"
    exit 2
}

# 架构从构建树里读，而不是猜：src/core/<config>/<arch>/
$coreRoot = Join-Path $BuildDir 'src/core'
$arch = ''
$configDir = Join-Path $coreRoot $BuildType
if (Test-Path -LiteralPath $configDir) {
    $dirs = @(Get-ChildItem -LiteralPath $configDir -Directory -ErrorAction SilentlyContinue)
    if ($dirs.Count -ge 1) { $arch = $dirs[0].Name }
}

$candidates = New-Object System.Collections.Generic.List[string]
if ($GnTarget) { $candidates.Add($GnTarget) }
if ($arch) { $candidates.Add("runGn_core_${BuildType}_$arch") }
foreach ($a in @('AMD64', 'x64', 'ARM64')) { $candidates.Add("runGn_core_${BuildType}_$a") }
$candidates.Add('runGn_WebEngineCore')
$candidates = @($candidates | Select-Object -Unique)

$gnLog = Join-Path ([System.IO.Path]::GetTempPath()) ("ccache-bound-gn-{0}.log" -f $PID)
$generated = $false
$usedTarget = ''

foreach ($target in $candidates) {
    Write-Host "[ccache-bound] GN 生成：cmake --build --target $target --config $BuildType（约四分钟）"
    # 与构建阶段用的是同一个 target；生成完之后构建阶段会看到它已是最新，不浪费时间
    & cmake --build $BuildDir --config $BuildType --target $target *>&1 | Tee-Object -FilePath $gnLog -Append
    if ($LASTEXITCODE -eq 0) { $generated = $true; $usedTarget = $target; break }
    Write-Host "[ccache-bound] target '$target' 没能跑通（cmake 退出码 $LASTEXITCODE），试下一个"
}

if (-not $generated) {
    # 最后再问一次 CMake 有哪些 runGn_* 目标（上游改了命名也还能兜住）
    Write-Host '[ccache-bound] 候选 target 都不通，向 cmake 要一次目标列表'
    $help = & cmake --build $BuildDir --target help 2>&1
    $found = @($help | Select-String -Pattern 'runGn_\S+' -AllMatches |
        ForEach-Object { $_.Matches } | ForEach-Object { $_.Value } |
        Where-Object { $_ -match 'core' } | Select-Object -Unique)
    foreach ($target in $found) {
        if ($candidates -contains $target) { continue }
        Write-Host "[ccache-bound] 发现目标 $target，试它"
        & cmake --build $BuildDir --config $BuildType --target $target *>&1 | Tee-Object -FilePath $gnLog -Append
        if ($LASTEXITCODE -eq 0) { $generated = $true; $usedTarget = $target; break }
    }
}

if (-not $generated) {
    Write-Host '[ccache-bound] GN 生成没能跑通，本检查跳过（视为「无法判断」）；完整输出见：'
    Write-Host "  $gnLog"
    Get-Content -LiteralPath $gnLog -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
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
