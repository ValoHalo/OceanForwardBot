@echo off
setlocal EnableExtensions

rem Always work from this script's directory.
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] Could not enter the script directory: %~dp0
    goto :fail
)

set "PYTHON_VERSION=3.12"
set "VENV_DIR=.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "MAIN_SCRIPT=main.py"
set "BOT_SCRIPT=watchdog.py"
set "REQUIREMENTS_FILE=requirements.txt"
set "CONFIG_FILE=config.json"
set "CONFIG_TEMPLATE=example.config.json"

echo ======================================================
echo Telegram Forward Bot launcher ^(Python %PYTHON_VERSION%^)
echo ======================================================

echo [1/5] Checking configuration...
if not exist "%CONFIG_FILE%" (
    if not exist "%CONFIG_TEMPLATE%" (
        echo [ERROR] Configuration template not found: "%CONFIG_TEMPLATE%"
        goto :fail
    )

    copy /Y "%CONFIG_TEMPLATE%" "%CONFIG_FILE%" >nul
    if errorlevel 1 (
        echo [ERROR] Could not create "%CONFIG_FILE%".
        goto :fail
    )

    echo Created "%CONFIG_FILE%" by copying "%CONFIG_TEMPLATE%".
    echo Edit the Bot token, Owner ID, and destinations, then run start.bat again.
    goto :config_created
)
echo Configuration file found.

echo [2/5] Checking the virtual environment...
if not exist "%VENV_PYTHON%" (
    where py >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Windows Python Launcher ^(py.exe^) was not found.
        echo Install Python %PYTHON_VERSION% with the Python Launcher option enabled.
        goto :fail
    )

    echo Creating "%VENV_DIR%" with Python %PYTHON_VERSION%...
    py -%PYTHON_VERSION% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment. Verify Python %PYTHON_VERSION% is installed.
        goto :fail
    )
)

if not exist "%VENV_PYTHON%" (
    echo [ERROR] The virtual environment is incomplete: "%VENV_PYTHON%" is missing.
    goto :fail
)
echo Virtual environment ready.

echo [3/5] Checking dependencies...
if not exist "%REQUIREMENTS_FILE%" (
    echo [ERROR] Requirements file not found: "%REQUIREMENTS_FILE%"
    goto :fail
)

"%VENV_PYTHON%" -m pip install --disable-pip-version-check -r "%REQUIREMENTS_FILE%"
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Check the network and proxy settings.
    goto :fail
)
echo Dependencies ready.

echo [4/5] Validating configuration...
if not exist "%MAIN_SCRIPT%" (
    echo [ERROR] Main script not found: "%MAIN_SCRIPT%"
    goto :fail
)

"%VENV_PYTHON%" "%MAIN_SCRIPT%" --check-config
if errorlevel 1 (
    echo [ERROR] Configuration validation failed. Fix "%CONFIG_FILE%" and retry.
    goto :fail
)
echo Configuration valid.

echo [5/5] Starting watchdog...
if not exist "%BOT_SCRIPT%" (
    echo [ERROR] Watchdog script not found: "%BOT_SCRIPT%"
    goto :fail
)

"%VENV_PYTHON%" "%BOT_SCRIPT%"
set "BOT_EXIT_CODE=%ERRORLEVEL%"

if "%BOT_EXIT_CODE%"=="130" (
    echo Watchdog stopped by user.
) else if not "%BOT_EXIT_CODE%"=="0" (
    echo [ERROR] Watchdog exited with code %BOT_EXIT_CODE%.
)

endlocal & exit /b %BOT_EXIT_CODE%

:config_created
endlocal & exit /b 0

:fail
endlocal & exit /b 1
