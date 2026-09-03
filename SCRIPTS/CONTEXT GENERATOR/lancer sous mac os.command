#!/bin/bash
# Cadastre Tool — macOS
# Double-clic pour démarrer

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

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

# ── Environnement Python ────────────────────────────────────────────────────
# L'environnement virtuel (.venv) n'est JAMAIS placé dans le dossier de l'outil :
# celui-ci est souvent sur un disque réseau partagé, or un venv contient des
# binaires et des chemins propres à une machine (le partager entre postes le
# casse) et l'écriture de milliers de fichiers sur SMB est très lente.
# On le crée donc en local, dans le dossier standard "Application Support" de
# l'utilisateur (non synchronisé par iCloud, contrairement à ~/Documents).
# Un venv distinct par emplacement d'installation (empreinte du chemin) évite
# les collisions si l'outil est copié à plusieurs endroits.
VENV_ROOT="$HOME/Library/Application Support/CadastreTool"
VENV_ID=$(printf '%s' "$APP_DIR" | shasum | cut -c1-12)
VENV="$VENV_ROOT/venv-$VENV_ID"
mkdir -p "$VENV_ROOT"

if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c "import sys" &>/dev/null; then
    [ -d "$VENV" ] && { echo "Environnement inutilisable — recréation..."; rm -rf "$VENV"; }
    echo "Création de l'environnement Python (local à ce poste)..."
    if ! python3 -m venv "$VENV"; then
        osascript -e 'display alert "Cadastre Tool" message "Impossible de créer l’environnement Python." as critical'
        read -p "Appuyez sur Entrée pour fermer."
        exit 1
    fi
fi

# Installe les dépendances, PUIS vérifie qu'elles s'importent réellement : pip
# peut réussir tout en laissant un environnement incohérent (interpréteur changé,
# installation SMB à moitié faite, extension binaire cassée…). Si l'un des deux
# échoue, on reconstruit le venv de zéro et on réessaie une fois.
install_deps() {
    "$VENV/bin/pip" install -r requirements.txt --quiet --disable-pip-version-check 2>&1
}
verify_imports() {
    "$VENV/bin/python" - <<'PY' 2>&1
import sys
try:
    import flask, ezdxf, requests, pyproj, shapely  # noqa: F401
except Exception as e:
    print(f"echec import : {type(e).__name__}: {e}")
    sys.exit(1)
PY
}

echo "Vérification des dépendances..."
PIP_LOG=$(install_deps);      PIP_RC=$?
IMPORT_LOG=$(verify_imports); IMPORT_RC=$?

if [ $PIP_RC -ne 0 ] || [ $IMPORT_RC -ne 0 ]; then
    echo "Environnement Python incohérent — reconstruction complète..."
    rm -rf "$VENV"
    if ! python3 -m venv "$VENV"; then
        osascript -e 'display alert "Cadastre Tool" message "Impossible de créer l’environnement Python." as critical'
        read -p "Appuyez sur Entrée pour fermer."
        exit 1
    fi
    PIP_LOG=$(install_deps);      PIP_RC=$?
    IMPORT_LOG=$(verify_imports); IMPORT_RC=$?
fi

if [ $PIP_RC -ne 0 ] || [ $IMPORT_RC -ne 0 ]; then
    DETAIL=$(printf '%s\n%s' "$PIP_LOG" "$IMPORT_LOG" | tail -n 20 | sed 's/"/\\"/g')
    osascript -e "display alert \"Cadastre Tool — dépendances\" message \"Les bibliothèques Python (flask, ezdxf, requests, pyproj, shapely) n'ont pas pu être installées ou chargées.\n\n$DETAIL\" as critical"
    printf '%s\n%s\n' "$PIP_LOG" "$IMPORT_LOG"
    read -p "Appuyez sur Entrée pour fermer."
    exit 1
fi

PORT=5057
LOG=/tmp/cadastre-tool.log

# Arrêter un éventuel serveur déjà lancé sur le port (ignorer si échec, ex: process système)
lsof -ti:$PORT | xargs kill -9 2>/dev/null

# Démarrer Flask
"$VENV/bin/python" server.py > "$LOG" 2>&1 &
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
