@echo off
chcp 65001 >nul
title Dify Local MQTT Controller
echo.
echo ================================================
echo   Dify Local MQTT Controller
echo ================================================
echo.
echo   1. Auto Controller + Chat Terminal
echo   2. Auto Controller only
echo   3. Chat Terminal only
echo   4. GUI Dashboard + Chat
echo.
set /p choice="Select (1/2/3/4): "

if "%choice%"=="1" (
    python main.py
) else if "%choice%"=="2" (
    python main.py auto
) else if "%choice%"=="3" (
    python main.py chat
) else if "%choice%"=="4" (
    python main.py gui
) else (
    echo Invalid choice, running both...
    python main.py
)
pause
