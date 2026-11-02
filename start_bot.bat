@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".env" (
  echo Сначала скопируй .env.example в .env и заполни логин, пароль, BARS_TOTP_SECRET и TG_TOKEN.
  pause
  exit /b
)
if not exist "venv\Scripts\python.exe" (
  echo Первый запуск: ставлю зависимости...
  python -m venv venv || (echo Не найден Python. Установи его с python.org и отметь "Add to PATH". & pause & exit /b)
  venv\Scripts\python -m pip install -q -r requirements.txt
)
title БАРС бот
venv\Scripts\python bars_bot.py %*
pause
