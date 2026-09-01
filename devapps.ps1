<#
.SYNOPSIS
    Start, stop and check the local apps listed in apps.json.

.DESCRIPTION
    One place to launch everything that otherwise has to be started by hand
    after a reboot.

    Liveness is judged in three steps, most reliable first:
      1. a process whose COMMAND LINE matches the app's `match` pattern
      2. the app's listening PORT, unless that listener belongs to another
         registered app (an app that also binds https_port + 1 for a redirect
         would otherwise report whatever is registered on that port as running)
      3. the PID recorded at launch

    Step 1 exists because a launcher process can exit after spawning the real
    one -- a .vbs or .cmd wrapper does exactly that -- which makes a recorded
    PID worthless within milliseconds.

    Starting is idempotent: an app already running is left alone, so running
    this twice never gives you two copies fighting over a port.

.EXAMPLE
    .\devapps.ps1 status
.EXAMPLE
    .\devapps.ps1 start                 # every app with autostart:true
.EXAMPLE
    .\devapps.ps1 start myapp           # just one, autostart flag ignored
.EXAMPLE
    .\devapps.ps1 restart myapp
.EXAMPLE
    .\devapps.ps1 install               # run at logon from now on
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'start', 'stop', 'restart', 'logs', 'install', 'uninstall')]
    [string]$Action = 'status',

    [Parameter(Position = 1)]
    [string]$Name,

    # start/restart: also start apps marked autostart:false
    [switch]$All,

    # status: emit machine-readable JSON instead of a table. The web UI calls
    # this so liveness has exactly one implementation, not two that drift.
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$DevRoot  = Split-Path -Parent $Root
$LogDir   = Join-Path $Root 'logs'
$StateDir = Join-Path $Root 'state'
$TaskName   = 'App Launcher at logon'
# Registered under this name before the project was renamed; install and
# uninstall both clear it so a machine can't end up running two tasks.
$LegacyTask = 'DevApps at logon'

foreach ($d in @($LogDir, $StateDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

function Get-Apps {
    $cfg = Get-Content (Join-Path $Root 'apps.json') -Raw | ConvertFrom-Json
    $apps = $cfg.apps
    if ($Name) {
        $apps = $apps | Where-Object { $_.name -eq $Name }
        if (-not $apps) { throw "No app named '$Name' in apps.json" }
    }
    return $apps
}

function Get-MatchedProcess {
    param($App)
    # The reliable signal: a process whose command line names this app's
    # script. Ports lie -- an app that binds a second port for an HTTP->HTTPS
    # redirect would otherwise be credited with whatever is registered there.
    if (-not $App.match) { return $null }
    $hit = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -and $_.CommandLine -match $App.match } |
           Select-Object -First 1
    if ($hit) { return $hit.ProcessId }
    return $null
}

function Get-ClaimedPids {
    # PIDs already accounted for by some OTHER app, so a shared port cannot be
    # miscredited to an app that is not actually running.
    param([string]$ExceptName)
    $claimed = @()
    $cfg = Get-Content (Join-Path $Root 'apps.json') -Raw | ConvertFrom-Json
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine }
    foreach ($a in $cfg.apps) {
        if ($a.name -eq $ExceptName) { continue }
        if (-not $a.match) { continue }
        # EVERY matching process, not just the first: an app can fork children
        # (a redirect listener often runs in a second process) and any of them
        # may hold the port another app claims.
        foreach ($proc in $all) {
            if ($proc.CommandLine -match $a.match) { $claimed += $proc.ProcessId }
        }
    }
    return $claimed
}

function Get-PortOwner {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return ($conn | Select-Object -First 1).OwningProcess
    } catch {
        return $null
    }
}

