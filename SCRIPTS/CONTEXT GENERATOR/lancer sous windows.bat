@echo off
chcp 65001 >nul
title Cadastre Tool

REM — Le serveur et ses fichiers sont dans le sous-dossier « app » (a la racine
REM   on ne garde que les deux lanceurs — pas de server.py double-cliquable par erreur).
if not exist "%~dp0app\server.py" (
    echo ERREUR : dossier "app" introuvable a cote du lanceur.
    echo Decompressez le ZIP en entier avant de lancer.
    pause
    exit /b 1
)
cd /d "%~dp0app"

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

REM — Avertissement (non bloquant) si Python < 3.10 : version cible de
REM   developpement, les versions anterieures peuvent buguer.
%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo AVERTISSEMENT : cet outil est developpe/teste pour Python 3.10+.
    echo   Une version anterieure peut fonctionner mais n'est pas garantie sans bug.
    echo.
)

REM — Environnement Python —
REM   L'environnement virtuel n'est JAMAIS place dans le dossier de l'outil :
REM   celui-ci est souvent sur un disque reseau partage, or un venv contient
REM   des binaires et des chemins propres a une machine (le partager entre
REM   postes le casse) et l'ecriture de milliers de fichiers sur le reseau est
REM   tres lente. On le cree en local dans %LOCALAPPDATA% (non synchronise, a la
REM   difference du dossier Documents avec OneDrive). Un venv distinct par
REM   emplacement d'installation (empreinte du chemin) evite les collisions.
for /f %%i in ('%PYTHON% -c "import hashlib,os;print(hashlib.sha1(os.getcwd().encode()).hexdigest()[:12])"') do set VENV_ID=%%i
set VENV=%LOCALAPPDATA%\CadastreTool\venv-%VENV_ID%
if not exist "%LOCALAPPDATA%\CadastreTool" md "%LOCALAPPDATA%\CadastreTool"

REM — Cree/repare le venv, installe les dependances PUIS verifie qu'elles
REM   s'importent reellement (pip peut reussir en laissant un venv incoherent :
REM   interpreteur change, install a moitie faite, extension binaire cassee...).
REM   Si l'un des deux echoue, on reconstruit le venv de zero et on reessaie.
echo.
echo Installation des dependances (patientez)...
call :ensure_venv
call :install_and_verify
if not "%DEPS_OK%"=="1" (
    echo Environnement Python incoherent - reconstruction complete...
    rmdir /s /q "%VENV%" 2>nul
    call :ensure_venv
    call :install_and_verify
)
if not "%DEPS_OK%"=="1" (
    echo.
    echo ERREUR : les bibliotheques Python ^(flask, ezdxf, requests, pyproj, shapely^)
    echo   n'ont pas pu etre installees ou chargees. Details :
    echo.
    "%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
    "%VENV%\Scripts\python.exe" -c "import flask, ezdxf, requests, pyproj, shapely"
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
set LOG=%~dp0app\cadastre-tool.log
if exist "%LOG%" del "%LOG%"
start "" /b "%VENV%\Scripts\python.exe" server.py > "%LOG%" 2>&1

REM — Attendre que le serveur soit prêt (max 30 s) —
set RETRY=0
:wait
ping -n 2 127.0.0.1 >nul
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
exit /b 0


:ensure_venv
REM   Cree le venv s'il manque, ou le supprime+recree s'il est casse.
if exist "%VENV%\Scripts\python.exe" (
    "%VENV%\Scripts\python.exe" -c "import sys" >nul 2>&1
    if not errorlevel 1 goto :eof
    echo   environnement inutilisable - recreation...
    rmdir /s /q "%VENV%" 2>nul
)
%PYTHON% -m venv "%VENV%"
if errorlevel 1 (
    echo ERREUR : Impossible de creer l'environnement virtuel.
    pause
    exit /b 1
)
goto :eof


:install_and_verify
REM   Installe requirements.txt PUIS verifie que tout s'importe. DEPS_OK=1 si OK.
set DEPS_OK=0
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check >nul 2>&1
if errorlevel 1 goto :eof
"%VENV%\Scripts\python.exe" -c "import flask, ezdxf, requests, pyproj, shapely" >nul 2>&1
if errorlevel 1 goto :eof
set DEPS_OK=1
goto :eof
