@echo off
rem Opens the OLM to PST converter window using an installed Python 3.9+.
rem Drag an .olm file onto this file to open it directly.
setlocal
cd /d "%~dp0.."
where py >nul 2>nul && (start "" pyw -3 -m olm2pst --gui %1 & exit /b 0)
where pythonw >nul 2>nul && (start "" pythonw -m olm2pst --gui %1 & exit /b 0)
echo Python 3 was not found.
echo Install it from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH"),
echo or run:  winget install Python.Python.3.12
echo Then double-click this file again.
pause
exit /b 1