function Get-RecordedPid {
    param($App)
    # The weakest signal, and the only one that can name the WRONG process:
    # Windows recycles PIDs, so a stale file can point at something unrelated.
    # That misreports the app as running and, worse, makes `start` skip it as
    # already up -- which is exactly how a dead app stays dead while holding
    # another process's recycled PID.
    #
    # An app with a `match` pattern never needs this step: we launch with full
    # paths precisely so step 1 can see it, so if step 1 found nothing the app
    # is not running and the file is stale.
    if ($App.match) {
        Remove-Item (Join-Path $StateDir ($App.name + '.pid')) -ErrorAction SilentlyContinue
        return $null
    }
    $f = Join-Path $StateDir ($App.name + '.pid')
    if (-not (Test-Path $f)) { return $null }
    $recorded = (Get-Content $f -Raw).Trim()
    if (-not $recorded) { return $null }
    try {
        $proc = Get-Process -Id ([int]$recorded) -ErrorAction Stop
        # Even without a match pattern, a PID another registered app answers
        # for is not ours.
        if ((Get-ClaimedPids -ExceptName $App.name) -contains $proc.Id) {
            Remove-Item $f -ErrorAction SilentlyContinue
            return $null
        }
        return $proc.Id
    } catch {
        # Stale file from a process that died or a pre-reboot PID that now
        # belongs to something unrelated.
        Remove-Item $f -ErrorAction SilentlyContinue
        return $null
    }
}

function Get-AppState {
    param($App)

    # 1. Command-line match -- authoritative, and survives a launcher process
    #    that exits after spawning (a .vbs or .cmd wrapper does exactly that).
    $matched = Get-MatchedProcess -App $App
    if ($matched) {
        return [pscustomobject]@{ Running = $true; ProcessId = $matched; By = 'match' }
    }

    # 2. Port, but only if the listener is not another app's process. Covers
    #    an app started by hand outside the launcher.
    if ($App.port) {
        $owner = Get-PortOwner -Port $App.port
        if ($owner) {
            $claimed = Get-ClaimedPids -ExceptName $App.name
            if ($claimed -notcontains $owner) {
                return [pscustomobject]@{ Running = $true; ProcessId = $owner; By = 'port' }
            }
        }
    }

    # 3. Recorded PID, for anything with neither a match pattern nor a port.
    $recorded = Get-RecordedPid -App $App
    if ($recorded) { return [pscustomobject]@{ Running = $true; ProcessId = $recorded; By = 'pid' } }
    return [pscustomobject]@{ Running = $false; ProcessId = $null; By = 'none' }
}

function Start-App {
    param($App)
    $state = Get-AppState -App $App
    if ($state.Running) {
        Write-Host ("  {0,-13} already running (pid {1})" -f $App.name, $state.ProcessId) -ForegroundColor DarkGray
        return
    }

    # `dir` is normally a folder name under dev/, but an absolute path is
    # allowed so an app living outside dev/ can still be registered. Join-Path
    # would mangle an absolute second argument, hence the explicit test.
    $workdir = if ([System.IO.Path]::IsPathRooted($App.dir)) { $App.dir }
               else { Join-Path $DevRoot $App.dir }
    if (-not (Test-Path $workdir)) {
        Write-Host ("  {0,-13} SKIPPED - {1} not found" -f $App.name, $workdir) -ForegroundColor Yellow
        return
    }

    $outLog = Join-Path $LogDir ($App.name + '.log')
    $errLog = Join-Path $LogDir ($App.name + '.err.log')
    $splat = @{
        FilePath         = $App.command
        WorkingDirectory = $workdir
        WindowStyle      = 'Hidden'
        PassThru         = $true
    }
    if ($App.args) {
        # Expand a script argument to its full path: the command line is what
        # Get-MatchedProcess reads, and 'app.py' alone is not identifiable.
        $expanded = foreach ($a in $App.args) {
            $candidate = Join-Path $workdir $a
            if (Test-Path $candidate) { $candidate } else { $a }
        }
        $splat.ArgumentList = $expanded
    }
    # wscript launches its own host window and refuses stream redirection.
    if ($App.command -notmatch 'wscript') {
        $splat.RedirectStandardOutput = $outLog
        $splat.RedirectStandardError  = $errLog
    }

    try {
        $proc = Start-Process @splat
    } catch {
        Write-Host ("  {0,-13} FAILED - {1}" -f $App.name, $_.Exception.Message) -ForegroundColor Red
        return
    }

    Set-Content -Path (Join-Path $StateDir ($App.name + '.pid')) -Value $proc.Id -Encoding ascii

    if ($App.port) {
        # Confirm it actually bound rather than dying on startup - a crash
        # loop should be reported here, not discovered later in the browser.
        $bound = $false
        foreach ($i in 1..20) {
            Start-Sleep -Milliseconds 500
            if (Get-PortOwner -Port $App.port) { $bound = $true; break }
        }
        if ($bound) {
            Write-Host ("  {0,-13} started (pid {1}) -> {2}" -f $App.name, $proc.Id, $App.url) -ForegroundColor Green
        } else {
            Write-Host ("  {0,-13} started but nothing on port {1} - see {2}" -f $App.name, $App.port, $errLog) -ForegroundColor Red
        }
    } else {
        Write-Host ("  {0,-13} started (pid {1})" -f $App.name, $proc.Id) -ForegroundColor Green
    }
}

