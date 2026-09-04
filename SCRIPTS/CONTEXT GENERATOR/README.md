<p align="center">
  <img src="../../docs/images/tapir_logo.png" width="110" alt="Tapir logo">
</p>

# Context Generator

**Importer automatiquement le contexte d'un projet — terrain, parcelles et bâtiments — dans Archicad, à partir des données publiques françaises.**

Vous dessinez une emprise sur une carte, l'outil récupère le cadastre (PCI), l'altimétrie (RGE ALTI / LiDAR HD) et les bâtiments (BD TOPO), puis génère directement dans Archicad, via l'add-on [Tapir](https://github.com/ENZYME-APD/tapir-archicad-automation) :

- un **maillage de terrain** qui suit le relief réel ;
- un **maillage par parcelle sélectionnée**, encastré dans le terrain ;
- un **maillage par bâtiment cadastral** (hauteur BD TOPO, 3 m par défaut) ;
- le **géoréférencement** du projet (Point de Repère calé sur les coordonnées Lambert‑93 réelles).

Le tout en **coordonnées locales**, autour d'un point d'origine que vous choisissez.

| Sélection dans l'outil | Résultat dans Archicad |
|---|---|
| ![Sélection](../../docs/images/webapp-selection.png) | ![Vue 3D](../../docs/images/archicad-3d-view.png) |

> ⚠️ **Données françaises uniquement** (IGN, BD TOPO, RGE ALTI, cadastre PCI). Adaptable à d'autres pays en remplaçant les sources dans `app/server.py`.

---

## Sommaire

