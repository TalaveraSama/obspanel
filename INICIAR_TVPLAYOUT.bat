@echo off
setlocal
cd /d "%~dp0"

title TVPlayout VLC PRO - Playout por VLC

if not exist "requirements.txt" (
  echo ERROR: Falta requirements.txt en:
  echo %CD%
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creando entorno Python...
  py -3 -m venv .venv
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 (
    echo ERROR: No se pudo crear .venv. Verifique Python 3.11-3.13.
    pause
    exit /b 1
  )
) else (
  echo [1/3] Entorno Python existente.
)

echo [2/3] Verificando dependencias...
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,jinja2,multipart,vlc" >nul 2>&1
if errorlevel 1 (
  echo Dependencias faltantes. Instalando...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo ERROR instalando dependencias.
    pause
    exit /b 1
  )
) else (
  echo Dependencias OK.
)

echo [3/3] Iniciando TVPlayout VLC PRO...
echo NOTA: El panel abre VLC como reproductor. Asegurese de tener VLC instalado
echo       (el panel busca libvlc en las carpetas tipicas o en Ajustes VLC).
".venv\Scripts\python.exe" app.py
pause
