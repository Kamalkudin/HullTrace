@echo off
setlocal
title HullTrace Web
cd /d "%~dp0"
python -B -m Maritim.server --port 8767
if errorlevel 1 pause
