@echo off
setlocal
cd /d "%~dp0"
title TVPlayout - Diagnostico VLC

echo ============================================================
echo   DIAGNOSTICO VLC - TVPlayout
echo   Comprueba que el panel pueda controlar tu VLC instalado
echo ============================================================
echo.

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" tools\vlc_doctor.py --panel %*
if errorlevel 1 (
  echo.
  echo ------------------------------------------------------------
  echo  Hay algo que corregir. Pistas:
  echo   * Si dice "No se encontro vlc.exe": pon la ruta completa en
  echo     AJUSTES VLC ^-^> "Ruta a vlc.exe (app)".
  echo   * Si dice "VLC no responde": cierra todos los VLC abiertos
  echo     y pulsa "INICIAR VLC" en el panel.
  echo   * Si dice "rechaza la password (401)": deja la misma password
  echo     en AJUSTES VLC y reinicia VLC desde el panel.
  echo ------------------------------------------------------------
)

echo.
pause
