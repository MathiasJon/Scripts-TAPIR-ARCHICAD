"""
Diagnostic PEB — teste plusieurs sources de données autour d'Orly.
Lancer depuis le dossier cadastre-tool :
  .venv/bin/python debug_peb.py
"""
import requests, json

# BBOX Villeneuve-le-Roi / Orly (lon_min, lat_min, lon_max, lat_max)
W, S, E, N = 2.35, 48.72, 2.42, 48.76

print("=" * 60)
print("Test PEB autour d'Orly — Villeneuve-le-Roi 94290")
print("=" * 60)

# ── 1. Géorisques API ──────────────────────────────────────────
print("\n[1] Géorisques /api/v1 — tentatives PEB")
for endpoint in [
    'https://georisques.gouv.fr/api/v1/peb',
    'https://georisques.gouv.fr/api/v1/bruit_peb',
    'https://georisques.gouv.fr/api/v1/zonages_ppr',   # essai avec type_ppr
]:
    params = {'bbox': f'{W},{S},{E},{N}', 'page_size': 5}
    if 'zonages_ppr' in endpoint:
        params['type_ppr'] = 'PEB'
    try:
        r = requests.get(endpoint, params=params, timeout=10)
        count = len(r.json().get('features', [])) if r.ok else 0
        print(f"  {r.status_code} {endpoint.split('/')[-1]:25s} → {count} features")
        if r.ok and count:
            print(f"    Propriétés : {list(r.json()['features'][0]['properties'].keys())[:8]}")
    except Exception as e:
        print(f"  ERR {endpoint.split('/')[-1]:25s} → {e}")

# ── 2. Géorisques API OpenAPI — lister tous les endpoints ──────
print("\n[2] Géorisques — liste des endpoints disponibles")
try:
    r = requests.get('https://georisques.gouv.fr/api/v1/openapi.json', timeout=10)
    if r.ok:
        paths = list(r.json().get('paths', {}).keys())
        bruit = [p for p in paths if any(x in p.lower() for x in ('bruit', 'peb', 'bruit'))]
        print(f"  Endpoints liés au bruit/PEB : {bruit or 'aucun trouvé'}")
        print(f"  Total endpoints disponibles : {len(paths)}")
    else:
        print(f"  HTTP {r.status_code}")
except Exception as e:
    print(f"  ERR {e}")

# ── 3. GPU WFS — GetCapabilities ───────────────────────────────
print("\n[3] GPU WFS data.geopf.fr — types disponibles contenant 'peb' ou 'bruit'")
try:
    r = requests.get(
        'https://data.geopf.fr/wfs/ows',
        params={'SERVICE': 'WFS', 'VERSION': '2.0.0', 'REQUEST': 'GetCapabilities'},
        timeout=15
    )
    if r.ok:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.content)
        ns = 'http://www.opengis.net/wfs/2.0'
        ns2 = 'http://www.opengis.net/wfs'
        matches = []
        for ft in root.iter(f'{{{ns}}}FeatureType'):
            name_el = ft.find(f'{{{ns}}}Name')
            if name_el is not None and name_el.text:
                n = name_el.text.lower()
                if any(x in n for x in ('peb', 'bruit', 'sup', 'servitude', 'asa')):
                    matches.append(name_el.text)
        print(f"  Trouvé : {matches or 'aucun'}")
        # Afficher aussi tous les types GPU
        gpu_types = [ft.find(f'{{{ns}}}Name').text
                     for ft in root.iter(f'{{{ns}}}FeatureType')
                     if ft.find(f'{{{ns}}}Name') is not None
                     and 'GPU' in (ft.find(f'{{{ns}}}Name').text or '').upper()][:20]
        print(f"  Types GPU (max 20) : {gpu_types}")
    else:
        print(f"  HTTP {r.status_code}")
except Exception as e:
    print(f"  ERR {e}")

# ── 4. GPU WFS — essai direct avec GPU:peb ─────────────────────
print("\n[4] GPU WFS — essai direct sur différents noms de type")
for typename in ('GPU:peb', 'GPU:zone_peb', 'GPU:sup_asa4', 'GPU:servitude'):
    try:
        r = requests.get(
            'https://data.geopf.fr/wfs/ows',
            params={
                'SERVICE': 'WFS', 'VERSION': '2.0.0', 'REQUEST': 'GetFeature',
                'TYPENAMES': typename, 'SRSNAME': 'CRS:84',
                'BBOX': f'{W},{S},{E},{N}',
                'outputformat': 'application/json', 'count': 5,
            },
            timeout=10
        )
        ct = r.headers.get('Content-Type', '')
        if r.ok and 'json' in ct:
            feats = r.json().get('features', [])
            print(f"  {typename:25s} → {len(feats)} features")
            if feats:
                print(f"    Propriétés : {list(feats[0]['properties'].keys())[:8]}")
        else:
            print(f"  {typename:25s} → HTTP {r.status_code} | {r.text[:100]}")
    except Exception as e:
        print(f"  {typename:25s} → ERR {e}")

# ── 5. Géoportail Urbanisme REST ───────────────────────────────
print("\n[5] Géoportail Urbanisme — documents PEB autour d'Orly")
try:
    r = requests.get(
        'https://www.geoportail-urbanisme.gouv.fr/api/document',
        params={'typedocument': 'PEB', 'bbox': f'{W},{S},{E},{N}'},
        timeout=10
    )
    if r.ok:
        docs = r.json()
        print(f"  {len(docs)} document(s) PEB trouvé(s)")
        for d in docs[:3]:
            print(f"    → {d.get('name', '?')} | {d.get('partition', '?')}")
    else:
        print(f"  HTTP {r.status_code} | {r.text[:150]}")
except Exception as e:
    print(f"  ERR {e}")

print("\n" + "=" * 60)
