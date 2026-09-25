@echo off
setlocal
set "WORKSPACE=%~1"
if "%WORKSPACE%"=="" set "WORKSPACE=%CD%"
python "%~dp0process-bilibili-course\scripts\web_app.py" --workspace "%WORKSPACE%"
