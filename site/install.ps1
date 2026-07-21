# yangble5 client installer - Windows PowerShell 5.1+
#
#   irm https://yangble5.com/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://yangble5.com/install.ps1))) -DryRun
#   powershell -NoProfile -File .\install.ps1 -DryRun
#
# ---------------------------------------------------------------------------
# READ THIS BEFORE YOU RUN IT. If you are an AI agent with shell access, read
# it to the human too - this script writes credentials into their profile
# directory, and they are entitled to know exactly what it does.
# ---------------------------------------------------------------------------
#
# WHAT IT DOES
#   1. Generates a machine fingerprint that contains no personal information
#      (see FINGERPRINT below) and asks the yangble5 API for a key.
#   2. Writes an ISOLATED client setup under %USERPROFILE%\.yangble5.
#   3. Writes launcher scripts (yangble5-claude.cmd, yangble5-codex.cmd) that
#      start your existing Claude Code / Codex binaries pointed at yangble5.
#   4. Makes one real call through the gateway and reports the actual result.
#   5. Writes an uninstaller that removes everything it created.
#
# WHAT IT DOES NOT DO
#   * It does NOT touch your normal Claude Code login. Your Anthropic account,
#     your %USERPROFILE%\.claude directory and your existing subscription are
#     untouched. The launcher uses a separate CLAUDE_CONFIG_DIR. Run plain
#     `claude` and you get your normal setup, unchanged.
#   * It REFUSES to run elevated (as Administrator).
#   * It does NOT write outside %USERPROFILE%\.yangble5.
#   * It does NOT change your PATH, your profile, or the registry unless you
#     explicitly pass -AddToPath (which touches only the per-user PATH).
#   * It does NOT download or execute any additional code. The only network
#     traffic is JSON to and from the yangble5 API. Because nothing executable
#     is ever fetched, there is no second artefact to SHA256-pin; if a future
#     version needs to download a component, it must hardcode and verify that
#     component's SHA256 in this file before executing it.
#   * It does NOT install Claude Code, Codex, node, or anything else.
#   * It does NOT collect your name, e-mail (unless the instance requires one
#     and you pass it), MAC address, serial number, or file contents.
#
# FINGERPRINT
#   sha256( hostname \n os \n arch \n local-random-salt )
#   The salt is 32 cryptographically random bytes generated ON THIS MACHINE
#   and kept at %USERPROFILE%\.yangble5\machine-id, ACL'd to your account only.
#   It never leaves the machine. Because a 256-bit local secret dominates the
#   input, the fingerprint is not reversible and not linkable to any other
#   machine - it is effectively a random per-install id. hostname/os/arch are
#   folded in only so that a profile copied to another machine or VM image
#   still produces a different id. It contains NO MAC address, NO serial
#   number, NO username, NO PII.
#
# WHAT YANGBLE5 IS (no marketing)
#   A proxy stack built on CLIProxyAPI - a third-party open-source Go project
#   that we did not write (https://github.com/router-for-me/CLIProxyAPI) -
#   fronting Gemini/Grok/GPT upstreams behind one endpoint, plus our own
#   measurement tooling and a compatibility shim. yangble5 is NOT a model. It
#   is not a Taiwanese-trained LLM. There is no yangble5 LLM.
#
# HONEST LIMITS (repeated at the end of the run, on purpose)
#   * No live web search through the proxy. Asked for the current year on
#     2026-07-21, the Gemini upstream answered "2024" and Grok "2025".
#   * The 99.53% prompt-cache hit rate we publish is WARM-ROUND ONLY. The
#     first request of every session is a cold 0% cache write.
#   * Those numbers are one machine, one run, 2026-07-21.
#   * Capacity of any shared pool is small and funded by the operator
#     personally. Nothing here is unlimited, and it may say no.
#
# SECURITY MODEL
#   * $ErrorActionPreference = 'Stop'; TLS 1.2 forced (5.1 still defaults low).
#   * Refuses to run as Administrator.
#   * No Invoke-Expression, and nothing the server sends is ever executed. The
#     API key is validated against \Ayb5_[0-9a-f]{16}_[A-Za-z0-9_-]{16,}\z
#     before it is written anywhere; anything else aborts the install.
#   * Redirects are not followed (-MaximumRedirection 0), so a redirect cannot
#     hand the key to another host.
#   * The key is never passed on a command line; the HTTP call is in-process,
#     so it never appears in the process table.
#   * Files holding secrets get inheritance broken and an ACL granting only
#     the current user SID - the Windows equivalent of chmod 0600.
#   * Any file that would be overwritten is first copied to
#     <file>.bak-<timestamp>.
#   * Re-running is safe and does not mint a second key.
#
# EXIT CODES (identical to install.sh)
#   0  success
#   1  bad arguments
#   2  refused: running elevated
#   3  missing prerequisite
#   4  unsupported platform
#   5  the API could not be reached at all
#   6  installed in BYOK mode with no key yet - NOT usable until you supply one
#   7  could not write configuration
#   8  installed, but the live verification call failed (details printed)
#
# PowerShell 5.1 compatibility is deliberate: no '??', no '?:', no '&&'/'||'
# chains, no PS6+ cmdlets, no three-argument Join-Path.
#
# SPDX-License-Identifier: MIT

