@echo off
rem Starts GhostTrace on this computer and opens it in your web browser.
rem Nothing is uploaded: the app answers only on this computer.
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo GhostTrace needs Python 3.10 or newer: https://www.python.org/downloads/
  echo When you install it, tick "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)
%PY% code\main.py serve --open %*
pause
