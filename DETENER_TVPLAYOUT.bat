@echo off
setlocal
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8088" ^| findstr "LISTENING"') do (
  echo Deteniendo PID %%P...
  taskkill /PID %%P /T /F
)
echo Listo.
pause
