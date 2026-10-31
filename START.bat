@echo off
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
 echo Python-omgeving niet gevonden. Controleer de VoiceStudio-map.
 pause
 exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0studio.py"
exit /b
