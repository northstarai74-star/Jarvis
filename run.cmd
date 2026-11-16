@echo off
setlocal
set "JARVIS_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%JARVIS_PYTHON%" set "JARVIS_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%JARVIS_PYTHON%" (
  echo Python 3.12 is required. No runtime was downloaded.
  exit /b 1
)
"%JARVIS_PYTHON%" -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))"
if errorlevel 1 exit /b 1
pushd "%~dp0"
"%JARVIS_PYTHON%" -m jarvis %*
set "JARVIS_RESULT=%ERRORLEVEL%"
popd
exit /b %JARVIS_RESULT%
