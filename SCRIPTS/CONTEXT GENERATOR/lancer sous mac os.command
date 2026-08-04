#!/bin/bash
# Cadastre Tool — macOS
# Double-clic pour démarrer

cd "$(dirname "$0")"

# Vérifier Python 3
if ! command -v python3 &>/dev/null; then
    osascript -e 'display alert "Python 3 requis" message "Installez Python 3 depuis https://www.python.org puis relancez." as critical'
    exit 1
fi

# Avertissement (non bloquant) si Python < 3.10 : version cible de développement,
# les versions antérieures n'ont pas toutes été testées et peuvent buguer.
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')
if [ "$PY_OK" != "1" ]; then
    echo "⚠ Python $PY_VER détecté — cet outil est développé/testé pour Python 3.10+."
    echo "  Des versions antérieures peuvent fonctionner mais ne sont pas garanties sans bug."
    echo ""
fi

# Créer l'environnement virtuel si besoin, puis synchroniser les dépendances
if [ ! -d ".venv" ]; then
    echo "Première utilisation — création de l'environnement..."
    python3 -m venv .venv
fi

echo "Vérification des dépendances..."
PIP_LOG=$(.venv/bin/pip install -r requirements.txt --quiet --disable-pip-version-check 2>&1)
if [ $? -ne 0 ]; then
    osascript -e "display alert \"Cadastre Tool — dépendances\" message \"L'installation des dépendances Python a échoué :\n\n$(echo "$PIP_LOG" | tail -n 20 | sed 's/"/\\"/g')\" as critical"
    echo "$PIP_LOG"
    read -p "Appuyez sur Entrée pour fermer."
    exit 1
fi

PORT=5057
LOG=/tmp/cadastre-tool.log

# Arrêter un éventuel serveur déjà lancé sur le port (ignorer si échec, ex: process système)
lsof -ti:$PORT | xargs kill -9 2>/dev/null

# Démarrer Flask
.venv/bin/python server.py > "$LOG" 2>&1 &
SERVER_PID=$!

# Attendre que le serveur soit prêt (max 10 s)
READY=0
for i in {1..20}; do
    sleep 0.5
    if curl -s http://localhost:$PORT/ > /dev/null 2>&1; then
        READY=1
        break
    fi
    # Le process Flask est mort (ex: port déjà occupé) — inutile d'attendre
    if ! kill -0 $SERVER_PID 2>/dev/null; then
        break
    fi
done

if [ "$READY" != "1" ]; then
    ERR=$(tail -n 20 "$LOG" 2>/dev/null)
    OCCUPIED=$(lsof -ti:$PORT 2>/dev/null)
    if [ -n "$OCCUPIED" ]; then
        MSG="Le port $PORT est déjà utilisé par un autre processus (PID $OCCUPIED) et n'a pas pu être libéré.\n\nSur Mac, vérifiez Réglages Système > Général > AirDrop et Handoff > Récepteur AirPlay (le désactiver ou changer son port).\n\nDétail du log :\n$ERR"
    else
        MSG="Le serveur n'a pas démarré. Détail du log :\n\n$ERR"
    fi
    osascript -e "display alert \"Cadastre Tool — échec du démarrage\" message \"$(echo "$MSG" | sed 's/"/\\"/g')\" as critical"
    echo "$MSG"
    echo "Log complet : $LOG"
    read -p "Appuyez sur Entrée pour fermer."
    kill $SERVER_PID 2>/dev/null
    exit 1
fi

open http://localhost:$PORT/

echo ""
echo "Cadastre Tool démarré."
echo "Appuyez sur Entrée pour arrêter le serveur et fermer."
read

kill $SERVER_PID 2>/dev/null
echo "Serveur arrêté."
