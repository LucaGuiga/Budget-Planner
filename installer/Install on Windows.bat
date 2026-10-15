@echo off
rem Double-click to install Budget Planner on this Windows PC.
rem Installs Python first if it isn't here, then runs app\install.py.
setlocal
cd /d "%~dp0"
if not exist "app\install.py" goto notunzipped
set "BUDGET_PLANNER_NO_PAUSE=1"

set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto install
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto install
set "NEWPY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%NEWPY%" goto newpython

echo Budget Planner needs Python, a free program. Installing it now, just for your account.
echo This takes a minute or two.
echo.
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if not exist "%NEWPY%" goto nopython

:newpython
set PY="%NEWPY%"

:install
echo.
%PY% app\install.py
echo.
pause
exit /b

:nopython
echo.
echo Python couldn't be installed automatically.
echo Go to https://www.python.org/downloads/ and click Download Python,
echo open the file and click Install Now. Then double-click this installer again.
echo.
pause
exit /b 1

:notunzipped
echo This installer has to run from the unzipped folder.
echo Close this window, right-click the zip file, choose Extract All,
echo then double-click "Install on Windows" inside the extracted folder.
echo.
pause
exit /b 1
