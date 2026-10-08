<#
.SYNOPSIS
  Install / update / remove the Alejandro Windows Connector.

.DESCRIPTION
  Reuses the desktop-mcp venv (desktop\.venv: Python 3.12 with httpx + websockets) and the
  same start-up mechanism as Desktop-MCP: a scheduled task at logon, in the user's own
  interactive session (desktop tools need it), restarted every minute if it dies.
  It does NOT modify the existing Desktop-MCP task.

.EXAMPLE
  .\install_connector.ps1 -DeviceId pc-casa -GatewayUrl wss://alejandro.example.com/bridge/v1/ws
  .\install_connector.ps1 -SetToken          # paste the token from `admin add` (hidden input)
  .\install_connector.ps1 -Status
  .\install_connector.ps1 -Remove            # remove the task; keeps config, token, audit
  .\install_connector.ps1 -Remove -Purge     # also delete %LOCALAPPDATA%\alejandro-connector
#>
param(
  [string]$DeviceId,
  [string]$GatewayUrl = "",
  [string]$DesktopUrl = "http://127.0.0.1:8011/mcp",
  [switch]$SetToken,
  [switch]$Status,
  [switch]$Remove,
  [switch]$Purge,
  [switch]$NoStart,
  # venv that runs the connector; default: this repo's desktop-mcp venv
  [string]$VenvScripts = ""
)
$ErrorActionPreference = "Stop"
$TaskName = "Alejandro-Connector"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $VenvScripts) { $VenvScripts = Join-Path $RepoRoot "desktop\.venv\Scripts" }
$Python   = Join-Path $VenvScripts "python.exe"
$PythonW  = Join-Path $VenvScripts "pythonw.exe"
$Home_    = Join-Path $env:LOCALAPPDATA "alejandro-connector"

function Invoke-Connector([string[]]$Args_) {
  Push-Location $RepoRoot
  try { & $Python -m remote_bridge.windows_connector @Args_ } finally { Pop-Location }
}

if ($Status) {
  Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Select-Object TaskName, State | Format-Table
  Invoke-Connector @("status")
  return
}

if ($Remove) {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($t) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task $TaskName removed."
  } else { Write-Host "Task $TaskName was not installed." }
  if ($Purge -and (Test-Path $Home_)) {
    Remove-Item -Recurse -Force $Home_
    Write-Host "Deleted $Home_ (token, policy, audit)."
  } else { Write-Host "Kept $Home_ (config, DPAPI token, policy, audit)." }
  return
}

if (-not (Test-Path $Python)) { throw "desktop venv not found at $Python. Run install_service.ps1 -Desktop first." }
& $Python -c "import httpx, websockets" 2>$null
if ($LASTEXITCODE -ne 0) { throw "desktop venv lacks httpx/websockets" }
& $Python -m py_compile (Join-Path $RepoRoot "remote_bridge\windows_connector.py")
if ($LASTEXITCODE -ne 0) { throw "connector does not compile" }

New-Item -ItemType Directory -Force $Home_ | Out-Null

if ($SetToken) {
  $sec = Read-Host "Device token (input hidden)" -AsSecureString
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  Push-Location $RepoRoot
  try { $plain | & $Python -m remote_bridge.windows_connector set-token } finally { Pop-Location; $plain = $null }
  if (-not $DeviceId) { return }
}

$cfgPath = Join-Path $Home_ "config.json"
$cfg = @{}
if (Test-Path $cfgPath) {
  Copy-Item $cfgPath "$cfgPath.bak-$(Get-Date -Format yyyyMMddHHmmss)"
  (Get-Content $cfgPath -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value }
}
if ($DeviceId) { $cfg["device_id"] = $DeviceId }
if (-not $cfg["device_id"]) { throw "pass -DeviceId the first time" }
if ($GatewayUrl) { $cfg["gateway_url"] = $GatewayUrl }
if (-not $cfg["gateway_url"]) { throw "pass -GatewayUrl wss://<dominio>/bridge/v1/ws the first time" }
$cfg["desktop_url"] = $DesktopUrl
[IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
Invoke-Connector @("policy") | Out-Null   # creates policy.json with the safe defaults

if (-not (Test-Path (Join-Path $Home_ "token.bin"))) {
  Write-Warning "No token yet. Run: .\install_connector.ps1 -SetToken"
}

$action  = New-ScheduledTaskAction -Execute $PythonW -Argument "-m remote_bridge.windows_connector run" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Description "Alejandro Windows Connector (outbound WSS to the gateway)" -Force | Out-Null
Write-Host "Task $TaskName registered (at logon, user session, restart every 1 min)."

if (-not $NoStart) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 4
  Invoke-Connector @("status")
}
