# PowerShell script to install (or repair) the MCP Server as a Windows Service
# Run as Administrator
#
# Safe to re-run any time, on an already-installed machine: it re-checks and
# repairs each step rather than assuming a fresh install (kills a stuck
# process still holding the port, re-verifies server_new.py isn't corrupted
# before touching the service, re-registers the service/task with current
# settings, and forces Task Scheduler to pick up a changed API key without
# needing a reboot).

param(
    [string]$ServiceName = "OpsMCP",
    [string]$Port = "8001",
    [string]$ApiKey = "",
    [string]$PythonVersion = "3.12.4"
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ServerScript = Join-Path $PSScriptRoot "server_new.py"
$RequirementsFile = Join-Path $PSScriptRoot "requirements.txt"

Write-Host "=== Ops MCP Server Installation / Repair ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Project Root: $ProjectRoot"
Write-Host "Port: $Port"
Write-Host ""

# Must be elevated: needed for the Python install, the firewall rule and the
# service/scheduled task registration below.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: this script must be run as Administrator." -ForegroundColor Red
    exit 1
}

# =============================================================================
# Relocate off network drives
# =============================================================================
# The service (NSSM) or scheduled task registered below runs as SYSTEM, which
# cannot see mapped drives (N:\, W:\, ...) or \\server\share paths - those
# only exist in the interactively logged-in user's own session. If this
# script is running from one, copy the whole folder to a local disk and
# re-launch itself from there automatically, instead of failing silently
# once installed. Re-running this from the network copy also DOUBLES as a
# way to refresh/repair a corrupted local C:\ops-mcp-server\server_new.py -
# Copy-Item -Force overwrites it with the known-good source copy.

function Test-NetworkPath {
    param([string]$Path)
    if ($Path -match '^\\\\') { return $true }
    try {
        $qualifier = (Split-Path -Qualifier $Path).TrimEnd(':')
        $drive = New-Object System.IO.DriveInfo("$qualifier`:\")
        return $drive.DriveType -eq [System.IO.DriveType]::Network
    }
    catch { return $false }
}

if (Test-NetworkPath $PSScriptRoot) {
    $LocalRoot = "C:\ops-mcp-server"
    Write-Host "This folder is on a network location ($PSScriptRoot)." -ForegroundColor Yellow
    Write-Host "SYSTEM (the account the service/task runs as) cannot reach mapped drives or shares." -ForegroundColor Yellow
    Write-Host "Copying to $LocalRoot and continuing from there..." -ForegroundColor Cyan
    Write-Host ""

    New-Item -ItemType Directory -Path $LocalRoot -Force | Out-Null
    Copy-Item -Path (Join-Path $PSScriptRoot '*') -Destination $LocalRoot -Recurse -Force

    $NewScript = Join-Path $LocalRoot (Split-Path -Leaf $PSCommandPath)
    $RelaunchArgs = @("-ExecutionPolicy", "Bypass", "-File", $NewScript)
    foreach ($key in $PSBoundParameters.Keys) {
        $RelaunchArgs += "-$key"
        $RelaunchArgs += [string]$PSBoundParameters[$key]
    }

    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $RelaunchArgs -NoNewWindow -Wait -PassThru
    exit $proc.ExitCode
}

# =============================================================================
# Python detection / silent install
# =============================================================================
# Windows ships a "python.exe" App Execution Alias stub in WindowsApps that
# resolves on PATH even when no real Python is installed - it just opens the
# Microsoft Store when run with arguments. We look for a real interpreter on
# disk directly, ignoring PATH, so that stub can never fool this script.

function Find-RealPython {
    $candidates = @()
    $candidates += Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue
    $candidates += Get-ChildItem "${env:ProgramFiles(x86)}\Python3*\python.exe" -ErrorAction SilentlyContinue
    $candidates += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue
    $candidates = $candidates | Where-Object { $_ -and (Test-Path $_.FullName) }
    if ($candidates.Count -gt 0) {
        return ($candidates | Sort-Object FullName -Descending | Select-Object -First 1).FullName
    }
    return $null
}

Write-Host "Checking for a real Python installation..." -ForegroundColor Cyan
$PythonPath = Find-RealPython

if (-not $PythonPath) {
    Write-Host "Python not found (or only the Microsoft Store stub is present)." -ForegroundColor Yellow
    Write-Host "Downloading Python $PythonVersion..." -ForegroundColor Cyan

    $installerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
    $installerPath = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"

    try {
        Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing -ErrorAction Stop
    }
    catch {
        Write-Host "ERROR: could not download the Python installer ($_)." -ForegroundColor Red
        Write-Host "Check internet access on this machine, or install Python manually from python.org and re-run this script." -ForegroundColor Red
        exit 1
    }

    Write-Host "Installing Python $PythonVersion silently (all users, added to PATH)..." -ForegroundColor Cyan
    $proc = Start-Process -FilePath $installerPath -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait -PassThru
    Remove-Item $installerPath -ErrorAction SilentlyContinue

    if ($proc.ExitCode -ne 0) {
        Write-Host "ERROR: Python installer exited with code $($proc.ExitCode)." -ForegroundColor Red
        exit 1
    }

    $PythonPath = Find-RealPython
    if (-not $PythonPath) {
        Write-Host "ERROR: Python was installed but could not be located afterwards." -ForegroundColor Red
        Write-Host "Open a NEW terminal (as Administrator) and re-run this script." -ForegroundColor Red
        exit 1
    }
    Write-Host "Python installed at $PythonPath" -ForegroundColor Green
}
else {
    Write-Host "Found Python at $PythonPath" -ForegroundColor Green
}
Write-Host ""

# =============================================================================
# Dependencies
# =============================================================================

Write-Host "Installing Python dependencies (fastmcp, python-dotenv)..." -ForegroundColor Cyan
& $PythonPath -m pip install --quiet --upgrade pip
& $PythonPath -m pip install --quiet -r $RequirementsFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed (exit $LASTEXITCODE). Check internet access on this machine." -ForegroundColor Red
    exit 1
}
Write-Host "Dependencies installed." -ForegroundColor Green
Write-Host ""

