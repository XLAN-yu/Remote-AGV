@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-rover-offline-package.ps1"
if errorlevel 1 (
  echo.
  echo 生成失败，请查看上方错误。
  popd
  pause
  exit /b 1
)
echo.
echo 生成完成，文件位于 dist-packages 目录。
popd
pause
