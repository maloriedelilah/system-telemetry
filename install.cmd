@echo off
rem update.cmd and install.cmd are the same file: each runs the .sh with its own name (update.sh,
rem install.sh) in Git Bash, as Administrator, from cmd, PowerShell or a double-click. Arguments
rem pass through:   update.cmd        update.cmd --rollback v1.8.1        install.cmd --ring canary
rem
rem - Git Bash comes from the folder Git for Windows' installer recorded in HKLM (admin-only), never
rem   from PATH, %ProgramFiles% (a user can override it in HKCU\Environment) or a per-user install,
rem   since this runs elevated.
rem - Git's login shell puts Git's own tools (ssh, git) ahead of Windows', so Windows' OpenSSH
rem   can't stand in for Git's. A login shell also reads your own ~/.bash_profile and ~/.bashrc.
rem - update.sh checks out the new release, which can rewrite this file while it runs, and cmd
rem   reads a batch file as it goes. So everything from bash onwards is on one line.
setlocal EnableExtensions DisableDelayedExpansion
net session >nul 2>&1 || goto elevate
set "GITBASH="
set "GITDIR="
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\GitForWindows" /v InstallPath /reg:64 2^>nul ^| findstr /i /c:"InstallPath"') do set "GITDIR=%%B"
if defined GITDIR if exist "%GITDIR%\bin\bash.exe" set "GITBASH=%GITDIR%\bin\bash.exe"
if not defined GITBASH goto nogit
cd /d "%~dp0"
set "CHERE_INVOKING=1"
"%GITBASH%" -l "./%~n0.sh" %* & exit /b
:elevate
echo Asking Windows for Administrator rights: say Yes to the prompt. %~nx0 continues in a new window.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -Verb RunAs -FilePath $env:ComSpec -ArgumentList ('/k \"\"%~f0\" %*\"')"
if errorlevel 1 echo !!! No Administrator window was started. Open Command Prompt with "Run as administrator" and run %~nx0 there.
exit /b 1
:nogit
echo !!! Git for Windows isn't installed for all users (no HKLM\SOFTWARE\GitForWindows). Install it from https://git-scm.com/download/win as Administrator and run %~nx0 again.
exit /b 1
