@echo off
rem AI-Qwen-Max launcher: double-click = interactive mode; args pass through (e.g. RUN.bat --serve)
rem NOTE: keep this file ASCII-only; cmd.exe parses .bat in the ANSI codepage.
cd /d "%~dp0"

python -c "import fastapi, uvicorn, httpx, prompt_toolkit" 2>nul
if errorlevel 1 (
    echo [RUN.bat] Python dependencies missing for this interpreter:
    echo     python -m pip install -e .
    echo.
    pause
    exit /b 1
)

python -m ai_qwen_max %*
if errorlevel 1 pause
