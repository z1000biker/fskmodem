@echo off
title FSK Modem Installer
color 0B
echo.
echo  ╔══════════════════════════════════════════╗
echo  ║      FSK AUDIO MODEM  –  Installer       ║
echo  ╚══════════════════════════════════════════╝
echo.
echo  Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found. Download from https://python.org
    pause
    exit /b 1
)
python --version

echo.
echo  Installing required packages...
echo  ─────────────────────────────────────────
pip install sounddevice numpy pillow pyserial
echo  ─────────────────────────────────────────
echo.
echo  [OPTIONAL] VB-Audio Virtual Cable for loopback testing:
echo     https://vb-audio.com/Cable/
echo     (Free – install then reboot – select CABLE in the app)
echo.
echo  Installation complete.
echo.
echo  To run the modem:
echo     python fsk_modem.py
echo.
pause