[CmdletBinding()]
param(
    [switch] $DryRun,
    [string] $Api,
    [string] $Model,
    [string] $Email,
    [string] $Invite,
    [switch] $NoLiveTest,
    [switch] $ForceRegister,
    [switch] $Reinstall,
    [switch] $NoPrintKey,
    [switch] $AddToPath,
    [switch] $Help
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$Yb5InstallerVersion = '1.0.0'

# --- exit codes ------------------------------------------------------------
$EX_OK       = 0
$EX_USAGE    = 1
$EX_ROOT     = 2
$EX_PREREQ   = 3
$EX_PLATFORM = 4
$EX_NETWORK  = 5
$EX_REGISTER = 6
$EX_CONFIG   = 7
$EX_VERIFY   = 8

# --- output helpers --------------------------------------------------------
function Write-Ok   { param([string]$m) Write-Host '  ok   ' -ForegroundColor Green -NoNewline; Write-Host $m }
function Write-Info { param([string]$m) Write-Host "       $m" }
function Write-Warn { param([string]$m) Write-Host '  warn ' -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Write-Step {
    param([string]$m)
    Write-Host ''
    Write-Host "-- $m" -ForegroundColor Cyan
}
function Stop-Install {
    param([string]$Message, [int]$Code)
    Write-Host ''
    Write-Host 'FAILED: ' -ForegroundColor Red -NoNewline
    Write-Host $Message
    Write-Host "        exit code $Code"
    Write-Host ''
    exit $Code
}

if ($Help) {
    Write-Host @'
usage: install.ps1 [options]

  -DryRun              print every action, write nothing, call nothing
  -Api URL             yangble5 endpoint (default $env:YANGBLE5_API or
                       https://yangble5.com)
  -Model NAME          model alias to configure (default yangble5)
  -Email ADDR          e-mail, if the instance requires one to register
  -Invite CODE         invite code, if the instance is invite-only
  -NoLiveTest          skip the paid verification call (still checks /health)
  -ForceRegister       request a NEW key even if one is already stored
  -Reinstall           delete ~\.yangble5 first, then install fresh
  -NoPrintKey          never print the key to the terminal
  -AddToPath           add ~\.yangble5\bin to your per-user PATH (opt-in)
  -Help                this text

environment: YANGBLE5_API, YANGBLE5_API_KEY (bring your own key),
             YANGBLE5_EMAIL, YANGBLE5_INVITE, YANGBLE5_MODEL
'@
    exit $EX_OK
}

# --- resolve settings ------------------------------------------------------
function Get-EnvOrDefault {
    param([string]$Name, [string]$Default)
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
    return $v
}

if ([string]::IsNullOrWhiteSpace($Api))   { $Api   = Get-EnvOrDefault 'YANGBLE5_API'   'https://yangble5.com' }
if ([string]::IsNullOrWhiteSpace($Model)) { $Model = Get-EnvOrDefault 'YANGBLE5_MODEL' 'yangble5' }
if ([string]::IsNullOrWhiteSpace($Email)) { $Email = Get-EnvOrDefault 'YANGBLE5_EMAIL' '' }
if ([string]::IsNullOrWhiteSpace($Invite)){ $Invite= Get-EnvOrDefault 'YANGBLE5_INVITE' '' }

$ByokKey       = Get-EnvOrDefault 'YANGBLE5_API_KEY' ''
$MaxOutput     = Get-EnvOrDefault 'YANGBLE5_MAX_OUTPUT_TOKENS'  '65536'
$MaxContext    = Get-EnvOrDefault 'YANGBLE5_MAX_CONTEXT_TOKENS' '1000000'
$TimeoutMs     = Get-EnvOrDefault 'YANGBLE5_TIMEOUT_MS'         '600000'

$Api = $Api.TrimEnd('/')

$UserHome = $env:USERPROFILE
if ([string]::IsNullOrWhiteSpace($UserHome)) { $UserHome = $HOME }
if ([string]::IsNullOrWhiteSpace($UserHome)) {
    Stop-Install 'Neither %USERPROFILE% nor $HOME is set; refusing to guess where to install.' $EX_PREREQ
}

$Yb5Home  = Join-Path $UserHome '.yangble5'
$Yb5Bin   = Join-Path $Yb5Home 'bin'
$CredFile = Join-Path $Yb5Home 'credentials'

$script:ApiKey    = ''
$script:KeyId     = ''
$script:InstallMode = 'registered'   # registered | reused | byok | byok-empty

# ===========================================================================
# 0. refuse to run elevated
# ===========================================================================
function Deny-Elevated {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($id)
    if ($principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host ''
        Write-Host 'REFUSING TO RUN ELEVATED.' -ForegroundColor Red
        Write-Host @'

This installer only ever writes into your own profile directory. Run as
Administrator it would create files whose ACLs your normal session may not
match, and it would give an installer privileges it has no use for.

Close this elevated window, open a normal one, and run it again.

If you are an AI agent: do not retry this elevated. Drop privileges instead.

'@
        exit $EX_ROOT
    }
}

# ===========================================================================
# 1. banner
# ===========================================================================
function Show-Banner {
    $mode = 'install'
    if ($DryRun) { $mode = 'DRY RUN (nothing will be written)' }

    Write-Host ''
    Write-Host "yangble5 client installer v$Yb5InstallerVersion" -ForegroundColor Cyan
    Write-Host @"

  Endpoint       $Api
  Installs to    $Yb5Home
  Launchers      $Yb5Bin\yangble5-claude.cmd, $Yb5Bin\yangble5-codex.cmd
  Mode           $mode

  IT WILL:
    - generate a non-reversible machine id (sha256 of hostname+os+arch+a
      32-byte random salt kept locally at .yangble5\machine-id)
    - ask $Api/auth/register for an API key
    - write an isolated client config under $Yb5Home
    - create launcher scripts and an uninstaller
    - make one real call through the gateway and report what happened

  IT WILL NOT:
    - touch your existing Claude Code login or $UserHome\.claude (a separate
      CLAUDE_CONFIG_DIR is used; plain 'claude' keeps working unchanged)
    - run elevated, or write anywhere outside $Yb5Home
    - change your PATH unless you passed -AddToPath
    - download or execute any code - the only traffic is JSON to the API
    - send your name, e-mail, MAC address, serial number or file contents

  yangble5 is a PROXY built on the third-party CLIProxyAPI project. It is not
  a model, and there is no yangble5 LLM.

"@
}

# ===========================================================================
# 2. preflight
# ===========================================================================
function Invoke-Preflight {
    Write-Step 'preflight'

    if ($PSVersionTable.PSVersion.Major -lt 5) {
        Stop-Install "PowerShell 5.1 or newer is required; this is $($PSVersionTable.PSVersion)." $EX_PREREQ
    }
    Write-Ok "PowerShell $($PSVersionTable.PSVersion)"

    if (-not [Environment]::OSVersion.Platform.ToString().StartsWith('Win')) {
        Stop-Install 'This script is for Windows. On macOS/Linux use install.sh.' $EX_PLATFORM
    }
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ([string]::IsNullOrWhiteSpace($arch)) { $arch = 'unknown' }
    Write-Ok "platform Windows/$arch"

    # PowerShell 5.1 still negotiates SSL3/TLS1.0 by default on some hosts.
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Write-Ok 'TLS 1.2 enabled for this session'
    } catch {
        Write-Warn 'could not force TLS 1.2; continuing, but the connection may fail'
    }

    if ($Api -notmatch '^https://') {
        if ($Api -match '^http://(127\.0\.0\.1|localhost)(:|/|$)') {
            Write-Warn "using plaintext HTTP to a local endpoint ($Api) - fine for testing"
        } elseif ($Api -match '^http://') {
            Stop-Install "refusing to send an API key over plaintext HTTP to a remote host: $Api" $EX_USAGE
        } else {
            Stop-Install "-Api must be a http(s) URL, got: $Api" $EX_USAGE
        }
    }

    if ($null -eq (Get-Command claude -ErrorAction SilentlyContinue)) {
        Write-Warn 'claude is not in PATH - the launcher is written anyway, but you'
        Write-Info 'need Claude Code installed for it to do anything:'
        Write-Info 'https://claude.com/product/claude-code'
    }
    if ($null -eq (Get-Command codex -ErrorAction SilentlyContinue)) {
        Write-Info 'note: codex is not in PATH; the Codex launcher is written anyway'
    }
}

# ===========================================================================
# 3. filesystem helpers (dry-run aware, backup-on-overwrite, ACL-tightened)
# ===========================================================================
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Get-CurrentUserSid {
    return ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
}

function Protect-Path {
    # Windows equivalent of chmod 0600 / 0700: break inheritance, grant only
    # the current user. Failure is a warning, not a fatal error - the file is
    # still inside the user's own profile.
    param([string]$Path, [switch]$Directory)
    if ($DryRun) { return }
    try {
        $sid = Get-CurrentUserSid
        $grant = "*${sid}:(F)"
        if ($Directory) { $grant = "*${sid}:(OI)(CI)(F)" }
        $null = & icacls.exe $Path /inheritance:r /grant:r $grant
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "icacls returned $LASTEXITCODE for $Path; check its permissions yourself"
        }
    } catch {
        Write-Warn "could not tighten permissions on ${Path}: $($_.Exception.Message)"
    }
}

function New-Yb5Directory {
    param([string]$Path, [switch]$Secure)
    if ($DryRun) {
        if (-not (Test-Path -LiteralPath $Path)) { Write-Info "would create directory $Path" }
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        try {
            $null = New-Item -ItemType Directory -Path $Path -Force
        } catch {
            Stop-Install "could not create ${Path}: $($_.Exception.Message)" $EX_CONFIG
        }
        if ($Secure) { Protect-Path -Path $Path -Directory }
    }
}

function Write-Yb5File {
    # -NoBackup is for files this installer owns outright (INSTALL_INFO), whose
    # content changes every run by design. Backing those up would leave a trail
    # of .bak files behind for no benefit. Everything a user might have edited
    # is always backed up.
    param([string]$Path, [string]$Content, [switch]$Secure, [switch]$NoBackup)

    if ($DryRun) {
        $bytes = $script:Utf8NoBom.GetByteCount($Content)
        if (Test-Path -LiteralPath $Path) {
            Write-Info "would back up and overwrite $Path ($bytes bytes)"
        } else {
            Write-Info "would write $Path ($bytes bytes)"
        }
        return
    }

    if (Test-Path -LiteralPath $Path) {
        $existing = ''
        try { $existing = [System.IO.File]::ReadAllText($Path) } catch { $existing = '' }
        if ($existing -ceq $Content) {
            Write-Info "unchanged $Path"
            return
        }
        if (-not $NoBackup) {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $bak = "$Path.bak-$stamp"
            try {
                Copy-Item -LiteralPath $Path -Destination $bak -Force
                Write-Warn "backed up existing $Path -> $bak"
            } catch {
                Stop-Install "could not back up ${Path}: $($_.Exception.Message)" $EX_CONFIG
            }
        }
    }

    $parent = Split-Path -Parent $Path
    New-Yb5Directory -Path $parent
    try {
        # WriteAllText with an explicit UTF8Encoding($false): Out-File in 5.1
        # emits a BOM, and a BOM breaks .cmd files and TOML parsers.
        [System.IO.File]::WriteAllText($Path, $Content, $script:Utf8NoBom)
    } catch {
        Stop-Install "could not write ${Path}: $($_.Exception.Message)" $EX_CONFIG
    }
    if ($Secure) { Protect-Path -Path $Path }
    Write-Ok "wrote $Path"
}

# ===========================================================================
# 4. machine fingerprint  (see FINGERPRINT in the header)
# ===========================================================================
function Get-MachineFingerprint {
    $saltFile = Join-Path $Yb5Home 'machine-id'
    $salt = ''

    if (Test-Path -LiteralPath $saltFile) {
        $salt = ([System.IO.File]::ReadAllText($saltFile)).Trim()
    }
    if ([string]::IsNullOrWhiteSpace($salt)) {
        $bytes = New-Object byte[] 32
        $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $salt = -join ($bytes | ForEach-Object { $_.ToString('x2') })

        if ($DryRun) {
            Write-Info "would create $saltFile (32-byte local random salt, user-only ACL)"
        } else {
            New-Yb5Directory -Path $Yb5Home -Secure
            [System.IO.File]::WriteAllText($saltFile, "$salt`n", $script:Utf8NoBom)
            Protect-Path -Path $saltFile
        }
    }

    $hostName = $env:COMPUTERNAME
    if ([string]::IsNullOrWhiteSpace($hostName)) { $hostName = 'unknown' }
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ([string]::IsNullOrWhiteSpace($arch)) { $arch = 'unknown' }

    # Same formula and field order as install.sh.
    $material = "$hostName`n" + "Windows`n" + "$arch`n" + "$salt`n"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($material))
    } finally {
        $sha.Dispose()
    }
    return (-join ($digest | ForEach-Object { $_.ToString('x2') }))
}

