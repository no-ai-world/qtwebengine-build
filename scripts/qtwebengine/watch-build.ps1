<#
.SYNOPSIS
    Watchdog for the build phase: progress, silence, memory pressure and cache activity.

.DESCRIPTION
    The build phase gets a 300 minute budget, so a single stall - or a round that cannot
    leave anything behind - can eat the whole thing. Neither is hypothetical: a round sat
    for a long time with the ninja counter frozen (the last log line was an ACTION that had
    already finished), and every round before the ccache wiring was fixed compiled for
    hours into a cache that was never called. The runner is a disposable VM, so there was
    no way to look afterwards either.

    This watchdog runs next to the build (started by the workflow step, which also
    collects its output file) and writes one line every few minutes into the same stdout.
    Each line answers the questions that were unanswerable last time: which ninja target
    finished last, how long the log has been silent, how much RAM is left, how many
    compilers are running and how big they are, what ccache thinks it has done, and -
    the decisive one - whether the wrapper is present in the ninja rules GN generated
    (read straight from src/core, so it does not depend on a CMake target name).

    Two side effects, both aimed at not wasting a round on something already known:

      * a sentinel file (ccache-not-bound.txt) when compiles are clearly happening and
        ccache reports zero calls. build.cmd reads it and records the round as failed
        instead of as a time-out, so a dead cache cannot re-dispatch itself forever;
      * with -AbortOnDeadCache, and only when the generated rules provably do NOT mention
        the wrapper AND the projected total build time exceeds the budget, it stops the
        compiler processes: such a round can neither finish nor leave a cache entry, so
        ending it in minutes is strictly better than ending it in five hours. A bound
        cache shows up in the stats within seconds of the first compile, and the missing
        wrapper is read from the generated rules, so the two signals cannot both be
        wrong about a working cache.

.PARAMETER LogFile
    The build log being written by the workflow step (Tee-Object target).

.PARAMETER OutFile
    Optional second copy (an artifact the workflow uploads, so the lines survive even if
    interleaving with the live step log is lost).

.PARAMETER CcacheExe
    ccache.exe to query (left out when the round does not use ccache).

.PARAMETER WorkRoot
    Directory the sentinel file is written to (WORK_ROOT of build.cmd).

.PARAMETER BuildDir
    CMake build directory (BUILD_DIR of build.cmd). When given, the watchdog reads the
    generated ninja rules under src/core and reports whether the wrapper is in them.

.PARAMETER Wrapper
    Wrapper name to look for in the generated rules.

.PARAMETER BudgetMinutes
    The build step's time budget. Used only to decide whether a dead-cache round could
    still have finished (projected total = elapsed / progress * total).

.PARAMETER IntervalSeconds
    How often to print a line.

.PARAMETER StallMinutes
    Log silence above this many minutes is reported as a stall.

.PARAMETER BindingGraceMinutes
    How long a cache may legitimately show no calls before "zero calls" means broken.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$LogFile,

    [string]$OutFile = '',
    [string]$CcacheExe = '',
    [string]$WorkRoot = '',
    [string]$BuildDir = '',
    [string]$Wrapper = 'ccache',
    [int]$BudgetMinutes = 0,
    [int]$IntervalSeconds = 300,
    [int]$StallMinutes = 20,
    [int]$BindingGraceMinutes = 25,
    [switch]$AbortOnDeadCache
)

$ErrorActionPreference = 'Continue'
$start = Get-Date
$sentinel = if ($WorkRoot) { Join-Path $WorkRoot 'ccache-not-bound.txt' } else { '' }
$sentinelWritten = $false
$lastSignature = ''
$lastWrapperReport = ''
$wrapperMissing = $false
$wrapperBound = $false
$reportedNoCalls = $false
$aborted = $false

function Write-Watch {
    param([string]$Text)
    $line = '[watch] ' + (Get-Date).ToString('HH:mm:ss') + ' ' + $Text
    Write-Host $line
    if ($OutFile) {
        try { Add-Content -LiteralPath $OutFile -Value $line -Encoding utf8 -ErrorAction Stop } catch { }
    }
}

