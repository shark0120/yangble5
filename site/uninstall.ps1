# yangble5 client uninstaller - Windows PowerShell 5.1+
#
#   irm https://yangble5.com/uninstall.ps1 | iex
#   & ([scriptblock]::Create((irm https://yangble5.com/uninstall.ps1))) -Yes
#   powershell -NoProfile -File .\uninstall.ps1 -DryRun
#
# It prints every path it is about to delete BEFORE deleting anything, and it
# deletes nothing else. Specifically it will NOT touch:
#   * %USERPROFILE%\.claude or .codex - your normal logins were never involved
#   * your PowerShell profile or the registry
#   * anything outside %USERPROFILE%\.yangble5
#
# The per-user PATH entry, if you added one with install.ps1 -AddToPath, is
# reported and removed ONLY when it points at .yangble5\bin. Nothing else in
# your PATH is touched.
#
# Deleting the key here removes it from THIS MACHINE only. The server still
# has its hash and the key still works if someone else has a copy. If you
# think it leaked, ask the operator to revoke it server-side as well.
#
# Refuses to run elevated, for the same reason the installer does.
#
# EXIT CODES
#   0  removed (or nothing was there to remove)
#   1  bad arguments, or refused because no confirmation was given
#   2  refused: running elevated
#   3  %USERPROFILE% is not usable
#
# PowerShell 5.1 compatible: no '??', no '?:', no '&&'/'||' chains.
#
# SPDX-License-Identifier: MIT

[CmdletBinding()]
param(
    [switch] $Yes,
    [switch] $DryRun,
    [switch] $Help
)

$ErrorActionPreference = 'Stop'

if ($Help) {
    Write-Host @'
usage: uninstall.ps1 [options]

  -Yes        do not prompt (required when there is no interactive console)
  -DryRun     print exactly what would be deleted, then stop
  -Help       this text
'@
    exit 0
}

# --- refuse to run elevated ------------------------------------------------
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
if ($principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ''
    Write-Host 'REFUSING TO RUN ELEVATED.' -ForegroundColor Red
    Write-Host @'

Everything this script deletes lives in a normal user's profile directory. As
Administrator it may resolve a different profile and either delete the wrong
thing or nothing at all. Run it as the user who installed yangble5.

'@
    exit 2
}

$userHome = $env:USERPROFILE
if ([string]::IsNullOrWhiteSpace($userHome)) { $userHome = $HOME }
if ([string]::IsNullOrWhiteSpace($userHome) -or (-not (Test-Path -LiteralPath $userHome))) {
    Write-Host '%USERPROFILE% is not set or not a directory; refusing to guess.' -ForegroundColor Red
    exit 3
}

$yb5Home = Join-Path $userHome '.yangble5'
$yb5Bin  = Join-Path $yb5Home 'bin'

# --- enumerate -------------------------------------------------------------
$homeExists = Test-Path -LiteralPath $yb5Home

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($null -eq $userPath) { $userPath = '' }
$pathEntries = @($userPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$pathHasBin  = ($pathEntries -contains $yb5Bin)

Write-Host ''
Write-Host 'yangble5 uninstaller' -ForegroundColor Red
Write-Host ''

if ((-not $homeExists) -and (-not $pathHasBin)) {
    Write-Host "  Nothing to remove - $yb5Home does not exist and your PATH has no"
    Write-Host '  entry pointing into it.'
    Write-Host ''
    exit 0
}

Write-Host '  It will delete EXACTLY these things and nothing else:'
Write-Host ''

if ($homeExists) {
    $bytes = 0
    try {
        $measured = Get-ChildItem -LiteralPath $yb5Home -Recurse -Force -File -ErrorAction SilentlyContinue |
                    Measure-Object -Property Length -Sum
        if ($null -ne $measured -and $null -ne $measured.Sum) { $bytes = [int64]$measured.Sum }
    } catch {
        $bytes = 0
    }
    Write-Host "    $yb5Home   (entire directory, $bytes bytes)"
    foreach ($n in @('credentials', 'machine-id', 'INSTALL_INFO', 'uninstall.ps1')) {
        $p = Join-Path $yb5Home $n
        if (Test-Path -LiteralPath $p) { Write-Host "      - $p" }
    }
    foreach ($d in @('bin', 'claude', 'codex')) {
        $p = Join-Path $yb5Home $d
        if (Test-Path -LiteralPath $p) { Write-Host "      - $p\" }
    }
}

if ($pathHasBin) {
    Write-Host ''
    Write-Host "    the per-user PATH entry '$yb5Bin'"
    Write-Host '      (only this exact entry; every other PATH entry is left alone,'
    Write-Host '       and the machine-wide PATH is never read or written)'
}

Write-Host @'

  It will NOT touch:
    %USERPROFILE%\.claude       your real Claude Code login
    %USERPROFILE%\.codex        your real Codex config
    your PowerShell profile, the machine-wide PATH, or any other registry value

  Note: this removes your key from THIS MACHINE. The server keeps its hash and
  the key keeps working for anyone else holding a copy. If it may have leaked,
  ask the operator to revoke it server-side too.

'@

if ($DryRun) {
    Write-Host '  dry run - nothing was deleted.' -ForegroundColor Yellow
    Write-Host ''
    exit 0
}

if (-not $Yes) {
    $interactive = $true
    try {
        if ([Environment]::UserInteractive -eq $false) { $interactive = $false }
    } catch {
        $interactive = $true
    }
    if (-not $interactive) {
        Write-Host '  Refusing to delete without confirmation.' -ForegroundColor Red
        Write-Host '  Re-run with -Yes if this is really what you want.'
        Write-Host ''
        exit 1
    }
    $answer = Read-Host '  Type YES to confirm'
    if ($answer -cne 'YES') {
        Write-Host '  aborted; nothing was deleted.'
        Write-Host ''
        exit 1
    }
}

# --- delete ----------------------------------------------------------------
if ($pathHasBin) {
    $kept = @($pathEntries | Where-Object { $_ -ne $yb5Bin })
    [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
    Write-Host "  removed PATH entry $yb5Bin" -ForegroundColor Green
}

if ($homeExists) {
    try {
        Remove-Item -LiteralPath $yb5Home -Recurse -Force
        Write-Host "  removed $yb5Home" -ForegroundColor Green
    } catch {
        Write-Host "  could not remove ${yb5Home}: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host '  Close any running yangble5-claude / yangble5-codex session and retry.'
        Write-Host ''
        exit 1
    }
}

Write-Host ''
Write-Host '  yangble5 is gone. Your normal Claude Code login was never touched -'
Write-Host '  run "claude" and everything is exactly as it was.'
Write-Host ''
exit 0
