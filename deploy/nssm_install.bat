@echo off
REM nssm_install.bat — Instala API y Worker como servicios Windows via NSSM
REM Requiere nssm.exe en el PATH: https://nssm.cc/download
REM Ejecutar como Administrador

SET PYTHON=C:\Python313\python.exe
SET BASE=C:\ruta\a\facebook_auto_poster
SET VENV=%BASE%\..\. venv\Scripts\python.exe

echo Instalando servicio fb-autoposter-api...
nssm install fb-autoposter-api "%PYTHON%" "%BASE%\api_main.py"
nssm set fb-autoposter-api AppDirectory "%BASE%"
nssm set fb-autoposter-api AppEnvironmentExtra "SPLIT_PROCESSES=1"
nssm set fb-autoposter-api DisplayName "FB AutoPoster - API"
nssm set fb-autoposter-api Start SERVICE_AUTO_START

echo Instalando servicio fb-autoposter-worker...
nssm install fb-autoposter-worker "%PYTHON%" "%BASE%\worker_main.py"
nssm set fb-autoposter-worker AppDirectory "%BASE%"
nssm set fb-autoposter-worker AppEnvironmentExtra "SPLIT_PROCESSES=1"
nssm set fb-autoposter-worker DisplayName "FB AutoPoster - Worker"
nssm set fb-autoposter-worker Start SERVICE_AUTO_START
nssm set fb-autoposter-worker DependOnService fb-autoposter-api

echo Iniciando servicios...
nssm start fb-autoposter-api
nssm start fb-autoposter-worker

echo Listo. Verificar con: nssm status fb-autoposter-api
