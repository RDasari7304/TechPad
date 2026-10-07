@echo off
REM One-time: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt && .venv\Scripts\playwright install chromium
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
if "%1"=="" (python -m larpcheck serve) else (python -m larpcheck %*)