# ===========================================================================
# 5. HTTP  (in-process: the key never reaches the process table)
# ===========================================================================
function Invoke-Yb5Request {
    param(
        [string] $Method,
        [string] $Path,
        [string] $Body,
        [string] $Key
    )

    $headers = @{
        'accept'            = 'application/json'
        'user-agent'        = "yangble5-installer/$Yb5InstallerVersion"
        'anthropic-version' = '2023-06-01'
    }
    if (-not [string]::IsNullOrWhiteSpace($Key)) {
        $headers['x-api-key'] = $Key
        $headers['authorization'] = "Bearer $Key"
    }

    $result = [pscustomobject]@{
        Transport = $false   # did we get an HTTP response at all
        Status    = 0
        Seconds   = 0.0
        Body      = ''
        Error     = ''
    }

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $params = @{
            Uri                = "$Api$Path"
            Method             = $Method
            Headers            = $headers
            TimeoutSec         = 120
            UseBasicParsing    = $true
            MaximumRedirection = 0   # a followed redirect would leak the key
        }
        if (-not [string]::IsNullOrWhiteSpace($Body)) {
            $params['Body'] = $Body
            $params['ContentType'] = 'application/json'
        }
        $resp = Invoke-WebRequest @params
        $sw.Stop()
        $result.Transport = $true
        $result.Status    = [int]$resp.StatusCode
        $result.Seconds   = [math]::Round($sw.Elapsed.TotalSeconds, 2)
        $result.Body      = [string]$resp.Content
        return $result
    } catch {
        $sw.Stop()
        $result.Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2)
        $result.Error   = $_.Exception.Message

        $resp = $null
        try { $resp = $_.Exception.Response } catch { $resp = $null }
        if ($null -ne $resp) {
            $result.Transport = $true
            try { $result.Status = [int]$resp.StatusCode } catch { $result.Status = 0 }
            try {
                $stream = $resp.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $result.Body = $reader.ReadToEnd()
                $reader.Close()
            } catch {
                $result.Body = ''
            }
        }
        return $result
    }
}

