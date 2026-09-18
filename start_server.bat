@echo off
REM Ops MCP Server Launcher (auto-restart loop)
REM Uses FastMCP with Streamable HTTP transport (standard MCP)
REM
REM The server has an internal self-watchdog: if the HTTP listener dies
REM while the process stays alive, it exits with code 3 and this loop
REM relaunches it. Ctrl+C will prompt "Terminate batch job (Y/N)?" -
REM answer Y to stop it for good.

cd /d "%~dp0"
echo ================================================================
echo           Ops MCP Server (FastMCP)
echo ================================================================
echo.

REM Check if FastMCP is installed
python -c "from fastmcp import FastMCP" 2>nul
if errorlevel 1 (
    echo Installing FastMCP dependencies...
    pip install fastmcp python-dotenv starlette
)

:loop
echo [%date% %time%] Starting server on port 8001...
python server_new.py --port 8001 --host 0.0.0.0
echo [%date% %time%] Server exited with code %errorlevel% - restarting in 10s >> "%~dp0restart.log"
echo [%date% %time%] Server exited with code %errorlevel% - restarting in 10s (Ctrl+C to cancel)
timeout /t 10 /nobreak >nul
goto loop
