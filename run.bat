@echo off
chcp 65001 > nul
cd /d "%~dp0"

if not exist ".venv" (
    echo [1/3] 가상환경 생성 중...
    python -m venv .venv
)

echo [2/3] 의존성 설치 중...
call .venv\Scripts\python.exe -m pip install -q --upgrade pip
call .venv\Scripts\python.exe -m pip install -q -r requirements.txt

echo [3/3] 서버 시작 - http://127.0.0.1:8000
call .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