# =============================================================================
# Sanity-check server_new.py BEFORE touching the service
# =============================================================================
# Catches a corrupted/incomplete file (e.g. an interrupted deploy) with a
# clear error instead of installing a service that hangs or crash-loops.

Write-Host "Checking server_new.py compiles cleanly..." -ForegroundColor Cyan
& $PythonPath -m py_compile $ServerScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: server_new.py failed to compile - it looks corrupted or incomplete." -ForegroundColor Red
    Write-Host "Re-copy this project folder from its known-good source (e.g. the network share) and re-run this installer." -ForegroundColor Red
    exit 1
}
Write-Host "OK: server_new.py compiles cleanly." -ForegroundColor Green
Write-Host ""

# =============================================================================
# Firewall
# =============================================================================
# Idempotent - skips if the rule already exists.

$FirewallRuleName = "$ServiceName-$Port"
$existingRule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
if ($existingRule) {
    Write-Host "Firewall rule '$FirewallRuleName' already exists, skipping." -ForegroundColor Gray
}
else {
    try {
        New-NetFirewallRule -DisplayName $FirewallRuleName -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow -ErrorAction Stop | Out-Null
        Write-Host "Firewall rule '$FirewallRuleName' created for inbound TCP $Port." -ForegroundColor Green
    }
    catch {
        Write-Host "Could not create firewall rule: $_" -ForegroundColor Red
    }
}
Write-Host ""

# =============================================================================
# API key: use -ApiKey if given, otherwise keep the existing one, otherwise
# generate a new one. Either way, print it clearly - re-running this script
# with no -ApiKey is the supported way to find out what key is already set.
# =============================================================================

$ExistingKey = [Environment]::GetEnvironmentVariable("MCP_API_KEY", "Machine")
$KeyChanged = $false

