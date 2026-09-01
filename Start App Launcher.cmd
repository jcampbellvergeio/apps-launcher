@echo off
rem Double-click to start every app with autostart:true, then open the page.
rem Runs in YOUR session, so nothing else can reap the processes it starts.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0devapps.ps1" start
start "" http://127.0.0.1:5058/
