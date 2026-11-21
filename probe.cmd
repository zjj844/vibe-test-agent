@echo off
REM ASCII only: cmd.exe reads .cmd with the OEM codepage, Chinese comments get garbled.
REM
REM Why this wrapper exists:
REM   `python -m selfheal.targets` only works when the CURRENT directory is the repo root,
REM   because Python adds the current directory to sys.path.
REM   Run from D:\dsh (the parent) and you get:
REM       ModuleNotFoundError: No module named 'selfheal'
REM   This script always cd's to its own folder first, so it works from anywhere.
cd /d "%~dp0"
python -m selfheal.targets %*
