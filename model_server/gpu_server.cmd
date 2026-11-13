@echo off
rem Double-click or run:  gpu_server.cmd start | stop | status
rem Same as gpu_server.ps1, without needing to change PowerShell's script policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gpu_server.ps1" %*
