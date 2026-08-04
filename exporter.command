#!/bin/bash
# Génère un ZIP propre pour partage sur le Bureau
# Contient uniquement les fichiers essentiels, avec les droits corrects

cd "$(dirname "$0")"

DEST="$HOME/Desktop/cadastre-tool.zip"
TMP=$(mktemp -d)
PKG="$TMP/cadastre-tool"
mkdir -p "$PKG/frontend"

# Fichiers essentiels uniquement
cp server.py          "$PKG/"
cp requirements.txt   "$PKG/"
cp frontend/index.html "$PKG/frontend/"
cp "lancer sous mac os.command"  "$PKG/"
cp "lancer sous windows.bat"     "$PKG/"

# Instructions d'installation
cat > "$PKG/INSTALLER.txt" << 'EOF'
CADASTRE TOOL — Installation
==============================

MACOS
------
1. Double-clic sur « lancer sous mac os.command »
   Si macOS bloque le fichier : clic-droit → Ouvrir → Ouvrir quand même
   (protection Gatekeeper normale pour les fichiers téléchargés)

2. L'outil installe automatiquement ses dépendances Python
   (première ouverture : 1-2 minutes selon la connexion)

3. L'interface s'ouvre dans le navigateur sur http://localhost:5057

WINDOWS
--------
1. Double-clic sur « lancer sous windows.bat »
2. L'outil installe automatiquement ses dépendances Python
3. L'interface s'ouvre dans le navigateur sur http://localhost:5057

PRÉ-REQUIS
-----------
Python 3.10 ou supérieur — https://www.python.org
Sur macOS : disponible via https://www.python.org ou `brew install python3`
Sur Windows : cocher "Add Python to PATH" lors de l'installation

SI LA FENÊTRE DU NAVIGATEUR RESTE VIDE
----------------------------------------
Le script affiche maintenant un message d'erreur avec le détail si le
serveur n'a pas démarré (au lieu d'ouvrir une page vide). Causes fréquentes :
- Port 5057 déjà utilisé par un autre programme
- Pas de connexion internet lors de la 1ère installation (dépendances)
- macOS : fichier bloqué par Gatekeeper → clic-droit → Ouvrir
EOF

# Bit exécutable sur le .command
chmod +x "$PKG/lancer sous mac os.command"

# Création du ZIP (ditto préserve les permissions sur macOS)
rm -f "$DEST"
cd "$TMP"
ditto -c -k --sequesterRsrc --keepParent cadastre-tool "$DEST"

rm -rf "$TMP"

osascript -e "display notification \"cadastre-tool.zip créé sur le Bureau\" with title \"Export terminé\""
echo "✓ ZIP créé : $DEST"