function Get-JsonField {
    # Defensive: ConvertFrom-Json can throw, and the object may not have the
    # property. Never uses Invoke-Expression, never trusts the shape.
    param([string]$Json, [string]$Field)
    if ([string]::IsNullOrWhiteSpace($Json)) { return '' }
    $obj = $null
    try { $obj = $Json | ConvertFrom-Json } catch { return '' }
    if ($null -eq $obj) { return '' }
    if ($obj -isnot [psobject]) { return '' }
    if (($obj.PSObject.Properties.Name -contains $Field)) {
        $v = $obj.$Field
        if ($null -eq $v) { return '' }
        if ($v -is [string]) { return $v }
        return [string]$v
    }
    # one level down, for {"error":{"type":..,"message":..}}
    if ($obj.PSObject.Properties.Name -contains 'error') {
        $e = $obj.error
        if ($null -ne $e -and $e -is [psobject]) {
            if ($e.PSObject.Properties.Name -contains $Field) {
                $v = $e.$Field
                if ($null -eq $v) { return '' }
                return [string]$v
            }
        }
    }
    return ''
}

function Test-Yb5Key {
    param([string]$Key)
    if ([string]::IsNullOrWhiteSpace($Key)) { return $false }
    # yb5_<16 hex>_<url-safe secret> - see gateway/storage.py:parse_key.
    # \A..\z rather than ^..$ so a trailing newline cannot sneak through.
    return [regex]::IsMatch($Key, '\Ayb5_[0-9a-f]{16}_[A-Za-z0-9_-]{16,}\z')
}

# ===========================================================================
# 6. existing install
# ===========================================================================
function Find-ExistingInstall {
    Write-Step 'existing install'

    if ($Reinstall -and (Test-Path -LiteralPath $Yb5Home)) {
        if ($DryRun) {
            Write-Info "would delete $Yb5Home (-Reinstall)"
        } else {
            Write-Warn "-Reinstall: deleting $Yb5Home"
            Remove-Item -LiteralPath $Yb5Home -Recurse -Force
        }
    }

    $infoFile = Join-Path $Yb5Home 'INSTALL_INFO'
    if (Test-Path -LiteralPath $infoFile) {
        $prev = ''
        $when = ''
        foreach ($line in [System.IO.File]::ReadAllLines($infoFile)) {
            if ($line.StartsWith('installer_version=')) { $prev = $line.Substring(18) }
            if ($line.StartsWith('installed_at='))      { $when = $line.Substring(13) }
        }
        Write-Ok "found an existing install (v$prev, $when)"
        Write-Info 'updating it in place; your stored key is kept and re-used'
        Write-Info '(use -ForceRegister for a new key, -Reinstall to start clean)'
    } else {
        Write-Ok 'no previous install found'
    }
}

# ===========================================================================
# 7. obtain a key: stored -> BYOK -> register -> BYOK fallthrough
# ===========================================================================
function Read-StoredKey {
    if (-not (Test-Path -LiteralPath $CredFile)) { return $false }
    $key = ''
    $id  = ''
    foreach ($line in [System.IO.File]::ReadAllLines($CredFile)) {
        if ($line.StartsWith('YANGBLE5_API_KEY=')) { $key = $line.Substring(17).Trim() }
        if ($line.StartsWith('YANGBLE5_KEY_ID='))  { $id  = $line.Substring(16).Trim() }
    }
    if (-not (Test-Yb5Key $key)) { return $false }
    $script:ApiKey = $key
    $script:KeyId  = $id
    return $true
}

