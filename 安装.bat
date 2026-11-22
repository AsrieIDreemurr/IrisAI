@echo off
chcp 65001 >nul
rem IrisAI 一键安装：建 Python 环境、复制配置、按功能选模型下载、装 llama.cpp / llama-swap / ComfyUI。可以重复运行。
cd /d "%~dp0"
set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3 -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo 没找到 Python 3.11 以上。
  choice /m "用 winget 装 Python 3.12"
  if errorlevel 2 goto :nopy
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo 装好后请关掉这个窗口，重新双击 安装.bat。
  pause
  exit /b 1
)
%PY% scripts\setup.py
echo.
pause
exit /b

:nopy
echo 请到 https://www.python.org 装 Python 3.12（勾选 Add python.exe to PATH），然后重新双击 安装.bat。
pause
exit /b 1