function Stop-App {
    param($App)
    $state = Get-AppState -App $App
    if (-not $state.Running) {
        Write-Host ("  {0,-13} not running" -f $App.name) -ForegroundColor DarkGray
        return
    }
    try {
        Stop-Process -Id $state.ProcessId -Force -ErrorAction Stop
        Write-Host ("  {0,-13} stopped (pid {1})" -f $App.name, $state.ProcessId) -ForegroundColor Yellow
    } catch {
        Write-Host ("  {0,-13} could not stop pid {1}: {2}" -f $App.name, $state.ProcessId, $_.Exception.Message) -ForegroundColor Red
    }
    Remove-Item (Join-Path $StateDir ($App.name + '.pid')) -ErrorAction SilentlyContinue
}

function Show-Status {
    $rows = foreach ($app in Get-Apps) {
        $state = Get-AppState -App $app
        [pscustomobject]@{
            App       = $app.name
            Status    = if ($state.Running) { 'running' } else { 'stopped' }
            PID       = $state.ProcessId
            Via       = $state.By
            Port      = $app.port
            Autostart = $app.autostart
            Url       = $app.url
        }
    }
    if ($Json) {
        # -Compress keeps it one line; wrapping in a literal array means a
        # single app still decodes as a list on the Python side.
        ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
    } else {
        $rows | Format-Table -AutoSize
    }
}

switch ($Action) {

    'status' { Show-Status }

    'start' {
        Write-Host "Starting apps..." -ForegroundColor Cyan
        foreach ($app in Get-Apps) {
            # An explicitly named app starts regardless of its autostart flag.
            if ($Name -or $All -or $app.autostart) { Start-App -App $app }
        }
        Write-Host ''
        Show-Status
    }

    'stop' {
        Write-Host "Stopping apps..." -ForegroundColor Cyan
        foreach ($app in Get-Apps) { Stop-App -App $app }
    }

    'restart' {
        foreach ($app in Get-Apps) {
            if ($Name -or $All -or $app.autostart) {
                Stop-App -App $app
                Start-Sleep -Milliseconds 700
                Start-App -App $app
            }
        }
        Write-Host ''
        Show-Status
    }

    'logs' {
        foreach ($app in Get-Apps) {
            $f = Join-Path $LogDir ($app.name + '.log')
            $e = Join-Path $LogDir ($app.name + '.err.log')
            Write-Host ("=== {0} ===" -f $app.name) -ForegroundColor Cyan
            foreach ($file in @($f, $e)) {
                if (Test-Path $file) {
                    $tail = Get-Content $file -Tail 12 -ErrorAction SilentlyContinue
                    if ($tail) { Write-Host ("--- {0}" -f (Split-Path $file -Leaf)) -ForegroundColor DarkGray; $tail }
                }
            }
        }
    }

    'install' {
        $ps1 = Join-Path $Root 'devapps.ps1'
        $act = New-ScheduledTaskAction -Execute 'powershell.exe' `
                 -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" start' -f $ps1)
        $trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        # A dev box is not always online the instant you log in; a short delay
        # keeps a Flask app from starting before the network stack is ready,
        # a cold boot otherwise produces DNS failures in apps that poll on
        # startup.
        $trg.Delay = 'PT30S'
        $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                 -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg `
            -Settings $set -Description 'Starts the local apps listed in apps.json' -Force | Out-Null
        Write-Host "Registered scheduled task '$TaskName' (runs 30s after logon)." -ForegroundColor Green
        try {
            Unregister-ScheduledTask -TaskName $LegacyTask -Confirm:$false -ErrorAction Stop
            Write-Host "Removed the older '$LegacyTask' task." -ForegroundColor DarkGray
        } catch { }
    }

    'uninstall' {
        $removed = $false
        foreach ($t in @($TaskName, $LegacyTask)) {
            try {
                Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop
                Write-Host "Removed scheduled task '$t'." -ForegroundColor Yellow
                $removed = $true
            } catch { }
        }
        if (-not $removed) {
            Write-Host "No scheduled task to remove." -ForegroundColor DarkGray
        }
    }
}