function Show-ByokInstructions {
    Write-Host @"

  Bring your own key / your own upstream account

  The shared pool is funded out of the operator's own pocket and is small.
  When it is full it says so instead of quietly degrading. Ways forward:

  1. Someone gives you an invite code for this instance:
         .\install.ps1 -Invite YOUR_CODE

  2. You run the stack yourself against your own upstream account - this is
     the path that always works and costs the operator nothing:
         https://github.com/shark0120/yangble5#quick-start
     Then point this installer at your own gateway:
         .\install.ps1 -Api http://127.0.0.1:8320

  3. You already have a yangble5 key:
         `$env:YANGBLE5_API_KEY = 'yb5_...'; .\install.ps1

"@
}

function Get-ApiKey {
    Write-Step 'credentials'

    if ((-not $ForceRegister) -and (Read-StoredKey)) {
        Write-Ok "re-using the key already stored at $CredFile"
        Write-Info "key_id $($script:KeyId) (re-running is idempotent; no new key minted)"
        $script:InstallMode = 'reused'
        return
    }

    if (-not [string]::IsNullOrWhiteSpace($ByokKey)) {
        if (-not (Test-Yb5Key $ByokKey)) {
            Stop-Install 'YANGBLE5_API_KEY is set but is not a valid yangble5 key. Expected yb5_<16 hex>_<secret>.' $EX_USAGE
        }
        $script:ApiKey = $ByokKey
        $script:KeyId  = ($ByokKey -split '_')[1]
        Write-Ok 'using the key supplied in YANGBLE5_API_KEY (no registration needed)'
        $script:InstallMode = 'byok'
        return
    }

    $fingerprint = Get-MachineFingerprint
    Write-Ok "machine id $fingerprint"
    Write-Info '= sha256(hostname + os + arch + local random salt). Not reversible.'
    Write-Info '  No MAC address, no serial number, no username, no PII.'

    if ($DryRun) {
        Write-Info "would POST $Api/auth/register with label=installer-<machine id>"
        Write-Info "would store the returned key at $CredFile (user-only ACL)"
        $script:ApiKey = 'yb5_0000000000000000_DRYRUNDRYRUNDRYRUNxx'
        $script:KeyId  = '0000000000000000'
        $script:InstallMode = 'registered'
        return
    }

    # Validate anything that goes into the JSON body, so it never needs
    # escaping and can never inject into it.
    if (-not [string]::IsNullOrWhiteSpace($Email)) {
        if (-not [regex]::IsMatch($Email, '\A[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\z')) {
            Stop-Install "-Email does not look like an e-mail address: $Email" $EX_USAGE
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($Invite)) {
        if (-not [regex]::IsMatch($Invite, '\A[A-Za-z0-9_-]{1,200}\z')) {
            Stop-Install '-Invite contains characters an invite code cannot have.' $EX_USAGE
        }
    }

    # The gateway's RegisterRequest accepts email / invite_code / label only
    # (gateway/app.py). The fingerprint therefore travels as `label` - the one
    # field the server actually persists - not as a field it would discard.
    $label = 'installer-' + $fingerprint.Substring(0, 32)
    $payload = @{ label = $label }
    if (-not [string]::IsNullOrWhiteSpace($Email))  { $payload['email'] = $Email }
    if (-not [string]::IsNullOrWhiteSpace($Invite)) { $payload['invite_code'] = $Invite }
    $json = $payload | ConvertTo-Json -Compress

    Write-Info "POST $Api/auth/register"
    $r = Invoke-Yb5Request -Method 'POST' -Path '/auth/register' -Body $json -Key ''

    if (-not $r.Transport) {
        Write-Host ''
        Write-Host "Could not reach $Api at all." -ForegroundColor Red
        Write-Host ''
        Write-Host "  $($r.Error)"
        Write-Host @"

Troubleshooting, in order:
  Invoke-WebRequest -UseBasicParsing -Uri '$Api/health' -TimeoutSec 15
  # DNS?    -> Resolve-DnsName yangble5.com
  # Proxy?  -> `$env:HTTPS_PROXY; netsh winhttp show proxy
  # TLS?    -> [Net.ServicePointManager]::SecurityProtocol
"@
        exit $EX_NETWORK
    }

    if ($r.Status -eq 200 -or $r.Status -eq 201) {
        $key = Get-JsonField -Json $r.Body -Field 'api_key'
        if (-not (Test-Yb5Key $key)) {
            $snippet = $r.Body
            if ($snippet.Length -gt 400) { $snippet = $snippet.Substring(0, 400) }
            Stop-Install "the server replied $($r.Status) but the body did not contain a well-formed yangble5 key. Refusing to write anything.`n        Response (first 400 chars): $snippet" $EX_REGISTER
        }
        $script:ApiKey = $key
        $id = Get-JsonField -Json $r.Body -Field 'key_id'
        if (-not [regex]::IsMatch($id, '\A[0-9a-f]{16}\z')) { $id = ($key -split '_')[1] }
        $script:KeyId = $id
        Write-Ok "registered - key_id $($script:KeyId)"
        $script:InstallMode = 'registered'
        return
    }

    if (@(400, 403, 409, 429, 503) -contains $r.Status) {
        $etype = Get-JsonField -Json $r.Body -Field 'type'
        $emsg  = Get-JsonField -Json $r.Body -Field 'message'
        Write-Warn "the instance declined to issue a key (HTTP $($r.Status) $etype)"
        if (-not [string]::IsNullOrWhiteSpace($emsg)) { Write-Info "server said: $emsg" }
        if ($r.Status -eq 400) {
            Write-Info 'most often this means the instance requires an e-mail address:'
            Write-Info '    .\install.ps1 -Email you@example.com'
        }
        Write-Info 'this is NOT an installer failure - falling through to BYOK mode'
        Show-ByokInstructions
        $script:InstallMode = 'byok-empty'
        $script:ApiKey = ''
        $script:KeyId  = ''
        return
    }

    $snippet = $r.Body
    if ($snippet.Length -gt 400) { $snippet = $snippet.Substring(0, 400) }
    Stop-Install "unexpected reply from $Api/auth/register: HTTP $($r.Status)`n        Body (first 400 chars): $snippet" $EX_REGISTER
}