if ($ApiKey) {
    $FinalKey = $ApiKey
    if ($FinalKey -ne $ExistingKey) {
        [Environment]::SetEnvironmentVariable("MCP_API_KEY", $FinalKey, "Machine")
        $KeyChanged = $true
    }
    Write-Host "API key set from -ApiKey parameter." -ForegroundColor Green
}
elseif ($ExistingKey) {
    $FinalKey = $ExistingKey
    Write-Host "API key already configured on this machine (unchanged)." -ForegroundColor Green
}
else {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 24
    $rng.GetBytes($bytes)
    $FinalKey = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    [Environment]::SetEnvironmentVariable("MCP_API_KEY", $FinalKey, "Machine")
    $KeyChanged = $true
    Write-Host "No API key existed on this machine - generated a new one." -ForegroundColor Yellow
}
Write-Host ""

# =============================================================================
# Repair: kill anything already holding the port
# =============================================================================
# Covers the case where a previous instance is stuck (e.g. crashed without
# releasing the socket) and would otherwise make the fresh start below fail
# with "address already in use".

Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "Stopping process $($_.OwningProcess) still holding port $Port..." -ForegroundColor Yellow
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1

# =============================================================================
# Service / Scheduled Task registration
# =============================================================================

$nssm = Get-Command nssm -ErrorAction SilentlyContinue
$usingTask = $false

if (-not $nssm) {
    Write-Host "NSSM not found. You can install it with:" -ForegroundColor Yellow
    Write-Host "  winget install nssm" -ForegroundColor Gray
    Write-Host "  or download from: https://nssm.cc/download" -ForegroundColor Gray
    Write-Host ""
    Write-Host "Using a scheduled task that runs at startup:" -ForegroundColor Yellow
    Write-Host ""

    Stop-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $ServiceName -Confirm:$false -ErrorAction SilentlyContinue

    $Action = New-ScheduledTaskAction -Execute $PythonPath -Argument "`"$ServerScript`" --port $Port --host 0.0.0.0" -WorkingDirectory $ProjectRoot
    $Trigger = New-ScheduledTaskTrigger -AtStartup
    $Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

    try {
        Register-ScheduledTask -TaskName $ServiceName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
        Write-Host "Scheduled task '$ServiceName' registered." -ForegroundColor Green
        $usingTask = $true
    }
    catch {
        Write-Host "ERROR creating scheduled task: $_" -ForegroundColor Red
        exit 1
    }

    if ($KeyChanged) {
        # The Task Scheduler service ("Schedule") caches its own process
        # environment from whenever IT started - a machine-wide env var
        # change is invisible to tasks it launches until that service
        # itself restarts (or the machine reboots). Restarting it is safe
        # and does not affect the user's own session or other programs.
        Write-Host "API key changed - restarting the Task Scheduler service so it picks it up..." -ForegroundColor Cyan
        Restart-Service -Name Schedule -Force
        Start-Sleep -Seconds 3
    }

    Start-ScheduledTask -TaskName $ServiceName
}
else {
    Write-Host "Creating Windows service with NSSM..." -ForegroundColor Cyan

    nssm stop $ServiceName 2>$null
    nssm remove $ServiceName confirm 2>$null

    nssm install $ServiceName $PythonPath "`"$ServerScript`" --port $Port --host 0.0.0.0"
    nssm set $ServiceName AppDirectory $ProjectRoot
    nssm set $ServiceName DisplayName "Ops MCP Server"
    nssm set $ServiceName Description "MCP Server for remote management of your Django application (optional)"
    nssm set $ServiceName Start SERVICE_AUTO_START
    nssm set $ServiceName AppEnvironmentExtra "MCP_API_KEY=$FinalKey"
    nssm start $ServiceName
}

Write-Host ""

# =============================================================================
# Verify it's actually up
# =============================================================================

Write-Host "Waiting for the server to come up on port $Port..." -ForegroundColor Cyan
$healthUrl = "http://127.0.0.1:$Port/health"
$up = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 3
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $up = $true; break }
    }
    catch { }
}

