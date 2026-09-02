<#
.SYNOPSIS
    Windows wrapper for devapps.py -- the App Launcher CLI.

.DESCRIPTION
    The engine is Python now, so one implementation serves Windows, Linux and
    macOS. This wrapper stays because a scheduled task may already point at it,
    and because `.\devapps.ps1 status` is what fingers remember.

    Arguments pass straight through, with -Json translated to --json.

.EXAMPLE
    .\devapps.ps1 status
.EXAMPLE
    .\devapps.ps1 start myapp
.EXAMPLE
    .\devapps.ps1 install
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$py = $null
foreach ($candidate in 'python', 'python3', 'py') {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $py = $found.Source; break }
}
if (-not $py) {
    Write-Host "Python was not found on PATH. Install Python 3 and try again." -ForegroundColor Red
    exit 1
}

# -Json is the PowerShell spelling; argparse wants --json.
$passthru = @()
foreach ($a in $args) {
    if ($a -is [string] -and $a -match '^-Json$') { $passthru += '--json' }
    else { $passthru += $a }
}

& $py (Join-Path $root 'devapps.py') @passthru
exit $LASTEXITCODE
