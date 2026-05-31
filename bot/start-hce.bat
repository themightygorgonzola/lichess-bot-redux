@echo off
setlocal

set BOT_DIR=%~dp0
set ENGINE=%BOT_DIR%engine\redux-hce.exe
set ENV_FILE=%BOT_DIR%.env

echo.
echo  ========================================
echo  LichessBotRedux  [HCE mode]
echo  Dashboard: http://localhost:3000
echo  Press Ctrl+C to stop
echo  ========================================
echo.

if not exist "%ENGINE%" (
    echo  [ERROR] Engine not found at %ENGINE%
    echo          Run: make.ps1 build from the project root
    pause
    exit /b 1
)

if not exist "%ENV_FILE%" (
    echo  [ERROR] .env not found
    echo          Copy .env.example to .env and add your LICHESS_TOKEN
    pause
    exit /b 1
)

set USE_NNUE=false
cd /d "%BOT_DIR%"
node index.js

endlocal