# headersHelper for the desktop-mcp entry in .mcp.json.
# Claude Code runs this on every connect and uses the JSON it prints as HTTP headers.
# Reads DESKTOP_MCP_API_KEY from the user registry environment (HKCU\Environment),
# so it works even when Claude was started from a process that predates the variable
# (the "${DESKTOP_MCP_API_KEY}" header expanded to an empty string -> HTTP 401).
$key = [Environment]::GetEnvironmentVariable("DESKTOP_MCP_API_KEY", "User")
if (-not $key) { $key = $env:DESKTOP_MCP_API_KEY }
if (-not $key) {
    [Console]::Error.WriteLine("DESKTOP_MCP_API_KEY is not set (run install_service.ps1 -Desktop)")
    exit 1
}
[Console]::Out.Write((@{ Authorization = "Bearer $key" } | ConvertTo-Json -Compress))