Write-Host ""
if ($up) {
    Write-Host "SUCCESS: server is up and responding at $healthUrl" -ForegroundColor Green
}
else {
    Write-Host "WARNING: could not confirm the server is up at $healthUrl after 60s." -ForegroundColor Yellow
    if ($usingTask) {
        Write-Host "Check: Get-ScheduledTaskInfo -TaskName '$ServiceName'" -ForegroundColor Gray
    }
    else {
        Write-Host "Check: nssm status $ServiceName" -ForegroundColor Gray
    }
    Write-Host "See README.md (Troubleshooting) for common causes." -ForegroundColor Gray
    Write-Host "This script is safe to re-run - it will repair a stuck process, a" -ForegroundColor Gray
    Write-Host "corrupted server_new.py (report only, no auto-fix without a source" -ForegroundColor Gray
    Write-Host "copy to restore from), and a wrong/uncollected API key." -ForegroundColor Gray
}

$ReachableIPs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress -Unique

# Ask the server itself for its tool list (MCP handshake: initialize ->
# notifications/initialized -> tools/list) so this always matches what's
# actually running, instead of a hardcoded list that can drift out of sync.
function Get-McpTools {
    param([string]$Url, [string]$ApiKey)
    $headers = @{ "Content-Type" = "application/json"; "Accept" = "application/json, text/event-stream" }
    if ($ApiKey) { $headers["Authorization"] = "Bearer $ApiKey" }
    try {
        Invoke-WebRequest -Uri $Url -Method Post -Headers $headers -TimeoutSec 5 -UseBasicParsing -Body '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"installer","version":"1.0"}}}' | Out-Null
        Invoke-WebRequest -Uri $Url -Method Post -Headers $headers -TimeoutSec 5 -UseBasicParsing -Body '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' | Out-Null
        $resp = Invoke-WebRequest -Uri $Url -Method Post -Headers $headers -TimeoutSec 5 -UseBasicParsing -Body '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
        $dataLine = ($resp.Content -split "`n") | Where-Object { $_ -like "data:*" } | Select-Object -First 1
        if (-not $dataLine) { return $null }
        return ($dataLine.Substring(5).Trim() | ConvertFrom-Json).result.tools
    }
    catch { return $null }
}

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " API KEY: $FinalKey" -ForegroundColor White
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " Use this in .mcp.json as the Bearer token for this server." -ForegroundColor Gray
Write-Host ""
Write-Host " Reachable at (from another machine on the LAN):" -ForegroundColor Cyan
if ($ReachableIPs) {
    foreach ($ip in $ReachableIPs) {
        Write-Host "   http://$ip`:$Port/mcp" -ForegroundColor White
    }
}
else {
    Write-Host "   (no non-loopback IPv4 address found - check network adapters)" -ForegroundColor Yellow
}
Write-Host ""

if ($up) {
    $Tools = Get-McpTools -Url $healthUrl.Replace("/health", "/mcp") -ApiKey $FinalKey
    if ($Tools) {
        Write-Host "Available tools ($($Tools.Count)):" -ForegroundColor Cyan
        foreach ($t in ($Tools | Sort-Object name)) {
            $desc = [string]$t.description
            if ($desc.Length -gt 78) { $desc = $desc.Substring(0, 75) + "..." }
            Write-Host ("  {0,-20} {1}" -f $t.name, $desc)
        }
    }
    else {
        Write-Host "(could not list tools - the server is up but tools/list didn't respond as expected)" -ForegroundColor Yellow
    }
    Write-Host ""
}

Write-Host "Commands:" -ForegroundColor Cyan
if ($usingTask) {
    Write-Host "  Get-ScheduledTaskInfo -TaskName '$ServiceName'   - Check status"
    Write-Host "  Stop-ScheduledTask -TaskName '$ServiceName'      - Stop"
    Write-Host "  Start-ScheduledTask -TaskName '$ServiceName'     - Start"
    Write-Host "  Unregister-ScheduledTask -TaskName '$ServiceName' -Confirm:`$false  - Remove"
}
else {
    Write-Host "  nssm status $ServiceName    - Check status"
    Write-Host "  nssm restart $ServiceName   - Restart service"
    Write-Host "  nssm stop $ServiceName      - Stop service"
    Write-Host "  nssm remove $ServiceName    - Remove service"
}
Write-Host ""
Write-Host "Re-running this script any time (no arguments needed) re-checks and" -ForegroundColor Gray
Write-Host "repairs the install, and reprints the current API key above." -ForegroundColor Gray
