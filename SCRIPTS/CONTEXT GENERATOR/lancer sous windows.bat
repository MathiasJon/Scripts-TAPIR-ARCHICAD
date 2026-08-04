@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Cadastre Tool

echo ============================================
echo  Cadastre Tool
echo ============================================
echo.

REM — Détecter Python (py launcher prioritaire, puis python) —
set PYTHON=
py --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=py
    goto python_found
)
python --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=python
    goto python_found
)
python3 --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=python3
    goto python_found
)

echo ERREUR : Python introuvable.
echo.
echo Solutions :
echo  1. Telechargez Python sur https://www.python.org
echo  2. Lors de l'installation, cochez "Add Python to PATH"
echo  3. Redemarrez cette fenetre apres l'installation
echo.
pause
exit /b 1

:python_found
for /f "tokens=*" %%v in ('%PYTHON% --version 2^>^&1') do echo Python detecte : %%v

REM — Créer l'environnement virtuel si besoin —
if not exist ".venv\Scripts\pip.exe" (
    echo.
    echo Premiere utilisation - creation de l'environnement virtuel...
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo ERREUR : Impossible de creer l'environnement virtuel.
        pause
        exit /b 1
    )
    echo OK
)

REM — Installer/mettre à jour les dépendances —
echo.
echo Installation des dependances (patientez)...
.venv\Scripts\pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo ERREUR lors de l'installation. Details :
    echo.
    .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo OK

REM — Arrêter un éventuel serveur sur le port 5000 —
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":5057 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

REM — Démarrer Flask (log dans cadastre-tool.log) —
echo.
echo Demarrage du serveur...
set LOG=%~dp0cadastre-tool.log
if exist "%LOG%" del "%LOG%"
start "" /b cmd /c ".venv\Scripts\python server.py > \"%LOG%\" 2>&1"

REM — Attendre que le serveur soit prêt (max 30 s) —
set RETRY=0
:wait
timeout /t 1 /nobreak >nul
set /a RETRY+=1
if %RETRY% GTR 30 (
    echo.
    echo ERREUR : Le serveur n'a pas demarre apres 30 secondes.
    echo.
    for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":5057 " ^| findstr "LISTENING"') do (
        echo Le port 5057 est deja utilise par le PID %%p - c'est probablement la cause.
    )
    echo.
    echo --- Contenu du log ---
    if exist "%LOG%" type "%LOG%"
    echo ----------------------
    echo.
    pause
    exit /b 1
)
curl -s http://localhost:5057/ >nul 2>&1
if errorlevel 1 goto wait

REM — Ouvrir le navigateur —
echo Serveur demarre.
start http://localhost:5057/
echo.
echo Cadastre Tool tourne sur http://localhost:5057
echo Fermez cette fenetre pour arreter le serveur.
echo.
pause >nul

REM — Arrêter Flask —
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":5057 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)
echo Serveur arrete.
