#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
exec .venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
