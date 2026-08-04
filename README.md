# Scripts TAPIR-ARCHICAD

Dépôt regroupant les scripts d'automatisation Archicad (via l'add-on [Tapir](https://github.com/ENZYME-APD/tapir-archicad-automation)) et les outils cadastraux associés, développés pour l'agence.

## Scripts

### [`SCRIPTS/CONTEXT GENERATOR`](SCRIPTS/CONTEXT%20GENERATOR)

Outil web local (Flask + Leaflet) qui permet de :
- sélectionner un périmètre cadastral sur une carte,
- identifier et télécharger les feuilles PCI (DXF) correspondantes depuis data.gouv.fr,
- générer directement dans Archicad, via Tapir : un maillage de terrain géoréférencé (RGE ALTI/LiDAR HD), les limites de parcelles cadastrales, et les bâtiments existants (hauteur BD TOPO), en coordonnées locales relatives à un point d'ancrage.

Lancement : voir `SCRIPTS/CONTEXT GENERATOR/lancer sous mac os.command` (macOS) ou `lancer sous windows.bat` (Windows). Détails dans `SCRIPTS/CONTEXT GENERATOR/CLAUDE.md`.