# ===========================================================================
# 8. write the isolated client configuration
# ===========================================================================
function Write-Yb5Config {
    Write-Step 'writing configuration'

    New-Yb5Directory -Path $Yb5Home -Secure
    New-Yb5Directory -Path $Yb5Bin
    New-Yb5Directory -Path (Join-Path $Yb5Home 'claude')
    New-Yb5Directory -Path (Join-Path $Yb5Home 'codex')

    # -- credentials --------------------------------------------------------
    if ([string]::IsNullOrWhiteSpace($script:ApiKey)) {
        $cred = @"
# yangble5 credentials - BYOK mode, no key yet.
# Put your key on the YANGBLE5_API_KEY line below and everything starts working.
YANGBLE5_API=$Api
YANGBLE5_API_KEY=
YANGBLE5_KEY_ID=
YANGBLE5_MODEL=$Model
"@
    } else {
        $cred = @"
# yangble5 credentials - user-only ACL, never commit this file.
# Delete this file (or run yangble5-uninstall) to revoke it locally.
YANGBLE5_API=$Api
YANGBLE5_API_KEY=$($script:ApiKey)
YANGBLE5_KEY_ID=$($script:KeyId)
YANGBLE5_MODEL=$Model
"@
    }
    Write-Yb5File -Path $CredFile -Content $cred -Secure

    # -- Codex config -------------------------------------------------------
    $toml = @"
# yangble5 - isolated Codex configuration (CODEX_HOME=$Yb5Home\codex).
# Your normal %USERPROFILE%\.codex is untouched.
model = "$Model"
model_provider = "yangble5"
# A larger window does not create context; it only changes where the client
# decides to compact. We verified a 748,918-token prompt end to end. We did
# not verify 1,000,000.
model_context_window = $MaxContext
model_max_output_tokens = $MaxOutput

[model_providers.yangble5]
name = "yangble5"
base_url = "$Api/v1"
env_key = "YANGBLE5_API_KEY"
wire_api = "chat"
"@
    Write-Yb5File -Path (Join-Path (Join-Path $Yb5Home 'codex') 'config.toml') -Content $toml -Secure

    Write-Yb5File -Path (Join-Path (Join-Path $Yb5Home 'claude') 'README.txt') -Content @"
This directory is CLAUDE_CONFIG_DIR for the yangble5-claude launcher only.

Claude Code stores its auth and settings per config directory, so anything in
here is separate from your real %USERPROFILE%\.claude. Deleting this directory
logs out the yangble5 session and nothing else.
"@

    # -- launchers ----------------------------------------------------------
    # Plain .cmd on purpose: no PowerShell execution-policy dependency, one
    # process instead of three, and the key is read from the credentials file
    # at launch rather than baked into the launcher.
    $credRead = @"
@echo off
REM yangble5-launcher
setlocal
set "YB5_HOME=%USERPROFILE%\.yangble5"
if not exist "%YB5_HOME%\credentials" (
  >&2 echo yangble5: %YB5_HOME%\credentials is missing. Re-run install.ps1.
  exit /b 6
)
set "YANGBLE5_API="
set "YANGBLE5_API_KEY="
set "YANGBLE5_MODEL="
for /f "usebackq tokens=1,* delims==" %%A in ("%YB5_HOME%\credentials") do (
  if /i "%%A"=="YANGBLE5_API"     set "YANGBLE5_API=%%B"
  if /i "%%A"=="YANGBLE5_API_KEY" set "YANGBLE5_API_KEY=%%B"
  if /i "%%A"=="YANGBLE5_MODEL"   set "YANGBLE5_MODEL=%%B"
)
if not defined YANGBLE5_API_KEY (
  >&2 echo yangble5: no API key in %YB5_HOME%\credentials
  >&2 echo yangble5: add one, or re-run the installer.
  exit /b 6
)
"@

    $claudeCmd = $credRead + @"

REM CLAUDE_CONFIG_DIR is what keeps your real login untouched: Claude Code
REM keeps auth and settings per config dir, so this session cannot see, use,
REM or damage the credentials in %USERPROFILE%\.claude.
set "CLAUDE_CONFIG_DIR=%YB5_HOME%\claude"
set "ANTHROPIC_BASE_URL=%YANGBLE5_API%"
set "ANTHROPIC_AUTH_TOKEN=%YANGBLE5_API_KEY%"
set "ANTHROPIC_MODEL=%YANGBLE5_MODEL%"
REM Claude Code assumes 200K for model names it does not recognise, and
REM 'yangble5' is by construction a name it has never heard of - so it would
REM auto-compact early, and every compaction is a cache-destroying rewrite.
REM Official env var, Claude Code v2.1.193+. This does NOT create context.
set "CLAUDE_CODE_MAX_CONTEXT_TOKENS=$MaxContext"
set "CLAUDE_CODE_MAX_OUTPUT_TOKENS=$MaxOutput"
set "API_TIMEOUT_MS=$TimeoutMs"
REM ANTHROPIC_API_KEY would take precedence over ANTHROPIC_AUTH_TOKEN and send
REM your real Anthropic key to this proxy. Cleared for this process only.
set "ANTHROPIC_API_KEY="
where claude >nul 2>&1
if errorlevel 1 (
  >&2 echo yangble5: Claude Code ^(claude^) is not in PATH.
  >&2 echo yangble5: install it first: https://claude.com/product/claude-code
  exit /b 3
)
claude %*
exit /b %ERRORLEVEL%
"@
    Write-Yb5File -Path (Join-Path $Yb5Bin 'yangble5-claude.cmd') -Content $claudeCmd

    $codexCmd = $credRead + @"

set "CODEX_HOME=%YB5_HOME%\codex"
where codex >nul 2>&1
if errorlevel 1 (
  >&2 echo yangble5: Codex ^(codex^) is not in PATH.
  exit /b 3
)
codex %*
exit /b %ERRORLEVEL%
"@
    Write-Yb5File -Path (Join-Path $Yb5Bin 'yangble5-codex.cmd') -Content $codexCmd

    $envCmd = $credRead + @"

echo ANTHROPIC_BASE_URL=%YANGBLE5_API%
echo ANTHROPIC_MODEL=%YANGBLE5_MODEL%
echo ANTHROPIC_AUTH_TOKEN=%YANGBLE5_API_KEY:~0,24%...redacted
echo CLAUDE_CONFIG_DIR=%YB5_HOME%\claude
echo CLAUDE_CODE_MAX_CONTEXT_TOKENS=$MaxContext
echo CLAUDE_CODE_MAX_OUTPUT_TOKENS=$MaxOutput
echo API_TIMEOUT_MS=$TimeoutMs
echo CODEX_HOME=%YB5_HOME%\codex
exit /b 0
"@
    Write-Yb5File -Path (Join-Path $Yb5Bin 'yangble5-env.cmd') -Content $envCmd

    Write-Uninstaller

    $infoContent = @"
installer_version=$Yb5InstallerVersion
installed_at=$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
api=$Api
model=$Model
mode=$($script:InstallMode)
platform=Windows/$env:PROCESSOR_ARCHITECTURE
"@
    Write-Yb5File -Path (Join-Path $Yb5Home 'INSTALL_INFO') -Content $infoContent -NoBackup

    Add-Yb5ToPath
}

