<#
.SYNOPSIS
    Watchdog for the build phase: progress, silence, memory pressure and cache activity.

.DESCRIPTION
    The build phase gets a 300 minute budget, so a single stall can eat the whole round.
    That is not hypothetical: a round sat with the ninja counter frozen for a long time
    while the last line in the log was an ACTION that had already finished, and nothing
    in the log said whether the box was thrashing, whether mspdbsrv had wedged, or
    whether ccache was doing anything - the runner is a disposable VM, so there was no
    way to look afterwards either.

    This watchdog runs next to the build (started by the workflow step, which also
    collects its output file), writes one line every few minutes into the same stdout,
    and never touches the build itself. Each line answers the questions that were
    unanswerable last time: which ninja target finished last, how long the log has been
    silent, how much RAM is left, how many compilers are running and how big they are,
    and what ccache thinks it has done.

    It deliberately does not kill anything. Its one side effect is a sentinel file
    (ccache-not-bound.txt) when compiles are clearly happening but ccache reports zero
    cacheable calls: build.cmd reads that sentinel and records the round as failed
    instead of as a time-out, so a dead cache cannot re-dispatch itself forever.

.PARAMETER LogFile
    The build log being written by the workflow step (Tee-Object target).

.PARAMETER OutFile
    Optional second copy (an artifact the workflow uploads, so the lines survive even if
    interleaving with the live step log is lost).

.PARAMETER CcacheExe
    ccache.exe to query (left out when the round does not use ccache).

.PARAMETER WorkRoot
    Directory the sentinel file is written to (WORK_ROOT of build.cmd).

.PARAMETER IntervalSeconds
    How often to print a line.

.PARAMETER StallMinutes
    Log silence above this many minutes is reported as a stall.

.PARAMETER BindingGraceMinutes
    After this many minutes, a cache that has never seen a call is reported as broken.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$LogFile,

    [string]$OutFile = '',
    [string]$CcacheExe = '',
    [string]$WorkRoot = '',
    [int]$IntervalSeconds = 300,
    [int]$StallMinutes = 20,
    [int]$BindingGraceMinutes = 25
)

$ErrorActionPreference = 'Continue'
$start = Get-Date
$sentinel = if ($WorkRoot) { Join-Path $WorkRoot 'ccache-not-bound.txt' } else { '' }
$sentinelWritten = $false
$lastSignature = ''

function Write-Watch {
    param([string]$Text)
    $line = '[watch] ' + (Get-Date).ToString('HH:mm:ss') + ' ' + $Text
    Write-Host $line
    if ($OutFile) {
        try { Add-Content -LiteralPath $OutFile -Value $line -Encoding utf8 -ErrorAction Stop } catch { }
    }
}

Write-Watch ("start pid=$PID log=$LogFile every ${IntervalSeconds}s stall>=${StallMinutes}min")

while ($true) {
    try {
        $progress = 'n/a'
        $progressNum = -1
        $idleMin = -1.0
        $lastLine = ''

        if (Test-Path -LiteralPath $LogFile) {
            try { $idleMin = [math]::Round(((Get-Date) - (Get-Item -LiteralPath $LogFile).LastWriteTime).TotalMinutes, 1) } catch { }
            try {
                $tail = @(Get-Content -LiteralPath $LogFile -Tail 60 -ErrorAction Stop)
                $ninjaLines = @($tail | Where-Object { $_ -match '\[\d+/\d+\]' })
                if ($ninjaLines.Count -gt 0) {
                    $lastLine = $ninjaLines[-1].Trim()
                    if ($lastLine -match '\[(\d+)/(\d+)\]') { $progressNum = [int]$Matches[1]; $progress = "$($Matches[1])/$($Matches[2])" }
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

        $signature = "$progress|$lastLine"
        $changed = if ($signature -ne $lastSignature) { 'new' } else { 'unchanged' }
        $lastSignature = $signature

        Write-Watch "progress=$progress log-idle=${idleMin}min free-ram=${freeRam}GB $clText $otherText $ccacheText [$changed]"
        if ($lastLine) { Write-Watch "  last: $lastLine" }

        if ($idleMin -ge $StallMinutes -and $progressNum -gt 0) {
            Write-Watch "STALLED: no ninja line for ${idleMin}min at $progress - if this keeps up the round is lost; check cl/mspdbsrv memory above and the free RAM"
        }

        $elapsedMin = ((Get-Date) - $start).TotalMinutes
        if ($cacheable -eq 0 -and $progressNum -gt 300 -and $elapsedMin -gt $BindingGraceMinutes) {
            Write-Watch "ERROR: ccache has never been called after $([math]::Round($elapsedMin))min and $progressNum targets - the wrapper is not bound; this round cannot carry anything into the next one"
            if ($sentinel -and -not $sentinelWritten) {
                try {
                    Set-Content -LiteralPath $sentinel -Value "ccache was never called (progress=$progress)" -Encoding ascii -ErrorAction Stop
                    $sentinelWritten = $true
                    Write-Watch "wrote sentinel $sentinel"
                } catch { }
            }
        }
    } catch {
        Write-Watch "iteration failed (ignored): $_"
    }

    Start-Sleep -Seconds $IntervalSeconds
}
