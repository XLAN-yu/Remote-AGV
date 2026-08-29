@echo off
setlocal
pushd "%~dp0"

if not exist "rover-offline\dist\index.html" (
  echo Static site not found. Building local files...
  call npm.cmd run build:rover-offline
  if errorlevel 1 (
    echo Build failed. Please review the messages above.
    popd
    pause
    exit /b 1
  )
)

echo.
echo ROVER ONE local control page is running:
echo http://127.0.0.1:3001/
echo.
echo Keep this window open. Press Ctrl+C to stop the local preview.
echo On the rover: connect to ROVER-ONE Wi-Fi, then open http://10.42.0.1
echo.
python -m http.server 3001 --bind 127.0.0.1 --directory "rover-offline\dist"

popd
