@echo off
setlocal
if "%CODEX_HOME%"=="" set "CODEX_HOME=%USERPROFILE%\.codex"
py -3 --version >nul 2>&1
if not errorlevel 1 (
  py -3 "%CODEX_HOME%\bin\codex-auto.py" %*
) else (
  python "%CODEX_HOME%\bin\codex-auto.py" %*
)
exit /b %ERRORLEVEL%
