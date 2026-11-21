@echo off
rem Start the local report console (selfheal web UI) on http://127.0.0.1:8100/
rem ASCII only on purpose: Chinese comments in .cmd files get garbled under the OEM codepage.
cd /d "%~dp0"
echo Starting selfheal report console ...
echo Open http://127.0.0.1:8100/ in your browser.
python -m selfheal.server --port 8100
