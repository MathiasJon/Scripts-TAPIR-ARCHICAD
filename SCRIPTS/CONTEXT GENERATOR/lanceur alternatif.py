#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cadastre Tool — lanceur multiplateforme (Windows + macOS), en Python pur.

Alternative aux scripts « lancer sous… » quand ceux-ci sont bloqués (Gatekeeper
sur un .command téléchargé, .bat en quarantaine, etc.).

Utilisation :
  - Double-clic (ouvre « Python Launcher »), OU
  - Ouvrir ce fichier dans IDLE puis menu  Run > Run Module  (F5), OU
  - Terminal / invite de commandes, depuis ce dossier :
        python3 "lanceur alternatif.py"      (Windows :  py "lanceur alternatif.py")

Un fichier .py exécuté depuis IDLE ou le terminal ne déclenche PAS Gatekeeper.
Ce script n'utilise que la bibliothèque standard : rien à installer pour le lancer.
Il crée l'environnement Python, installe les dépendances, démarre le serveur et
ouvre le navigateur — puis arrête tout à la fermeture.
"""

import hashlib
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

PORT = 5057
REQUIRED = ("flask", "ezdxf", "requests", "pyproj", "shapely")

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
LOG = APP / "cadastre-tool.log"


def fail(msg):
    print("\n" + "=" * 64 + "\nERREUR\n" + "=" * 64 + "\n" + msg + "\n")
    try:
        input("Appuyez sur Entrée pour fermer…")
    except EOFError:
        pass
    sys.exit(1)


def main():
    if not (APP / "server.py").exists():
        fail("Dossier « app » introuvable à côté de « lanceur alternatif.py ».\n"
             "Décompressez l'archive en entier avant de lancer.")

    if sys.version_info < (3, 10):
        print(f"AVERTISSEMENT : Python {sys.version.split()[0]} détecté — "
              "cet outil est développé/testé pour Python 3.10+.\n")

    # ── Environnement virtuel : TOUJOURS local au poste ─────────────────────
    # Jamais dans le dossier de l'outil (souvent sur un disque réseau partagé :
    # un venv contient des chemins propres à une machine). Un venv distinct par
    # emplacement d'installation (empreinte du chemin).
    is_win = os.name == "nt"
    if is_win:
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / "Library" / "Application Support"
    venv_root = base / "CadastreTool"
    venv_id = hashlib.sha1(str(APP).encode("utf-8")).hexdigest()[:12]
    venv = venv_root / f"venv-{venv_id}"
    venv_py = venv / ("Scripts/python.exe" if is_win else "bin/python")

    def venv_works():
        if not venv_py.exists():
            return False
        return subprocess.run([str(venv_py), "-c", "import sys"],
                              capture_output=True).returncode == 0

    def build_venv():
        import shutil
        import venv as _venv
        if venv.exists():
            print("Environnement inutilisable — reconstruction…")
            shutil.rmtree(venv, ignore_errors=True)
        else:
            print("Création de l'environnement Python (local à ce poste)…")
        _venv.EnvBuilder(with_pip=True, clear=True).create(str(venv))

    def install_and_verify():
        p = subprocess.run(
            [str(venv_py), "-m", "pip", "install", "-r", str(APP / "requirements.txt"),
             "--quiet", "--disable-pip-version-check"],
            capture_output=True, text=True)
        if p.returncode != 0:
            return False, (p.stdout or "") + (p.stderr or "")
        chk = subprocess.run([str(venv_py), "-c", "import " + ", ".join(REQUIRED)],
                             capture_output=True, text=True)
        return chk.returncode == 0, chk.stderr or ""

    venv_root.mkdir(parents=True, exist_ok=True)

    # pip peut réussir tout en laissant un venv incohérent → on vérifie que les
    # modules s'importent VRAIMENT, et on reconstruit une fois si besoin.
    print("Vérification des dépendances…")
    if not venv_works():
        build_venv()
    ok, log = install_and_verify()
    if not ok:
        build_venv()
        ok, log = install_and_verify()
    if not ok:
        fail("Les bibliothèques Python (flask, ezdxf, requests, pyproj, shapely) "
             "n'ont pas pu être installées ou chargées :\n\n" + log[-1500:])

    # ── Démarrer le serveur ────────────────────────────────────────────────
    print("Démarrage du serveur…")
    with open(LOG, "w", encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen([str(venv_py), "server.py"], cwd=str(APP),
                                stdout=logf, stderr=subprocess.STDOUT)

    import urllib.request
    url = f"http://localhost:{PORT}/"
    ready = False
    for _ in range(60):
        time.sleep(0.5)
        if proc.poll() is not None:
            break
        try:
            urllib.request.urlopen(url, timeout=1).close()
            ready = True
            break
        except Exception:
            pass

    if not ready:
        if proc.poll() is None:
            proc.terminate()
        detail = ""
        try:
            detail = "\n\n--- log ---\n" + LOG.read_text(encoding="utf-8", errors="replace")[-1500:]
        except OSError:
            pass
        fail(f"Le serveur n'a pas démarré. Vérifiez qu'aucun autre programme "
             f"n'utilise le port {PORT}." + detail)

    webbrowser.open(url)
    print(f"\nCadastre Tool tourne sur {url}")
    print("Fermez cette fenêtre (ou Ctrl-C / « Interrompre » dans IDLE) pour arrêter.\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Serveur arrêté.")


if __name__ == "__main__":
    main()