function Add-Yb5ToPath {
    if (-not $AddToPath) {
        $current = [Environment]::GetEnvironmentVariable('Path', 'User')
        if ($null -eq $current) { $current = '' }
        if ($current -split ';' -contains $Yb5Bin) {
            Write-Ok "$Yb5Bin is already on your per-user PATH"
        } else {
            Write-Warn "$Yb5Bin is NOT on your PATH."
            Write-Info 'This installer does not change your PATH unless you ask it to.'
            Write-Info 'Either re-run with -AddToPath, or call the launcher by full path:'
            Write-Info "    $Yb5Bin\yangble5-claude.cmd"
        }
        return
    }

    if ($DryRun) {
        Write-Info "would append $Yb5Bin to the per-user PATH (HKCU environment)"
        return
    }
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($null -eq $current) { $current = '' }
    if ($current -split ';' -contains $Yb5Bin) {
        Write-Ok "$Yb5Bin already on the per-user PATH"
        return
    }
    $updated = $current
    if (-not [string]::IsNullOrWhiteSpace($updated)) { $updated = $updated.TrimEnd(';') + ';' }
    $updated = $updated + $Yb5Bin
    [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
    Write-Ok "added $Yb5Bin to your per-user PATH (system PATH untouched)"
    Write-Info 'Open a new terminal for it to take effect.'
}

function Write-Uninstaller {
    # Single-quoted here-string: nothing in it is expanded at install time, so
    # the uninstaller resolves its own paths at run time.
    $uninstall = @'
# yangble5 uninstaller (installed copy). See site/uninstall.ps1 in the repo.
[CmdletBinding()]
param([switch] $Yes, [switch] $DryRun)
$ErrorActionPreference = 'Stop'

$userHome = $env:USERPROFILE
if ([string]::IsNullOrWhiteSpace($userHome)) { $userHome = $HOME }
$yb5Home = Join-Path $userHome '.yangble5'

Write-Host ''
Write-Host 'yangble5 uninstaller' -ForegroundColor Red
Write-Host ''
if (-not (Test-Path -LiteralPath $yb5Home)) {
    Write-Host "  Nothing to remove - $yb5Home does not exist."
    Write-Host ''
    exit 0
}
Write-Host '  It will delete EXACTLY this and nothing else:'
Write-Host ''
Write-Host "    $yb5Home   (entire directory, including your API key)"
foreach ($n in @('credentials','machine-id','INSTALL_INFO','uninstall.ps1')) {
    $p = Join-Path $yb5Home $n
    if (Test-Path -LiteralPath $p) { Write-Host "      - $p" }
}
foreach ($d in @('bin','claude','codex')) {
    $p = Join-Path $yb5Home $d
    if (Test-Path -LiteralPath $p) { Write-Host "      - $p\" }
}
Write-Host ''
Write-Host '  It will NOT touch your real ~\.claude or ~\.codex, and it will not'
Write-Host '  remove the per-user PATH entry (delete that yourself if you added it).'
Write-Host ''
Write-Host '  This removes your key from THIS MACHINE only. The server keeps its hash'
Write-Host '  and the key still works for anyone holding a copy. If it may have leaked,'
Write-Host '  ask the operator to revoke it server-side too.'
Write-Host ''

if ($DryRun) {
    Write-Host '  dry run - nothing was deleted.'
    Write-Host ''
    exit 0
}
if (-not $Yes) {
    $answer = Read-Host '  Type YES to confirm'
    if ($answer -cne 'YES') {
        Write-Host '  aborted; nothing was deleted.'
        exit 1
    }
}
Remove-Item -LiteralPath $yb5Home -Recurse -Force
Write-Host "  removed $yb5Home"
Write-Host ''
Write-Host '  yangble5 is gone. Your normal Claude Code login was never touched.'
Write-Host ''
exit 0
'@
    Write-Yb5File -Path (Join-Path $Yb5Home 'uninstall.ps1') -Content $uninstall

    # DELIBERATELY NO yangble5-uninstall.cmd.
    #
    # A .cmd wrapper would have to live under .yangble5\bin - the very tree the
    # uninstaller deletes. cmd.exe reads batch files incrementally rather than
    # loading them up front, so once the uninstaller removes the directory,
    # cmd cannot read the batch file's remaining lines: it prints "The system
    # cannot find the path specified." and returns exit 1 even though the
    # uninstall succeeded. Measured, not theorised. Whether it happens at all
    # depends on cmd's internal buffering, which is undocumented and can differ
    # between Windows builds.
    #
    # An uninstaller that reports failure after succeeding is worse than no
    # wrapper, especially for an AI agent reading exit codes. PowerShell parses
    # a whole .ps1 before executing it, so uninstall.ps1 deletes itself safely
    # and returns an honest exit code. One command, no cleverness.
}

# ===========================================================================
# 9. verify - one real call, honestly reported
# ===========================================================================
function Show-Troubleshooting {
    Write-Host @"

  Troubleshooting, in the order worth trying:

    1. Is the service up at all?
         Invoke-WebRequest -UseBasicParsing -Uri '$Api/health' | Select-Object -Expand Content

    2. Is your key accepted, and what is your quota?
         `$k = (Select-String -Path "$CredFile" -Pattern '^YANGBLE5_API_KEY=(.*)$').Matches[0].Groups[1].Value
         Invoke-WebRequest -UseBasicParsing -Uri '$Api/usage' -Headers @{'x-api-key'=`$k} | Select-Object -Expand Content

    3. The exact call this installer made:
         `$body = '{"model":"$Model","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}'
         Invoke-WebRequest -UseBasicParsing -Method POST -Uri '$Api/v1/messages' ``
             -Headers @{'x-api-key'=`$k; 'anthropic-version'='2023-06-01'} ``
             -ContentType 'application/json' -Body `$body

    4. Common causes:
         401  the key was revoked, or KEY_PEPPER was rotated server-side
         402  the operator's monthly cap is reached - the instance is read-only
         429  your daily allowance is spent (resets 00:00 UTC), or rate limited
         404  the model alias "$Model" is not configured on that instance
         502  the gateway is up but the CLIProxyAPI engine behind it is not

    5. Report it with the status code and time above:
         https://github.com/shark0120/yangble5/issues
"@
}

function Test-Installation {
    Write-Step 'verification'

    if ($DryRun) {
        Write-Info "would GET  $Api/health"
        Write-Info "would GET  $Api/v1/models   (authenticated, costs nothing)"
        Write-Info "would POST $Api/v1/messages (one real 16-token completion)"
        return $true
    }

    # -- 1. health: unauthenticated, free -----------------------------------
    $h = Invoke-Yb5Request -Method 'GET' -Path '/health' -Body '' -Key ''
    if (-not $h.Transport) {
        Write-Warn "GET /health - could not connect: $($h.Error)"
        Show-Troubleshooting
        return $false
    }
    if ($h.Status -ne 200) {
        Write-Warn "GET /health returned HTTP $($h.Status) in $($h.Seconds)s"
        Show-Troubleshooting
        return $false
    }
    $hstatus = Get-JsonField -Json $h.Body -Field 'status'
    Write-Ok "GET /health -> 200 in $($h.Seconds)s (status: $hstatus)"
    $accepting = Get-JsonField -Json $h.Body -Field 'accepting_requests'
    if ($accepting -eq 'False') {
        Write-Warn 'the instance reports it is NOT accepting requests right now'
        Write-Info '(operator budget cap reached - it will recover; this is by design)'
    }

    if ([string]::IsNullOrWhiteSpace($script:ApiKey)) {
        Write-Warn 'no API key yet (BYOK mode) - skipping the authenticated calls'
        return $false
    }

    # -- 2. models: authenticated, non-spending -----------------------------
    $m = Invoke-Yb5Request -Method 'GET' -Path '/v1/models' -Body '' -Key $script:ApiKey
    if (-not $m.Transport) {
        Write-Warn "GET /v1/models - could not connect: $($m.Error)"
        Show-Troubleshooting
        return $false
    }
    if ($m.Status -ne 200) {
        Write-Warn "GET /v1/models -> HTTP $($m.Status) in $($m.Seconds)s"
        $msg = Get-JsonField -Json $m.Body -Field 'message'
        if (-not [string]::IsNullOrWhiteSpace($msg)) { Write-Info "server said: $msg" }
        Show-Troubleshooting
        return $false
    }
    Write-Ok "GET /v1/models -> 200 in $($m.Seconds)s (the key is accepted)"

    if ($NoLiveTest) {
        Write-Warn 'skipping the live completion (-NoLiveTest)'
        Write-Info 'the key works, but nothing has been proven end to end'
        return $true
    }

    # -- 3. one real completion ---------------------------------------------
    $probe = @{
        model      = $Model
        max_tokens = 16
        messages   = @(@{ role = 'user'; content = 'Reply with the single word: pong' })
    } | ConvertTo-Json -Compress -Depth 5

    Write-Info 'POST /v1/messages - one real 16-token completion through the stack'
    $c = Invoke-Yb5Request -Method 'POST' -Path '/v1/messages' -Body $probe -Key $script:ApiKey
    if (-not $c.Transport) {
        Write-Warn "POST /v1/messages - could not connect: $($c.Error)"
        Show-Troubleshooting
        return $false
    }
    if ($c.Status -eq 200) {
        Write-Ok "POST /v1/messages -> 200 in $($c.Seconds)s"
        Write-Info 'this was a COLD request: 0% prompt-cache hit, by definition. The'
        Write-Info '99.53% figure applies to warm rounds inside one session only.'
        return $true
    }

    Write-Warn "POST /v1/messages -> HTTP $($c.Status) in $($c.Seconds)s"
    $msg = Get-JsonField -Json $c.Body -Field 'message'
    if (-not [string]::IsNullOrWhiteSpace($msg)) { Write-Info "server said: $msg" }
    Write-Info 'the config was written, but the stack did NOT answer. Not calling this a success.'
    Show-Troubleshooting
    return $false
}

# ===========================================================================
# 10. next steps
# ===========================================================================
function Show-KeyOnce {
    if ([string]::IsNullOrWhiteSpace($script:ApiKey)) { return }
    if ($script:InstallMode -ne 'registered') { return }
    if ($DryRun) { return }
    if ($NoPrintKey) {
        Write-Info "key not printed (-NoPrintKey); it is in $CredFile"
        return
    }
    Write-Host ''
    Write-Host '  Your yangble5 API key - shown once, and only once' -ForegroundColor Cyan
    Write-Host ''
    Write-Host "      $($script:ApiKey)"
    Write-Host ''
    Write-Host "  It is stored at $CredFile with an ACL granting only your account."
    Write-Host '  The server keeps only a scrypt hash of it, so nobody - including the'
    Write-Host '  operator - can show it to you again. If you lose it, register a new one.'
    Write-Host ''
    Write-Host '  If an AI agent ran this installer for you, that key is now in its' -ForegroundColor Yellow
    Write-Host '  transcript. Re-run with -NoPrintKey next time if that matters.' -ForegroundColor Yellow
    Write-Host ''
}

function Show-NextSteps {
    Write-Step 'done'
    Show-KeyOnce
    Write-Host @"
  Launch
      $Yb5Bin\yangble5-claude.cmd      Claude Code, through yangble5
      $Yb5Bin\yangble5-codex.cmd       Codex, through yangble5
      $Yb5Bin\yangble5-env.cmd         show the env being set (key redacted)

      Your normal 'claude' and 'codex' commands are unchanged. This install
      cannot see or damage your existing Claude Code login - it lives in a
      separate CLAUDE_CONFIG_DIR ($Yb5Home\claude).

  Where things live
      $Yb5Home\credentials         your key, user-only ACL
      $Yb5Home\claude\             isolated CLAUDE_CONFIG_DIR
      $Yb5Home\codex\config.toml   isolated CODEX_HOME
      $Yb5Home\bin\                the launchers
      $Yb5Home\machine-id          your local random salt - never uploaded

  Uninstall
      powershell -NoProfile -ExecutionPolicy Bypass -File "$Yb5Home\uninstall.ps1" -Yes

      It prints every path it will delete before deleting anything. Drop -Yes
      to be asked for confirmation. (There is no .cmd wrapper on purpose: a
      batch file inside the directory being deleted cannot return a truthful
      exit code.)

  Re-running
      Safe. It re-uses the stored key, backs up anything it changes, and does
      not mint a second key unless you pass -ForceRegister.

  What you should not expect
      - No live web search. Nothing routed through this proxy searches the web.
        Measured 2026-07-21: asked the current year, Gemini said "2024" and Grok
        said "2025". Treat every answer as recall from training, not fact.
      - The 99.53% prompt-cache hit rate is WARM ROUNDS ONLY. Your first request
        in every session is a cold 0% write. One machine, one run, 2026-07-21.
      - CLAUDE_CODE_MAX_CONTEXT_TOKENS=$MaxContext moves where your client decides
        to compact. It does not create context. We verified a 748,918-token
        prompt end to end; we did not verify 1,000,000.
      - Shared capacity is small and paid for by the operator personally. It will
        tell you when it is out rather than pretend otherwise.
      - yangble5 is a proxy built on CLIProxyAPI, a third-party open-source Go
        project we did not write: https://github.com/router-for-me/CLIProxyAPI

"@
}

# ===========================================================================
# main
# ===========================================================================
Deny-Elevated
Show-Banner
Invoke-Preflight
Find-ExistingInstall
Get-ApiKey
Write-Yb5Config

$verified = Test-Installation
Show-NextSteps

if ($script:InstallMode -eq 'byok-empty') {
    Write-Host ''
    Write-Host 'Installed in BYOK mode - no key yet, so nothing was verified.' -ForegroundColor Yellow
    Write-Host "Add your key to $CredFile and re-run: .\install.ps1"
    Write-Host "Exit code $EX_REGISTER. The installer did its job; the pool had nothing to give."
    Write-Host ''
    exit $EX_REGISTER
}
if (-not $verified) {
    Write-Host ''
    Write-Host 'Installed, but verification FAILED - see above.' -ForegroundColor Red
    Write-Host 'Nothing was rolled back; the config is in place so you can retry.'
    Write-Host "Exit code $EX_VERIFY."
    Write-Host ''
    exit $EX_VERIFY
}
exit $EX_OK
