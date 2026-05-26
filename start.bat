@echo off

echo ===== MimoRoute 启动 =====

:: 读取端口
for /f "tokens=2 delims=:, " %%a in ('findstr /C:"port" config.json') do set PORT=%%~a
if "%PORT%"=="" set PORT=18888

:: 杀掉占用端口的进程
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo 端口 %PORT% 被占用，终止进程 PID: %%a
    taskkill /F /PID %%a >nul 2>&1
)

echo 启动 MimoRoute (端口: %PORT%)...
python server.py
pause
