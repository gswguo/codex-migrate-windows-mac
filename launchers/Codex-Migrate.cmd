@echo off
setlocal
set "PROJECT_DIR=%~dp0.."
set "PYTHONPATH=%PROJECT_DIR%\src;%PYTHONPATH%"
if defined CODEX_MIGRATE_PYTHON (
    "%CODEX_MIGRATE_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 goto custom_python
    goto missing_python
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto py_launcher
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto python_launcher
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto python3_launcher
goto missing_python
:custom_python
if "%~1"=="" ("%CODEX_MIGRATE_PYTHON%" -m codex_migrate wizard) else ("%CODEX_MIGRATE_PYTHON%" -m codex_migrate %*)
exit /b %errorlevel%
:py_launcher
if "%~1"=="" (py -3 -m codex_migrate wizard) else (py -3 -m codex_migrate %*)
exit /b %errorlevel%
:python_launcher
if "%~1"=="" (python -m codex_migrate wizard) else (python -m codex_migrate %*)
exit /b %errorlevel%
:python3_launcher
if "%~1"=="" (python3 -m codex_migrate wizard) else (python3 -m codex_migrate %*)
exit /b %errorlevel%
:missing_python
echo Python 3.11 or newer is required. Install Python yourself, or set CODEX_MIGRATE_PYTHON to its executable.
echo Nothing was installed and no Codex data was changed.
if "%~1"=="" pause
exit /b 1