- [Pré‑requis](#pré-requis)
- [Installation](#installation)
- [Premier lancement](#premier-lancement)
- [Utilisation pas à pas](#utilisation-pas-à-pas)
- [Fonds de plan et calques d'analyse](#fonds-de-plan-et-calques-danalyse)
- [Ce qui est créé dans Archicad](#ce-qui-est-créé-dans-archicad)
- [Limites et avertissements](#limites-et-avertissements)
- [Confidentialité](#confidentialité)
- [Dépannage](#dépannage)
- [Crédits](#crédits)

---

## Pré‑requis

| | |
|---|---|
| **Python** | 3.10 ou supérieur — [python.org](https://www.python.org) (l'installeur macOS fournit aussi IDLE ; sous Windows, cocher *« Add Python to PATH »*). |
| **Archicad** | ouvert, **avec un projet** (même vierge), et l'add‑on **Tapir** installé et actif — [installation de Tapir](https://github.com/ENZYME-APD/tapir-archicad-automation). |
| **Connexion Internet** | requise (appels aux API publiques IGN / BRGM). |

Aucune autre installation manuelle : l'outil crée son propre environnement Python et télécharge ses dépendances au premier lancement.

---

## Installation

Téléchargez le ZIP : **[cadastre-tool.zip](../../releases/latest/download/cadastre-tool.zip)** et décompressez‑le **en entier**.

```
cadastre-tool/
├── lancer sous mac os.command      ← macOS : double‑clic
├── lancer sous windows.bat         ← Windows : double‑clic
├── lanceur alternatif.py           ← si les deux précédents sont bloqués
├── INSTALLER.txt
└── app/   (le serveur et l'interface — ne rien lancer ici)
```

### macOS

Double‑clic sur **« lancer sous mac os.command »**.

Si macOS bloque le fichier (*« provient d'un développeur non identifié »*) :
clic‑droit → **Ouvrir** → **Ouvrir quand même**.
Sur **macOS 15+**, si cela ne suffit pas : *Réglages Système → Confidentialité et sécurité → « Ouvrir quand même »*.

### Windows

Double‑clic sur **« lancer sous windows.bat »**.

### Si rien ne fonctionne (quarantaine, antivirus, politique d'entreprise)

Utilisez **« lanceur alternatif.py »** — même résultat, tous systèmes :

- clic‑droit → **Ouvrir avec → IDLE**, puis menu **Run → Run Module** (`F5`) ;
- ou dans un terminal, depuis le dossier : `python3 "lanceur alternatif.py"` (Windows : `py "lanceur alternatif.py"`).

Un fichier `.py` exécuté depuis IDLE ou le terminal ne déclenche pas Gatekeeper.

**Autre option sans blocage :** récupérer le dossier via un disque partagé, une clé USB, AirDrop ou `git clone` — les fichiers ne sont alors jamais mis en quarantaine.

---

## Premier lancement

1. Le lanceur crée un environnement Python **local à votre poste** (dans *Application Support* sur macOS, `%LOCALAPPDATA%` sur Windows — jamais dans le dossier de l'outil, pour qu'il puisse vivre sur un disque réseau partagé sans casser).
2. Il installe les bibliothèques (`flask`, `ezdxf`, `pyproj`, `shapely`, `requests`) — **1 à 2 minutes** la première fois.
3. L'interface s'ouvre dans le navigateur : **http://localhost:5057**

Fermez la fenêtre du lanceur pour arrêter le serveur.

---

## Utilisation pas à pas

### 1 · Sélection

1. **Recherche d'adresse** ou **de parcelle** pour vous situer (optionnel).
2. Bouton **Rectangle** → tracez l'emprise du contexte à générer.
   - Un **gabarit A3** (1/500 ou 1/1000) peut être affiché comme repère de cadrage.
3. **Identifier les feuilles** → l'outil détermine les feuilles cadastrales couvertes (toutes sont incluses automatiquement).
4. **Passez en phase 2** (le tracé se verrouille).

### 2 · Génération

5. **Choisissez les parcelles à projeter** en cliquant dessus sur la carte.
   Chaque parcelle sélectionnée deviendra un maillage encastré dans le terrain (utile pour poser un projet dessus).
6. **Point d'ancrage** — placé automatiquement sur un coin des parcelles dès la première sélection. Glissez‑le pour l'ajuster : il s'aimante aux sommets cadastraux.
   👉 **Ce point devient l'origine locale (0,0) du projet dans Archicad.**
7. *(optionnel)* **Exporter adresse + nom du projet** : renseigne les infos projet dans Archicad.
8. *(optionnel)* **Télécharger le DXF fusionné** : le cadastre PCI de l'emprise en un seul DXF (hachures bâtiments, orthophoto IGN en option).
9. **Générer dans Archicad**.

### 3 · Altimétrie de référence (le « point 0 » local)

Une fenêtre demande **quelle altitude sert de Z = 0** pour l'implantation :

| Mode | Usage |
|---|---|
| **Minimum** *(recommandé)* | terrain et bâtiments toujours au‑dessus de 0. |
| **Moyenne / Maximum** | selon le besoin. |
| **Manuel** | valeur saisie, ou cliquez un **point de contour** affiché sur la carte pour reprendre son altitude. |
| **Point d'origine** | altitude au point d'ancrage — **cliquez le marqueur rouge sur la carte** pour le choisir. Les autres points affichent alors leur écart relatif (+0,20 m, −0,45 m…). |

Un dégradé bleu → rouge sur le contour des parcelles visualise le relief.

### 4 · Confirmation dans Archicad

Une **unique** boîte de dialogue Archicad demande confirmation **avant toute création**. Cliquez **Générer**. La génération se déroule ensuite sans autre interruption (barre de progression dans le navigateur, bouton *Annuler* disponible).

---

## Fonds de plan et calques d'analyse

**Fonds de plan** (sélecteur en haut à droite de la carte, sans clé d'API, choix mémorisé) :

- **Plan archi** *(défaut)* — rendu vectoriel N&B « plan d'architecte » : limites communales en rouge, végétation en vert, eau en bleu, gares et stations de métro affichées ;
- Plan IGN, OpenStreetMap, photo aérienne IGN, satellite.

**Calques superposables :**

- **Zones inondables** (aléas fréquents et centennaux — BRGM / Géorisques).
- **Desserte transports** — rayon de **500 m** autour des gares, stations de métro et de tramway (BD TOPO), en zone unie.
- **Monuments historiques** — emprise des monuments (nom au survol) + **périmètre d'abords officiel** (Périmètre Délimité des Abords, ou rayon de 500 m). Rappel : dans ce périmètre, l'avis de l'Architecte des Bâtiments de France est requis ; la co‑visibilité reste à son appréciation.

---

## Ce qui est créé dans Archicad

- **Géoréférencement** : le Point de Repère est positionné sur les coordonnées **Lambert‑93 réelles** du point d'ancrage. Tous les éléments générés sont en coordonnées locales relatives à ce point.
- **Maillage de terrain** : grille d'altitude RGE ALTI / LiDAR HD sur l'emprise, corps solide avec jupe. Un anti‑pics filtre la végétation résiduelle du LiDAR.
- **Maillage par parcelle sélectionnée** : encastré exactement dans le terrain (trou + comblement au même contour, raccord net).
- **Maillage par bâtiment cadastral** : emprise PCI, hauteur issue de la BD TOPO (**3 m par défaut** si aucune correspondance).
- **Opération d'éléments solides** *(décochable)* : soustraction terrain − bâtiments.
- **Polylignes de limites de parcelle** sur l'étage courant (annotation de plan).

---

## Limites et avertissements

- **France uniquement.** Hors de France, les sources ne renvoient rien.
- **Archicad doit être ouvert avec un projet** et Tapir actif — sinon message d'erreur explicite, rien n'est créé.
- **Hauteurs de bâtiment approximatives** : BD TOPO n'a pas toujours la donnée ; 3 m par défaut. À vérifier / ajuster ensuite.
- **Polylignes de parcelle en 2D** : elles sont posées sur l'étage courant, pas drapées sur le relief (limitation d'Archicad / Tapir).
- **Monuments historiques** : la donnée indique qu'un immeuble est protégé et son nom ; elle ne distingue pas *classé* / *inscrit* et dépend de ce que chaque commune a versé au Géoportail de l'Urbanisme. La **co‑visibilité n'est pas calculée** (appréciation ABF à l'œil nu).
- **API Carto de l'IGN** parfois momentanément indisponible → l'outil réessaie automatiquement et, pour les feuilles, reconstruit la liste à partir des parcelles.
- L'**opération d'éléments solides** peut faire buguer le moteur 3D d'Archicad sur des terrains très complexes → la décocher en cas de souci.
- Reconstruction chaussée / trottoir et annotations de hauteur : **fonctions en cours**, désactivées.

---

## Confidentialité

Tout s'exécute **en local** sur votre poste. L'outil interroge directement les API publiques (IGN Géoplateforme, API Carto, BRGM, adresse.data.gouv.fr) et l'add‑on Tapir sur `localhost`. **Aucune donnée n'est envoyée à un tiers**, aucun compte requis.

---

## Dépannage

| Symptôme | Solution |
|---|---|
| `No module named 'flask'` | Ne pas lancer `app/server.py` directement — utiliser un des trois lanceurs. |
| L'environnement semble cassé (import qui échoue) | Le lanceur le reconstruit automatiquement au lancement suivant. |
| macOS : `.command` bloqué | clic‑droit → Ouvrir, ou *Réglages Système → Confidentialité et sécurité*, ou utiliser `lanceur alternatif.py`. |
| Windows : le `.bat` se ferme aussitôt | Utiliser `lanceur alternatif.py` (clic‑droit → Ouvrir avec → IDLE → Run). |
| « Le serveur n'a pas démarré » / port 5057 occupé | Sur Mac, désactiver *Récepteur AirPlay* (*Réglages Système → Général → AirDrop et Handoff*) ou fermer l'appli qui occupe le port. |
| « Aucun projet ouvert dans Archicad » | Ouvrir ou créer un projet, vérifier que Tapir est actif, relancer. |
| Le venv prend de la place | Ancien(s) dossier(s) `venv-…` dans *Application Support/CadastreTool* (macOS) ou `%LOCALAPPDATA%\CadastreTool` (Windows) — supprimables à la main. |

---

## Crédits

Construit sur l'add‑on **[Tapir](https://github.com/ENZYME-APD/tapir-archicad-automation)** pour l'automatisation Archicad.
Données : IGN (Géoplateforme, API Carto, RGE ALTI, BD TOPO), DGFiP (cadastre PCI), BRGM (Géorisques), base Adresse Nationale.
