@echo off
rem 地霊の声係（2026-09-08）。
rem クラウドの「いまの一言」を30秒おきに覗き、ずんだもんで声にして置きに行く。
rem VOICEVOX ENGINE（127.0.0.1:50021）が動いていることが前提。
rem このPCが起きているあいだだけ動く。閉じれば止まる。
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
rem ずんだもん（VOICEVOX ENGINE）が起きていなければ先に起こす
netstat -ano | findstr ":50021" | findstr LISTENING >nul
if errorlevel 1 (
    echo VOICEVOX ENGINE を起こします
    start "" /min "vv\windows-cpu\run.exe" --host 127.0.0.1 --port 50021
    timeout /t 20 /nobreak >nul
)
:loop
".venv\Scripts\python.exe" sayd.py
echo 声係が止まりました。10秒後にやり直します。
timeout /t 10 /nobreak >nul
goto loop
