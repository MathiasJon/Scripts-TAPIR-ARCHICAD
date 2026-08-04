# Cadastre Tool — Outil de téléchargement PCI pour agence d'architecture

## Objectif
Outil web local (macOS) permettant de sélectionner un périmètre sur une carte,
identifier les feuilles cadastrales PCI intersectantes, télécharger les DXF
correspondants depuis data.gouv.fr, et les fusionner en un seul fichier DXF.

## Stack
- Frontend : HTML/JS vanilla + Leaflet.js
- Backend : Python 3 + Flask (localhost:5057 — port choisi pour éviter le conflit avec AirPlay Receiver qui occupe le 5000 par défaut sur macOS)
- DXF : ezdxf
- APIs : API Carto IGN (apicarto.ign.fr), API Adresse (adresse.data.gouv.fr), data.gouv.fr

## Lancement
Double-clic sur `lancer.command` — démarre Flask en arrière-plan et ouvre le navigateur.

## Structure
```
cadastre-tool/
├── CLAUDE.md
├── lancer.command          # Double-clic pour lancer sur macOS
├── requirements.txt
├── server.py               # Backend Flask
└── frontend/
    └── index.html          # Interface Leaflet + JS
```

## Endpoints Flask
- `GET /` — sert index.html
- `POST /api/feuilles` — identifie les feuilles cadastrales (corps: {geojson: ...})
- `POST /api/download` — télécharge et fusionne les DXF (corps: {feuilles: [...]})
- `GET /api/download/<filename>` — sert le DXF fusionné

## Coordonnées
- Carte : WGS84 (EPSG:4326) pour Leaflet
- API Carto IGN : WGS84 en entrée
- DXF PCI téléchargés : Lambert-93 (EPSG:2154) — préservé tel quel dans la fusion

## URLs data.gouv.fr
Format DXF PCI : `https://cadastre.data.gouv.fr/bundler/cadastre-etalab/communes/{code_insee}/dxf/feuilles`
Ou par feuille : `https://cadastre.data.gouv.fr/bundler/cadastre-etalab/feuilles/{dept}/{commune}/{section}/{feuille}/dxf`
