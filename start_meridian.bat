@echo off
:: Meridian + Cloudflare tunnel startup script
:: Tunnel URL is permanent: https://mcp.usemeridian.us/mcp
:: OAuth tokens persist across restarts via data/oauth_tokens.json

echo [Meridian] Starting server and tunnel...

:: Kill stale processes
taskkill /F /IM python.exe 2>nul
taskkill /F /IM cloudflared.exe 2>nul
timeout /t 2 /nobreak > nul

:: Start Meridian server
cd /d %~dp0
start "Meridian Server" /min cmd /c "pixi run start > tmp\server.log 2>&1"

:: Start named tunnel (permanent URL: https://mcp.usemeridian.us)
start "Cloudflare Tunnel" /min C:\Users\13144\cloudflared.exe tunnel run --token eyJhIjoiZjNiMTcwODFhNmY0ZDZkMGI5ODIzYWQ4N2I5NjM0NWUiLCJ0IjoiNTk5NTJhZDAtNDNjNy00NDFiLWIyMmYtYjc2MTVlYjRiNGZkIiwicyI6ImRjZmFmMGY3ZjYyOTdlZmFiZDU5M2Y5NzI2YmIzNDhhOWRiMWZmNTc1ZTA1MzFhNjdhOWUxMGI3YjQ0NjUwMDQifQ==

echo.
echo [Meridian] Starting up... give it 5 seconds.
echo.
echo MCP connector URL: https://mcp.usemeridian.us/mcp
echo Dashboard:         http://localhost:7878/dashboard
echo.
timeout /t 5 /nobreak > nul
echo [Meridian] Ready.