Write-Watch ("start pid=$PID log=$LogFile every ${IntervalSeconds}s stall>=${StallMinutes}min budget=${BudgetMinutes}min abort=$($AbortOnDeadCache.IsPresent)")

while ($true) {
    try {
        $progress = 'n/a'
        $progressNum = -1
        $totalEdges = -1
        $idleMin = -1.0
        $lastLine = ''

        if (Test-Path -LiteralPath $LogFile) {
            try { $idleMin = [math]::Round(((Get-Date) - (Get-Item -LiteralPath $LogFile).LastWriteTime).TotalMinutes, 1) } catch { }
            try {
                $tail = @(Get-Content -LiteralPath $LogFile -Tail 60 -ErrorAction Stop)
                $ninjaLines = @($tail | Where-Object { $_ -match '\[\d+/\d+\]' })
                if ($ninjaLines.Count -gt 0) {
                    $lastLine = $ninjaLines[-1].Trim()
                    if ($lastLine -match '\[(\d+)/(\d+)\]') {
                        $progressNum = [int]$Matches[1]
                        $totalEdges = [int]$Matches[2]
                        $progress = "$($Matches[1])/$($Matches[2])"
                    }
                }
            } catch { }
        }

        $freeRam = '?'
        $clText = 'cl=?'
        $otherText = ''
        try {
            $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
            $freeRam = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
        } catch { }
        try {
            $cl = @(Get-Process -Name cl -ErrorAction SilentlyContinue)
            if ($cl.Count -gt 0) {
                $clGb = [math]::Round((($cl | Measure-Object -Property WorkingSet64 -Sum).Sum) / 1GB, 1)
                $clText = "cl=$($cl.Count)(${clGb}GB)"
            } else {
                $clText = 'cl=0'
            }
            $bits = @()
            foreach ($n in @('mspdbsrv', 'ninja', 'link', 'ccache')) {
                $p = @(Get-Process -Name $n -ErrorAction SilentlyContinue)
                if ($p.Count -gt 0) {
                    $gb = [math]::Round((($p | Measure-Object -Property WorkingSet64 -Sum).Sum) / 1GB, 1)
                    $bits += "$n=$($p.Count)(${gb}GB)"
                }
            }
            $otherText = ($bits -join ' ')
        } catch { }

        $ccacheText = ''
        $cacheable = $null
        if ($CcacheExe -and (Test-Path -LiteralPath $CcacheExe)) {
            try {
                $stats = & $CcacheExe --show-stats 2>$null
                $mc = $stats | Select-String -Pattern 'Cacheable calls:\s*(\d+)' | Select-Object -First 1
                if ($mc) { $cacheable = [int]$mc.Matches[0].Groups[1].Value }
                $mh = $stats | Select-String -Pattern '^\s*Hits:\s*(\d+)' | Select-Object -First 1
                $hits = if ($mh) { $mh.Matches[0].Groups[1].Value } else { '?' }
                $ms = $stats | Select-String -Pattern 'Cache size \(GB\):\s*([\d.]+)' | Select-Object -First 1
                $size = if ($ms) { $ms.Matches[0].Groups[1].Value } else { '?' }
                $ccacheText = "ccache(cacheable=$cacheable hits=$hits size=${size}GB)"
            } catch { $ccacheText = 'ccache(?)' }
        }

        # wrapper 是否出现在 GN 生成的 ninja 规则里：这是「ccache 会不会被调用」的决定性证据
        $wrapperReport = ''
        if ($BuildDir) {
            $core = Join-Path $BuildDir 'src/core'
            if (Test-Path -LiteralPath $core) {
                $ninja = @(Get-ChildItem -LiteralPath $core -Recurse -Filter '*.ninja' -File -ErrorAction SilentlyContinue)
                if ($ninja.Count -gt 0) {
                    $newest = ($ninja | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
                    if (((Get-Date) - $newest).TotalSeconds -gt 60) {
                        $hit = $false
                        foreach ($f in $ninja) {
                            if (Select-String -LiteralPath $f.FullName -Pattern $Wrapper -SimpleMatch -List -ErrorAction SilentlyContinue) { $hit = $true; break }
                        }
                        $wrapperMissing = -not $hit
                        $wrapperBound = $hit
                        $wrapperReport = if ($hit) { "wrapper=$Wrapper BOUND" } else { "wrapper=$Wrapper MISSING in $($ninja.Count) ninja file(s)" }
                    } else {
                        $wrapperReport = 'wrapper check waits for gn to finish writing'
                    }
                }
            }
        }

        $signature = "$progress|$lastLine"
        $changed = if ($signature -ne $lastSignature) { 'new' } else { 'unchanged' }
        $lastSignature = $signature
        Write-Watch "progress=$progress log-idle=${idleMin}min free-ram=${freeRam}GB $clText $otherText $ccacheText [$changed]"
        if ($lastLine) { Write-Watch "  last: $lastLine" }
        if ($wrapperReport -and $wrapperReport -ne $lastWrapperReport) {
            Write-Watch "  $wrapperReport"
            $lastWrapperReport = $wrapperReport
            # 也发一条 job annotation：步骤日志在作业结束前拿不到，而 annotation 在
            # check run 上边跑边能查（gh api .../check-runs/<id>/annotations），
            # 这样「缓存到底有没有接上」不必等五个小时。
            Write-Host "::notice title=ccache binding::$wrapperReport; $ccacheText; progress=$progress"
        }

        if ($idleMin -ge $StallMinutes -and $progressNum -gt 0) {
            Write-Watch "STALLED: no ninja line for ${idleMin}min at $progress - if this keeps up the round is lost; check the free RAM and cl/mspdbsrv above"
        }

        $elapsedMin = ((Get-Date) - $start).TotalMinutes

        # 「编译在进行、ccache 却一次没被调用」= 这一轮留不下任何缓存
        $noCacheCalls = ($cacheable -eq 0 -and $progressNum -gt 200 -and $elapsedMin -gt $BindingGraceMinutes)
        if ($noCacheCalls) {
            $why = if ($wrapperMissing) { "the generated rules do not mention $Wrapper" }
                   elseif ($wrapperBound) { "the rules do mention $Wrapper, so it is invoked but counts nothing cacheable" }
                   else { 'binding unknown' }
            if (-not $reportedNoCalls) {
                Write-Watch "ERROR: ccache reported no calls after $([math]::Round($elapsedMin))min and $progressNum targets - $why"
            }
            $reportedNoCalls = $true
            if ($sentinel -and -not $sentinelWritten) {
                try {
                    Set-Content -LiteralPath $sentinel -Value "ccache was never called (progress=$progress, $wrapperReport)" -Encoding ascii -ErrorAction Stop
                    $sentinelWritten = $true
                    Write-Watch "wrote sentinel $sentinel (build.cmd turns this round into 'failed')"
                    Write-Host "::warning title=ccache never called::$why; progress=$progress; free-ram=${freeRam}GB"
                } catch { }
            }
        }

        # 只有「缓存肯定不会有」且「这一轮按当前速率也编不完」时才停手：否则继续跑还有意义
        if ($noCacheCalls -and $wrapperMissing -and $AbortOnDeadCache -and -not $aborted) {
            if ($BudgetMinutes -gt 0 -and $progressNum -gt 0 -and $totalEdges -gt 0) {
                $projected = $elapsedMin * ($totalEdges / $progressNum)
                if ($projected -gt $BudgetMinutes) {
                    $aborted = $true
                    Write-Watch ("ABORT: dead cache (rules have no $Wrapper) and projected total {0:N0}min > budget {1}min - stopping the compilers, build.cmd will record this round as failed" -f $projected, $BudgetMinutes)
                    foreach ($n in @('ninja', 'cl', 'ccache', 'link', 'mspdbsrv')) {
                        try { Get-Process -Name $n -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue } catch { }
                    }
                } else {
                    Write-Watch ("dead cache, but projected total {0:N0}min fits the {1}min budget - letting it run" -f $projected, $BudgetMinutes)
                }
            } else {
                Write-Watch 'dead cache, but no budget/progress to project from - not aborting'
            }
        }
    } catch {
        Write-Watch "iteration failed (ignored): $_"
    }

    Start-Sleep -Seconds $IntervalSeconds
}
