@echo off
rem Local self-check: same steps as .github/workflows/ci.yml
rem Run this before pushing. ASCII only (Chinese in .cmd gets garbled under the OEM codepage).
cd /d "%~dp0"

echo [1/3] compileall ...
python -m compileall -q selfheal demo scripts
if errorlevel 1 goto fail

echo [2/3] contract self-check ...
python scripts\check_contracts.py
if errorlevel 1 goto fail

echo [3/3] pytest ...
python -m pytest -q
if errorlevel 1 goto fail

echo.
echo ALL GREEN - safe to push.
exit /b 0

:fail
echo.
echo FAILED - fix the errors above before pushing.
exit /b 1
