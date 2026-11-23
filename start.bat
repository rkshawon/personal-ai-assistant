@echo off
setlocal
rem Amolex-9026 launcher. All installs live in E:\ProgramData\Amolex (override with AMOLEX_HOME).

if "%AMOLEX_HOME%"=="" set "AMOLEX_HOME=E:\ProgramData\Amolex"
set "HF_HOME=%AMOLEX_HOME%\cache\hf"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "OLLAMA_MODELS=%AMOLEX_HOME%\models\ollama"
set "PY=%AMOLEX_HOME%\venv\Scripts\python.exe"

if not exist "%PY%" (
    echo First run: installing into %AMOLEX_HOME% ...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -InstallRoot "%AMOLEX_HOME%"
    if errorlevel 1 (
        echo Setup failed. See the messages above.
        pause
        exit /b 1
    )
)

rem Start the local AI server in the background if it is installed and not running
if exist "%AMOLEX_HOME%\ollama\ollama.exe" (
    tasklist /FI "IMAGENAME eq ollama.exe" | find /I "ollama.exe" >nul || start "" /MIN "%AMOLEX_HOME%\ollama\ollama.exe" serve
)

"%PY%" "%~dp0main.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
exit /b %CODE%
