import json
import math
import os
import io
import tarfile
import re
import tempfile
import threading
import uuid
import zipfile
from flask import Flask, request, jsonify, send_file, send_from_directory
import requests
import ezdxf
try:
    from pyproj import Transformer as _Transformer
    _wgs84_to_l93 = _Transformer.from_crs('EPSG:4326', 'EPSG:2154', always_xy=True)
    _l93_to_wgs84 = _Transformer.from_crs('EPSG:2154', 'EPSG:4326', always_xy=True)
    _PYPROJ_OK = True
except Exception as _e:
    print(f'[pyproj] indisponible ({_e}) — emprise orthophoto = bbox des feuilles')
    _PYPROJ_OK = False

try:
    from shapely.geometry import shape as _shapely_shape, mapping as _shapely_mapping, Polygon as _ShapelyPolygon, Point as _ShapelyPoint
    from shapely.ops import unary_union as _unary_union, transform as _shapely_transform
    _SHAPELY_OK = True
except Exception as _e:
    print(f'[shapely] indisponible ({_e}) — génération chaussée/trottoir désactivée')
    _SHAPELY_OK = False

class _SkipBuildingsSentinel(Exception):
    """Signal interne : saute la génération des bâtiments (test terrain seul)."""


app = Flask(__name__, static_folder='frontend')

# Stockage temporaire en mémoire : token → (bytes, filename, mimetype)
_pending: dict = {}

# Annulation de la génération Archicad en cours (un seul flux à la fois, usage local)
_archicad_cancel = threading.Event()

# Avancement de la génération Archicad en cours, pour affichage en direct côté navigateur
_archicad_progress = {'stage': '', 'done': 0, 'total': 0}


# ── Client Tapir (add-on Archicad, API JSON locale) ─────────────────────────
# Archicad peut se rabattre sur un port suivant si 19723 est déjà occupé
# (ex: une instance précédente encore en train de se fermer) — on essaie les deux.
TAPIR_HOST = 'http://127.0.0.1'
TAPIR_PORTS = [19723, 19724, 19725, 19726]
_tapir_port_cache = None

def _archicad_run_command(command, parameters=None, timeout=30):
    """Appelle une commande native de l'API JSON Archicad (ex: API.GetProductInfo)."""
    global _tapir_port_cache
    ports_to_try = [_tapir_port_cache] if _tapir_port_cache else TAPIR_PORTS
    last_err = None
    for port in ports_to_try:
        try:
            resp = requests.post(
                f'{TAPIR_HOST}:{port}',
                json={'command': command, 'parameters': parameters or {}},
                timeout=timeout,
            )
            resp.raise_for_status()
            _tapir_port_cache = port
            break
        except requests.RequestException as e:
            last_err = e
            resp = None
    if resp is None:
        raise RuntimeError(f"Archicad injoignable sur les ports {TAPIR_PORTS} : {last_err}")
    data = resp.json()
    if not data.get('succeeded'):
        raise RuntimeError(data.get('error', {}).get('message', 'Erreur API Archicad inconnue'))
    return data.get('result')


def _tapir_run_command(command, parameters=None, timeout=90):
    """Appelle une commande de l'add-on Tapir (ex: CreateMeshes, CreateSlabs, SetGeoLocation)."""
    result = _archicad_run_command('API.ExecuteAddOnCommand', {
        'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': command},
        'addOnCommandParameters': parameters or {},
    }, timeout=timeout)
    response = result.get('addOnCommandResponse') if result else None
    if response and 'error' in response:
        raise RuntimeError(f"Tapir [{command}]: {response['error']}")
    return response

def _bbox_wgs84_to_l93(lon_min, lat_min, lon_max, lat_max):
    if not _PYPROJ_OK:
        return None
    x_min, y_min = _wgs84_to_l93.transform(lon_min, lat_min)
    x_max, y_max = _wgs84_to_l93.transform(lon_max, lat_max)
    return x_min, y_min, x_max, y_max


OVERPASS_MIRRORS = [
    'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
    'https://overpass-api.de/api/interpreter',
    'https://overpass.osm.ch/api/interpreter',
]


def _point_wgs84_to_l93(lon, lat):
    """Convertit un point WGS84 (lon, lat) en Lambert-93. None si pyproj indisponible."""
    if not _PYPROJ_OK:
        return None
    return _wgs84_to_l93.transform(lon, lat)


def _point_elevation(lon, lat):
    """Altitude RGE ALTI/LiDAR HD (m) d'un point WGS84, ou None si indisponible."""
    r = requests.get(
        'https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest/elevation.json',
        params={'lon': lon, 'lat': lat, 'resource': 'ign_rge_alti_wld'},
        timeout=15,
    )
    r.raise_for_status()
    elevations = r.json().get('elevations', [])
    return elevations[0].get('z') if elevations else None


def _batch_point_elevations(lonlat_points, batch_size=4000):
    """Altitudes RGE ALTI/LiDAR HD (m) pour une liste de points WGS84 (lon, lat).
    Retourne une liste de même longueur (None pour les points sans altitude)."""
    elevations = []
    for i in range(0, len(lonlat_points), batch_size):
        chunk = lonlat_points[i:i + batch_size]
        lon_str = '|'.join(f'{lon:.7f}' for lon, lat in chunk)
        lat_str = '|'.join(f'{lat:.7f}' for lon, lat in chunk)
        r = requests.post(
            'https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest/elevation.json',
            json={'lon': lon_str, 'lat': lat_str, 'resource': 'ign_rge_alti_wld', 'delimiter': '|'},
            timeout=60,
        )
        r.raise_for_status()
        elevations.extend(item.get('z') for item in r.json().get('elevations', []))
    return elevations


MAX_ELEVATION_GRID_POINTS = 9000  # garde-fou : au-delà, on augmente le pas de la grille

def _fetch_elevation_grid_l93(min_x, min_y, max_x, max_y, spacing_m=1.0):
    """
    Construit une grille régulière de points d'altitude (RGE ALTI/LiDAR HD, résolution
    ~1 m sur zone couverte par le LiDAR HD) sur l'emprise L93 donnée. Le pas est
    automatiquement agrandi si la grille dépasse MAX_ELEVATION_GRID_POINTS.
    Retourne (rows, spacing_m) — rows: liste de lignes, chaque ligne une liste de
    (x_l93, y_l93, z) ; z est None si l'altitude n'a pas pu être récupérée.
    """
    width, height = max_x - min_x, max_y - min_y
    n_estimate = (width / spacing_m + 1) * (height / spacing_m + 1)
    if n_estimate > MAX_ELEVATION_GRID_POINTS:
        spacing_m = max(spacing_m, (width * height / MAX_ELEVATION_GRID_POINTS) ** 0.5)

    def frange(start, stop, step):
        # Garantit que 'stop' est toujours inclus exactement : sinon, quand (stop-start)
        # n'est pas un multiple exact de step, la dernière ligne de grille peut tomber
        # jusqu'à 'step' avant le bord réel, laissant une bande sans point (artefacts
        # de triangulation observés en bordure max_x / max_y).
        vals, v = [], start
        while v < stop - 1e-6:
            vals.append(v)
            v += step
        vals.append(stop)
        return vals

    xs = frange(min_x, max_x, spacing_m)
    ys = frange(min_y, max_y, spacing_m)

    all_points = [(x, y) for y in ys for x in xs]
    lonlat = [_l93_to_wgs84.transform(x, y) for x, y in all_points]

    elevations = {}
    batch_size = 4000
    for i in range(0, len(lonlat), batch_size):
        chunk = lonlat[i:i + batch_size]
        lon_str = '|'.join(f'{lon:.7f}' for lon, lat in chunk)
        lat_str = '|'.join(f'{lat:.7f}' for lon, lat in chunk)
        # POST (pas GET) : la liste de points en GET dépasse vite la longueur d'URL max
        r = requests.post(
            'https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest/elevation.json',
            json={'lon': lon_str, 'lat': lat_str, 'resource': 'ign_rge_alti_wld', 'delimiter': '|'},
            timeout=60,
        )
        r.raise_for_status()
        for j, item in enumerate(r.json().get('elevations', [])):
            elevations[i + j] = item.get('z')

    rows = []
    idx = 0
    for y in ys:
        row = []
        for x in xs:
            row.append((x, y, elevations.get(idx)))
            idx += 1
        rows.append(row)
    rows = _fill_missing_grid_z(rows)
    return rows, spacing_m


def _fill_missing_grid_z(rows):
    """
    Comble les trous de couverture LiDAR (altitude None) par la moyenne des cases
    valides voisines, itérée jusqu'à convergence — jamais une valeur arbitraire (0),
    qui créerait un pli local de plusieurs mètres (confirmé cause de blocage du
    moteur 3D d'Archicad : contour invalide / auto-intersection).
    """
    n_rows = len(rows)
    n_cols = len(rows[0]) if rows else 0
    if n_rows == 0 or n_cols == 0:
        return rows

    grid = [[rows[j][i][2] for i in range(n_cols)] for j in range(n_rows)]
    if not any(v is None for row in grid for v in row):
        return rows

    for _ in range(20):
        new_grid = [row[:] for row in grid]
        remaining = 0
        for j in range(n_rows):
            for i in range(n_cols):
                if grid[j][i] is None:
                    neighbors = [
                        grid[nj][ni]
                        for dj, di in ((-1, 0), (1, 0), (0, -1), (0, 1))
                        for nj, ni in [(j + dj, i + di)]
                        if 0 <= nj < n_rows and 0 <= ni < n_cols and grid[nj][ni] is not None
                    ]
                    if neighbors:
                        new_grid[j][i] = sum(neighbors) / len(neighbors)
                    else:
                        remaining += 1
        grid = new_grid
        if remaining == 0:
            break

    # Dernier recours (zone entièrement sans donnée, jamais rencontré en pratique) :
    # valeur valide la plus proche dans la grille.
    valid_pts = [(j, i, grid[j][i]) for j in range(n_rows) for i in range(n_cols) if grid[j][i] is not None]
    if valid_pts:
        for j in range(n_rows):
            for i in range(n_cols):
                if grid[j][i] is None:
                    _, _, z = min(valid_pts, key=lambda t: (t[0] - j) ** 2 + (t[1] - i) ** 2)
                    grid[j][i] = z

    return [
        [(rows[j][i][0], rows[j][i][1], grid[j][i]) for i in range(n_cols)]
        for j in range(n_rows)
    ]


# ── Génération approximative chaussée / trottoir ────────────────────────────
# Approche : la chaussée est déduite d'un tampon autour de l'axe de route BD TOPO
# (largeur = attribut IGN), le "domaine public" est l'emprise du périmètre moins
# les parcelles cadastrales privées, et le trottoir est la différence des deux.
# C'est une reconstruction indicative (pas un relevé de bordure réel).

def _geojson_polygon_to_wkt_latlon(geom_dict):
    """Convertit un Polygon GeoJSON (lon,lat) en WKT avec l'ordre lat/lon attendu
    par le CQL_FILTER de ce service WFS (axe EPSG:4326 strict lat,lon)."""
    ring = geom_dict['coordinates'][0]
    pts = ','.join(f'{lat} {lon}' for lon, lat in ring)
    return f'POLYGON(({pts}))'


def _fetch_routes_bdtopo(perimeter_geojson):
    """Récupère les tronçons de route BD TOPO (IGN) intersectant le périmètre.
    Retourne une liste de (geometry GeoJSON WGS84, largeur_de_chaussee en m)."""
    wkt = _geojson_polygon_to_wkt_latlon(perimeter_geojson)
    params = {
        'SERVICE': 'WFS', 'VERSION': '2.0.0', 'REQUEST': 'GetFeature',
        'TYPENAMES': 'BDTOPO_V3:troncon_de_route',
        'OUTPUTFORMAT': 'application/json',
        'CQL_FILTER': f'INTERSECTS(geometrie,{wkt})',
        'COUNT': 2000,
    }
    resp = requests.get('https://data.geopf.fr/wfs/ows', params=params, timeout=30)
    resp.raise_for_status()
    routes = []
    for f in resp.json().get('features', []):
        geom = f.get('geometry')
        if not geom:
            continue
        largeur = f.get('properties', {}).get('largeur_de_chaussee') or 4.0
        routes.append((geom, float(largeur)))
    return routes


def _fetch_parcelles_geometries(perimeter_geojson):
    """Géométries (GeoJSON WGS84) des parcelles cadastrales intersectant le périmètre."""
    try:
        resp = requests.get(
            'https://apicarto.ign.fr/api/cadastre/parcelle',
            params={'geom': json.dumps(perimeter_geojson), '_limit': 2000},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []
    return [f['geometry'] for f in resp.json().get('features', []) if f.get('geometry')]


def _fetch_batiments_bdtopo(perimeter_geojson):
    """Récupère les bâtiments BD TOPO (IGN) intersectant le périmètre, avec leur hauteur.
    Retourne une liste de (geometry GeoJSON WGS84, hauteur en m)."""
    wkt = _geojson_polygon_to_wkt_latlon(perimeter_geojson)
    params = {
        'SERVICE': 'WFS', 'VERSION': '2.0.0', 'REQUEST': 'GetFeature',
        'TYPENAMES': 'BDTOPO_V3:batiment',
        'OUTPUTFORMAT': 'application/json',
        'CQL_FILTER': f'INTERSECTS(geometrie,{wkt})',
        'COUNT': 2000,
    }
    resp = requests.get('https://data.geopf.fr/wfs/ows', params=params, timeout=30)
    resp.raise_for_status()
    batiments = []
    for f in resp.json().get('features', []):
        geom = f.get('geometry')
        hauteur = f.get('properties', {}).get('hauteur')
        if geom and hauteur:
            batiments.append((geom, float(hauteur)))
    return batiments


def _extract_cadastral_building_polys(msp):
    """Extrait les polygones bâtiments (calques 3BATIDUR / 3BATILEGER) d'un modelspace ezdxf déjà en L93."""
    polys = []
    for entity in list(msp):
        if entity.dxf.layer not in ('3BATIDUR', '3BATILEGER'):
            continue
        dxftype = entity.dxftype()
        try:
            if dxftype == 'LWPOLYLINE':
                pts = [(p[0], p[1]) for p in entity.get_points()]
            elif dxftype == 'POLYLINE':
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            else:
                continue
            if len(pts) >= 3:
                poly = _ShapelyPolygon(pts).buffer(0)
                if not poly.is_empty:
                    polys.append(poly)
        except Exception:
            continue
    return polys


def _extract_cadastral_building_polys_from_feuilles(feuilles, projection='l93'):
    """Télécharge le DXF de chaque feuille et en extrait les polygones bâtiments cadastraux (L93)."""
    polys = []
    for f in feuilles:
        dxf_bytes, err = _download_dxf(
            f.get('commune_path', ''), f.get('com_abs', '000'),
            f.get('section', ''), f.get('numero', '01'), projection,
        )
        if dxf_bytes is None:
            continue
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
                tmp.write(dxf_bytes)
                tmp_path = tmp.name
            src_doc = ezdxf.readfile(tmp_path)
            polys.extend(_extract_cadastral_building_polys(src_doc.modelspace()))
        except Exception:
            continue
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    return polys


def _match_building_heights(cadastral_polys, bd_topo_l93, min_overlap_ratio=0.3):
    """
    Pour chaque polygone cadastral, associe la hauteur du bâtiment BD TOPO qui le
    recouvre le plus (plus grande surface d'intersection). Exige un recouvrement
    significatif (par défaut >30% de la surface cadastrale) pour éviter les faux
    appariements dus au léger décalage entre les deux sources.
    Retourne une liste de (poly, hauteur_ou_None) dans le même ordre que cadastral_polys.
    """
    results = []
    for poly in cadastral_polys:
        best_hauteur, best_area = None, 0.0
        for bd_poly, hauteur in bd_topo_l93:
            if not bd_poly.intersects(poly):
                continue
            inter_area = bd_poly.intersection(poly).area
            if inter_area > best_area:
                best_area = inter_area
                best_hauteur = hauteur
        results.append((poly, best_hauteur if best_area > min_overlap_ratio * poly.area else None))
    return results


def _add_building_height_labels(doc, msp, perimeter_geojson):
    """
    Associe à chaque emprise bâtiment cadastrale (calques 3BATIDUR / 3BATILEGER déjà
    présents dans le DXF fusionné, en L93) la hauteur du bâtiment BD TOPO qui la
    recouvre le plus, et ajoute un texte "H: X m" au centre (calque HAUTEUR_BATI_BDTOPO).
    Aucune extrusion 3D — juste une annotation en plan.
    Retourne le nombre de bâtiments annotés.
    """
    # Marge de recherche BD TOPO (20 m) : sa géométrie est parfois légèrement décalée
    # par rapport au cadastre et pourrait sinon tomber juste hors du périmètre dessiné,
    # faussant l'appariement pour les bâtiments près du bord.
    search_geojson = _shapely_to_wgs84_geojson(_to_l93(perimeter_geojson).buffer(0).buffer(20))
    batiments = _fetch_batiments_bdtopo(search_geojson)
    if not batiments:
        return 0
    bd_topo_l93 = [(_to_l93(geom).buffer(0), hauteur) for geom, hauteur in batiments]
    matches = _match_building_heights(_extract_cadastral_building_polys(msp), bd_topo_l93)

    layer_name = 'HAUTEUR_BATI_BDTOPO'
    if layer_name not in doc.layers:
        doc.layers.new(layer_name, dxfattribs={'color': 3})

    count = 0
    for poly, hauteur in matches:
        if hauteur is None:
            continue
        c = poly.centroid
        msp.add_text(
            f'H: {hauteur:.1f} m',
            dxfattribs={'layer': layer_name, 'height': 0.8, 'insert': (c.x, c.y)},
        )
        count += 1
    return count


def _only_polygonal(geom):
    """Filtre une géométrie shapely pour ne garder que sa partie Polygon/MultiPolygon."""
    if geom.is_empty:
        return geom
    if geom.geom_type in ('Polygon', 'MultiPolygon'):
        return geom
    if geom.geom_type == 'GeometryCollection':
        polys = [g for g in geom.geoms if g.geom_type in ('Polygon', 'MultiPolygon')]
        return _unary_union(polys) if polys else _ShapelyPolygon()
    return _ShapelyPolygon()


def _add_hatch_from_shapely(doc, msp, geom, layer_name, rgb):
    """Ajoute un ou plusieurs HATCH pleins depuis une géométrie shapely (Polygon/MultiPolygon)."""
    if layer_name not in doc.layers:
        doc.layers.new(layer_name, dxfattribs={'color': 7})
    polys = list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [geom]
    for poly in polys:
        if poly.is_empty or poly.area < 0.5:  # ignore les résidus < 0.5 m²
            continue
        hatch = msp.add_hatch(dxfattribs={'layer': layer_name})
        hatch.set_solid_fill()
        hatch.rgb = rgb
        hatch.paths.add_polyline_path(list(poly.exterior.coords), is_closed=True)
        for interior in poly.interiors:
            hatch.paths.add_polyline_path(list(interior.coords), is_closed=True)


def _l93_project(x, y, z=None):
    px, py = _wgs84_to_l93.transform(x, y)
    return (px, py) if z is None else (px, py, z)


def _to_l93(geom_dict):
    return _shapely_transform(_l93_project, _shapely_shape(geom_dict))


def _compute_voirie_geometries(perimeter_geojson, all_parcelles):
    """
    Calcule (en Lambert-93) la chaussée et le trottoir approximatifs à partir de
    la BD TOPO (axes de route + largeur) et des parcelles cadastrales du périmètre.
    Si une route traverse une parcelle, celle-ci est exclue du domaine privé
    (traitée comme du domaine public, pas une vraie parcelle bâtie).
    Retourne (chaussee, trottoir, perimeter_l93) — géométries shapely, vides si rien trouvé.
    """
    perimeter_l93 = _to_l93(perimeter_geojson).buffer(0)

    routes = _fetch_routes_bdtopo(perimeter_geojson)
    empty = _ShapelyPolygon()
    if not routes:
        return empty, empty, perimeter_l93
    route_lines_l93 = [(_to_l93(geom), largeur) for geom, largeur in routes]

    parcelle_geoms_l93 = []
    for p in all_parcelles:
        if not p.get('geometry'):
            continue
        pg = _to_l93(p['geometry']).buffer(0)
        crossed_by_route = any(line.intersects(pg) for line, _ in route_lines_l93)
        if not crossed_by_route:
            parcelle_geoms_l93.append(pg)

    parcelles_union = _unary_union(parcelle_geoms_l93) if parcelle_geoms_l93 else None
    domaine_public = perimeter_l93.difference(parcelles_union) if parcelles_union else perimeter_l93
    domaine_public = _only_polygonal(domaine_public)
    if domaine_public.is_empty:
        return empty, empty, perimeter_l93

    chaussee_raw = _unary_union([line.buffer(largeur / 2, cap_style='flat') for line, largeur in route_lines_l93])
    chaussee = _only_polygonal(chaussee_raw.intersection(domaine_public))
    trottoir = _only_polygonal(domaine_public.difference(chaussee))
    return chaussee, trottoir, perimeter_l93


def _generate_voirie(doc, msp, perimeter_geojson, all_parcelles):
    """
    Ajoute les calques CHAUSSEE_APPROX / TROTTOIR_APPROX au document DXF.
    Retourne (nombre de calques ajoutés, bounds (min_x, min_y, max_x, max_y) ou None).
    """
    chaussee, trottoir, perimeter_l93 = _compute_voirie_geometries(perimeter_geojson, all_parcelles)

    count = 0
    if not chaussee.is_empty:
        _add_hatch_from_shapely(doc, msp, chaussee, 'CHAUSSEE_APPROX', (90, 90, 90))
        count += 1
    if not trottoir.is_empty:
        _add_hatch_from_shapely(doc, msp, trottoir, 'TROTTOIR_APPROX', (200, 195, 185))
        count += 1

    bounds = perimeter_l93.bounds if not perimeter_l93.is_empty else None  # (min_x, min_y, max_x, max_y)
    return count, bounds


def _add_anchor_marker(doc, msp, anchor_x, anchor_y):
    """Ajoute un repère d'ancrage (POINT + TEXT) au point choisi par l'utilisateur sur la carte."""
    layer = 'REPERE_ANCRAGE_ARCHICAD'
    if layer not in doc.layers:
        doc.layers.new(layer, dxfattribs={'color': 1})
    msp.add_point((anchor_x, anchor_y, 0), dxfattribs={'layer': layer})
    label = f'ANCRAGE ARCHICAD — X={anchor_x:.2f} Y={anchor_y:.2f} (L93)'
    msp.add_text(label, dxfattribs={'layer': layer, 'height': 1.0, 'insert': (anchor_x, anchor_y)})

DXF_BASES = {
    'l93': 'https://cadastre.data.gouv.fr/data/dgfip-pci-vecteur/2026-03-01/dxf/feuilles',
    'cc':  'https://cadastre.data.gouv.fr/data/dgfip-pci-vecteur/2026-03-01/dxf-cc/feuilles',
}

# Cache des codes postaux pour éviter de ré-interroger geo.api.gouv.fr
_cp_cache: dict = {}

def _get_codes_postaux(commune_paths: list[str], code_deps: dict) -> dict:
    """
    Retourne un dict {commune_path: code_postal} pour une liste de commune_paths.
    Gère Paris (75), Lyon (69) et Marseille (13) par calcul direct ;
    les autres communes via geo.api.gouv.fr (requête groupée).
    """
    result = {}
    to_query = []

    for cp in commune_paths:
        if cp in _cp_cache:
            result[cp] = _cp_cache[cp]
            continue
        dep = code_deps.get(cp, cp[:2])
        # Arrondissements de Paris, Lyon, Marseille : code cadastral ≠ commune officielle
        if dep == '75' and cp.startswith('75') and int(cp[2:]) > 100:
            arr = int(cp[2:]) - 100
            code_postal = f'75{arr:03d}'
        elif dep == '69' and cp.startswith('69') and int(cp[2:]) > 380:
            arr = int(cp[2:]) - 380
            code_postal = f'69{arr:03d}'
        elif dep == '13' and cp.startswith('13') and int(cp[2:]) > 200:
            arr = int(cp[2:]) - 200
            code_postal = f'13{arr:03d}'
        else:
            to_query.append(cp)
            continue
        _cp_cache[cp] = code_postal
        result[cp] = code_postal

    # Requête groupée pour les communes normales
    if to_query:
        try:
            params = '&'.join(f'code={c}' for c in set(to_query))
            r = requests.get(
                f'https://geo.api.gouv.fr/communes?{params}&fields=codesPostaux',
                timeout=8
            )
            for item in r.json():
                cp_code = item.get('code', '')
                cps = item.get('codesPostaux', [])
                postal = cps[0] if cps else ''
                _cp_cache[cp_code] = postal
                result[cp_code] = postal
        except Exception:
            pass
        # Remplir les non-trouvés
        for cp in to_query:
            result.setdefault(cp, '')

    return result


@app.route('/')
def index():
    return send_from_directory('frontend', 'index.html')


@app.route('/api/feuilles', methods=['POST'])
def get_feuilles():
    """Identifie les feuilles cadastrales intersectant un polygone GeoJSON."""
    data = request.get_json()
    if not data or 'geojson' not in data:
        return jsonify({'error': 'Corps JSON requis avec clé "geojson"'}), 400

    geom = data['geojson']

    try:
        resp = requests.get(
            'https://apicarto.ign.fr/api/cadastre/feuille',
            params={'geom': json.dumps(geom), '_limit': 50},
            timeout=15
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({'error': f'Erreur API IGN: {str(e)}'}), 502

    features = resp.json().get('features', [])
    feuilles, seen = [], set()

    for f in features:
        p = f.get('properties', {})
        code_dep  = str(p.get('code_dep', ''))
        code_com  = str(p.get('code_com', ''))
        code_arr  = str(p.get('code_arr', code_com))
        com_abs   = str(p.get('com_abs', '000')).zfill(3)
        section   = str(p.get('section', ''))
        feuille_n = int(p.get('feuille', 0))
        numero    = str(feuille_n).zfill(2)
        nom_com   = p.get('nom_com', '')
        code_insee = p.get('code_insee', code_dep + code_com)

        # Choisir le bon code INSEE pour le chemin (arrondissement ou commune)
        commune_path = code_dep + (code_arr if code_arr not in ('', '000', code_com) else code_com)

        key = f"{commune_path}-{com_abs}-{section}-{numero}"
        if key in seen:
            continue
        seen.add(key)

        feuilles.append({
            'code_dep':      code_dep,
            'code_com':      code_com,
            'code_insee':    code_insee,
            'commune_path':  commune_path,
            'com_abs':       com_abs,
            'section':       section,
            'numero':        numero,
            'nom_com':       nom_com,
            'geometry':      f.get('geometry'),
        })

    # Enrichir avec les codes postaux (requête groupée)
    code_deps_map = {f['commune_path']: f['code_dep'] for f in feuilles}
    cp_map = _get_codes_postaux(list(code_deps_map.keys()), code_deps_map)
    for f in feuilles:
        f['code_postal'] = cp_map.get(f['commune_path'], '')

    LIMIT = 50
    return jsonify({
        'feuilles':  feuilles,
        'count':     len(feuilles),
        'at_limit':  len(feuilles) >= LIMIT,
    })


@app.route('/api/parcelles', methods=['POST'])
def get_parcelles():
    """
    Identifie des parcelles cadastrales.
    Deux modes, combinables :
    - body.geojson fourni  -> parcelles intersectant ce périmètre
    - body.section/numero fournis -> filtre en plus sur la section/numéro exact
      (nécessite quand même un périmètre/geom : l'API IGN ne renvoie rien sur un
       filtre code_insee+section+numero seul, sans geom).
    - body.numero peut contenir plusieurs numéros séparés par ";" (ex: "195;196;200") :
      une requête IGN par numéro, résultats fusionnés et dédoublonnés.
    """
    data = request.get_json() or {}
    geom       = data.get('geojson')
    code_insee = (data.get('code_insee') or '').strip()
    section    = (data.get('section') or '').strip().upper()
    numeros    = [n.strip() for n in (data.get('numero') or '').split(';') if n.strip()]

    if not geom and not code_insee:
        return jsonify({'error': 'Fournir "geojson" (périmètre) ou "code_insee" (recherche directe)'}), 400

    base_params = {'_limit': 2000}
    if geom:
        base_params['geom'] = json.dumps(geom)
    if code_insee:
        base_params['code_insee'] = code_insee
    if section:
        base_params['section'] = section

    # Une requête par numéro (l'API IGN ne supporte pas une liste dans "numero"),
    # ou une seule requête sans filtre numéro si aucun n'est fourni.
    param_variants = [dict(base_params, numero=n.zfill(4)) for n in numeros] or [base_params]

    features = []
    for params in param_variants:
        try:
            resp = requests.get('https://apicarto.ign.fr/api/cadastre/parcelle', params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            return jsonify({'error': f'Erreur API IGN: {str(e)}'}), 502
        features.extend(resp.json().get('features', []))

    parcelles, seen = [], set()

    for f in features:
        p = f.get('properties', {})
        code_dep  = str(p.get('code_dep', ''))
        code_com  = str(p.get('code_com', ''))
        code_arr  = str(p.get('code_arr', code_com))
        com_abs   = str(p.get('com_abs', '000')).zfill(3)
        sec       = str(p.get('section', ''))
        num       = str(p.get('numero', ''))
        nom_com   = p.get('nom_com', '')
        insee     = p.get('code_insee', code_dep + code_com)
        idu       = p.get('idu', '')

        commune_path = code_dep + (code_arr if code_arr not in ('', '000', code_com) else code_com)

        key = idu or f"{commune_path}-{com_abs}-{sec}-{num}"
        if key in seen:
            continue
        seen.add(key)

        parcelles.append({
            'idu':           idu,
            'code_dep':      code_dep,
            'code_com':      code_com,
            'code_insee':    insee,
            'commune_path':  commune_path,
            'com_abs':       com_abs,
            'section':       sec,
            'numero':        num,
            'nom_com':       nom_com,
            'contenance':    p.get('contenance', 0),
            'geometry':      f.get('geometry'),
        })

    code_deps_map = {p['commune_path']: p['code_dep'] for p in parcelles}
    cp_map = _get_codes_postaux(list(code_deps_map.keys()), code_deps_map)
    for p in parcelles:
        p['code_postal'] = cp_map.get(p['commune_path'], '')

    LIMIT = 2000
    return jsonify({
        'parcelles': parcelles,
        'count':     len(parcelles),
        'at_limit':  len(parcelles) >= LIMIT,
    })


def _shapely_to_wgs84_geojson(geom):
    """Reprojette une géométrie shapely (L93) en WGS84 et la retourne en dict GeoJSON."""
    def _project(x, y, z=None):
        lon, lat = _l93_to_wgs84.transform(x, y)
        return (lon, lat) if z is None else (lon, lat, z)
    return _shapely_mapping(_shapely_transform(_project, geom))


@app.route('/api/voirie', methods=['POST'])
def get_voirie():
    """
    Calcule la chaussée/trottoir approximatifs (BD TOPO + cadastre) pour prévisualisation
    sur la carte. Même calcul que celui utilisé à l'export DXF (voir _generate_voirie).
    """
    data = request.get_json() or {}
    perimeter_geojson = data.get('perimeter_geojson')
    all_parcelles = data.get('all_parcelles_in_perimeter') or []

    if not _SHAPELY_OK or not _PYPROJ_OK:
        return jsonify({'error': 'shapely/pyproj indisponible'}), 500
    if not perimeter_geojson:
        return jsonify({'error': 'Corps JSON requis avec clé "perimeter_geojson"'}), 400

    try:
        chaussee, trottoir, _ = _compute_voirie_geometries(perimeter_geojson, all_parcelles)
    except Exception as e:
        return jsonify({'error': f'Erreur calcul voirie: {e}'}), 502

    return jsonify({
        'chaussee':  _shapely_to_wgs84_geojson(chaussee) if not chaussee.is_empty else None,
        'trottoir':  _shapely_to_wgs84_geojson(trottoir) if not trottoir.is_empty else None,
    })


@app.route('/api/archicad/progress', methods=['GET'])
def archicad_progress():
    """Avancement de la génération Archicad en cours (pour affichage en direct)."""
    return jsonify(_archicad_progress)


@app.route('/api/archicad/cancel', methods=['POST'])
def archicad_cancel():
    """Demande l'arrêt d'une génération Archicad en cours (voir _archicad_cancel)."""
    _archicad_cancel.set()
    return jsonify({'ok': True})


@app.route('/api/archicad/generate', methods=['POST'])
def archicad_generate():
    """
    Génère dans Archicad, via Tapir :
    - géoréférencement (Point de Repère positionné sur les coordonnées L93 réelles
      du point d'ancrage choisi dans le navigateur — qui devient l'origine locale 0,0
      des éléments créés ci-dessous) ;
    - maillage de terrain (grille d'altitude RGE ALTI/LiDAR HD sur le périmètre) ;
    - dalles pour les bâtiments cadastraux du périmètre (hauteur BD TOPO, 3 m par
      défaut si aucune correspondance trouvée).
    """
    data = request.get_json() or {}
    if not _SHAPELY_OK or not _PYPROJ_OK:
        return jsonify({'error': 'shapely/pyproj indisponible'}), 500

    anchor_point = data.get('anchor_point')
    perimeter_geojson = data.get('perimeter_geojson')
    feuilles = data.get('feuilles') or []
    skip_buildings = bool(data.get('skip_buildings'))
    parcels_scope = data.get('parcels_scope') or 'all'
    selected_parcelles_geojson = data.get('parcelles_geojson')
    solid_operations = data.get('solid_operations', True)
    if not anchor_point:
        return jsonify({'error': "Point d'ancrage requis"}), 400
    if not perimeter_geojson:
        return jsonify({'error': 'Périmètre requis'}), 400

    _archicad_cancel.clear()
    _archicad_progress.update({'stage': 'Géolocalisation…', 'done': 0, 'total': 0})

    anchor_lon, anchor_lat = anchor_point['lon'], anchor_point['lat']
    anchor_x, anchor_y = _wgs84_to_l93.transform(anchor_lon, anchor_lat)
    anchor_z = _point_elevation(anchor_lon, anchor_lat) or 0.0

    errors = []

    # ── Géoréférencement : Point de Repère = ancrage (coordonnées L93 réelles) ──
    try:
        _tapir_run_command('SetGeoLocation', {
            'projectLocation': {
                'longitude': anchor_lon, 'latitude': anchor_lat,
                'altitude': anchor_z, 'north': 0.0,
            },
            'surveyPoint': {
                'position': {'eastings': anchor_x, 'northings': anchor_y, 'elevation': anchor_z},
                'geoReferencingParameters': {
                    'crsName': 'RGF93 / Lambert-93',
                    'description': 'RGF93 / Lambert-93 (EPSG:2154)',
                    'geodeticDatum': 'RGF93',
                    'verticalDatum': 'NGF-IGN69',
                    'mapProjection': 'Lambert-93',
                    'mapZone': 'France',
                },
            },
        })
    except Exception as e:
        errors.append(f'Géoréférencement : erreur — {e}')

    _archicad_progress.update({'stage': 'Maillage de terrain…', 'done': 0, 'total': 0})

    # ── Maillage de terrain (RGE ALTI/LiDAR HD, coordonnées locales) ────────
    # Référence locale Z=0 : le point le plus bas du terrain de la zone sélectionnée
    # (pas l'altitude du point d'ancrage) — tout le reste (terrain, bâtiments) est
    # exprimé en hauteur positive au-dessus de ce minimum.
    mesh_points = 0
    parcelles_count = 0
    z_ref = anchor_z  # repli si le maillage échoue : au moins les bâtiments restent cohérents
    try:
        perimeter_l93 = _to_l93(perimeter_geojson).buffer(0)
        min_x, min_y, max_x, max_y = perimeter_l93.bounds
        rows, spacing = _fetch_elevation_grid_l93(min_x, min_y, max_x, max_y, spacing_m=1.0)

        valid_z = [z for row in rows for _, _, z in row if z is not None]
        z_ref = min(valid_z) if valid_z else anchor_z

        def to_local(x, y, z):
            return {'x': x - anchor_x, 'y': y - anchor_y, 'z': (z - z_ref) if z is not None else 0.0}

        # Contour réel du périmètre (pas sa boîte englobante, qui déborde du tracé
        # car le Lambert-93 n'est pas aligné avec la grille lat/lon de la carte).
        perimeter_poly = (
            max(perimeter_l93.geoms, key=lambda g: g.area)
            if perimeter_l93.geom_type == 'MultiPolygon' else perimeter_l93
        )
        # Grille dans les deux sens (lignes + colonnes) : le Lambert-93 n'étant pas
        # aligné avec le tracé du périmètre, un bord peut être presque parallèle aux
        # lignes seules et ne les croiser sur aucune longue portion — donc aucun point
        # interpolé inséré par Archicad le long de ce bord. Les colonnes garantissent
        # qu'un bord, quelle que soit son orientation, croise toujours une ligne de grille.
        columns = [[rows[i][j] for i in range(len(rows))] for j in range(len(rows[0]))] if rows else []
        sublines = (
            [{'coordinates': [to_local(x, y, z) for x, y, z in row]} for row in rows]
            + [{'coordinates': [to_local(x, y, z) for x, y, z in col]} for col in columns]
        )

        # Étape 1 — maillage provisoire au contour = boîte englobante (couvre toute la
        # grille) avec les sublines. Étape 2 — on le réduit au contour réel via
        # ModifyMeshes : Archicad insère alors automatiquement, là où le nouveau
        # contour traverse la grille, des sommets à l'altitude correctement interpolée
        # (vérifié empiriquement). Les sommets qu'on donne nous-mêmes explicitement
        # (les coins du vrai contour) gardent en revanche notre valeur telle quelle —
        # on les interroge donc nous-mêmes individuellement pour avoir leur vraie cote.
        bbox_corners = [
            {'x': min_x - anchor_x, 'y': min_y - anchor_y, 'z': 0.0},
            {'x': max_x - anchor_x, 'y': min_y - anchor_y, 'z': 0.0},
            {'x': max_x - anchor_x, 'y': max_y - anchor_y, 'z': 0.0},
            {'x': min_x - anchor_x, 'y': max_y - anchor_y, 'z': 0.0},
        ]
        create_result = _tapir_run_command('CreateMeshes', {
            'meshesData': [{
                'level': 0.0,
                'polygonCoordinates': bbox_corners,
                'sublines': sublines,
                'showLines': False,
                'ridges': 'AllSmooth',
            }]
        })
        mesh_element_id = create_result['elements'][0]['elementId']

        # ── Calque de destination verrouillé ? ──────────────────────────────
        # Le maillage est créé sur le calque actif d'Archicad, quel qu'il soit —
        # s'il est verrouillé, les modifications suivantes (réduction du contour,
        # nettoyage du quadrillage via sublines) peuvent être silencieusement
        # ignorées ou incohérentes. On vérifie et on demande la permission de le
        # déverrouiller via une fenêtre native avant de poursuivre.
        try:
            created_details = _tapir_run_command(
                'GetDetailsOfElements', {'elements': [{'elementId': mesh_element_id}]}
            )
            layer_idx = created_details['detailsOfElements'][0].get('layerIndex')
            if layer_idx is not None:
                attrs_resp = _archicad_run_command('API.GetAttributesByType', {'attributeType': 'Layer'})
                layer_ids = [{'attributeId': a['attributeId']} for a in attrs_resp['attributeIds']]
                layer_attrs = _archicad_run_command('API.GetLayerAttributes', {'attributeIds': layer_ids})
                attrs = layer_attrs['attributes']
                if 0 <= layer_idx < len(attrs):
                    layer = attrs[layer_idx]['layerAttribute']
                    if layer.get('isLocked'):
                        alert = _tapir_run_command('ShowAlert', {
                            'alertType': 'warning',
                            'title': 'Cadastre Tool',
                            'message': f"Le calque « {layer.get('name', '')} » est verrouillé.",
                            'subMessage': 'Le déverrouiller pour permettre la génération ?',
                            'button1': 'Déverrouiller',
                            'button2': 'Continuer sans',
                        })
                        if alert and alert.get('clickedButton') == 1:
                            _tapir_run_command('CreateLayers', {
                                'layerDataArray': [{'index': str(layer_idx), 'isLocked': False}],
                                'overwriteExisting': True,
                            })
        except Exception as e:
            errors.append(f'Vérification du calque : erreur — {e}')

        # Sommets du contour tels que dessinés (pas densifiés) : Archicad insère lui-même
        # des points intermédiaires cohérents là où ce contour traverse la grille interne
        # du maillage (interpolation depuis la même donnée que la surface, donc lisse).
        # Pour les sommets qu'on donne nous-mêmes (les coins), on interpole aussi depuis
        # la grille (bilinéaire) plutôt que d'interroger leur altitude indépendamment :
        # une requête indépendante ramène le bruit propre au relevé LiDAR, incohérent
        # avec la surface lissée du maillage (constaté avec le contour densifié).
        xs_grid = [pt[0] for pt in rows[0]]
        ys_grid = [row[0][1] for row in rows]

        def bilinear_z(px, py):
            i = 0
            while i < len(xs_grid) - 2 and xs_grid[i + 1] < px:
                i += 1
            j = 0
            while j < len(ys_grid) - 2 and ys_grid[j + 1] < py:
                j += 1
            x0, x1 = xs_grid[i], xs_grid[i + 1]
            y0, y1 = ys_grid[j], ys_grid[j + 1]
            z00, z10 = rows[j][i][2], rows[j][i + 1][2]
            z01, z11 = rows[j + 1][i][2], rows[j + 1][i + 1][2]
            if None in (z00, z10, z01, z11):
                valid = [z for z in (z00, z10, z01, z11) if z is not None]
                return sum(valid) / len(valid) if valid else None
            tx = (px - x0) / (x1 - x0) if x1 != x0 else 0.0
            ty = (py - y0) / (y1 - y0) if y1 != y0 else 0.0
            return z00 * (1 - tx) * (1 - ty) + z10 * tx * (1 - ty) + z01 * (1 - tx) * ty + z11 * tx * ty

        real_corners = [
            {'x': x - anchor_x, 'y': y - anchor_y, 'z': to_local(x, y, bilinear_z(x, y))['z']}
            for x, y in perimeter_poly.exterior.coords
        ]

        _tapir_run_command('ModifyMeshes', {
            'meshesData': [{'elementId': mesh_element_id, 'meshData': {'polygonCoordinates': real_corners}}]
        })
        # Contour complet APRÈS réduction (avec les sommets intermédiaires insérés
        # automatiquement par Archicad aux croisements de grille, en plus de nos coins) —
        # à conserver pour réapplication après l'étape suivante, qui le perturbe.
        d = _tapir_run_command('GetDetailsOfElements', {'elements': [{'elementId': mesh_element_id}]})
        full_boundary = d['detailsOfElements'][0]['details']['polygonCoordinates']

        # Les coins ressortent comme des pics par rapport à leurs voisins immédiats
        # (vérifié : ~4 m au coin contre 0-2,5 m juste à côté). On corrige leur cote par
        # la moyenne de leurs deux voisins immédiats dans le contour (plutôt que
        # l'altitude "vraie" du coin, qui provoque visiblement un pli mal triangulé).
        corner_xy = {(round(c['x'], 3), round(c['y'], 3)) for c in real_corners}
        n = len(full_boundary)
        smoothed_boundary = list(full_boundary)
        for idx, pt in enumerate(full_boundary):
            if (round(pt['x'], 3), round(pt['y'], 3)) in corner_xy:
                prev_z = full_boundary[(idx - 1) % n]['z']
                next_z = full_boundary[(idx + 1) % n]['z']
                smoothed_boundary[idx] = {**pt, 'z': (prev_z + next_z) / 2}
        full_boundary = smoothed_boundary

        # Fusionner les sommets de contour quasi-confondus (< 5 cm) : l'insertion
        # automatique des croisements de grille peut déposer un nouveau sommet à
        # quelques centimètres d'un sommet déjà existant, créant un segment quasi nul
        # (triangle en lame de rasoir) — constaté cause plausible de blocage du moteur
        # 3D d'Archicad sur un maillage aussi dense (contour + zone gauche du terrain).
        MIN_BOUNDARY_SEG = 0.05
        merged_boundary = [full_boundary[0]]
        for pt in full_boundary[1:]:
            prev = merged_boundary[-1]
            if math.hypot(pt['x'] - prev['x'], pt['y'] - prev['y']) >= MIN_BOUNDARY_SEG:
                merged_boundary.append(pt)
        if len(merged_boundary) > 1 and math.hypot(
            merged_boundary[0]['x'] - merged_boundary[-1]['x'],
            merged_boundary[0]['y'] - merged_boundary[-1]['y'],
        ) < MIN_BOUNDARY_SEG:
            merged_boundary.pop()
        full_boundary = merged_boundary

        # ── Points d'intersection parcelles ↔ triangulation du terrain ──────────
        # Un maillage "helper" (même emprise/mêmes sublines que le terrain), réduit
        # (ModifyMeshes) au contour brut de CHAQUE parcelle : Archicad insère alors
        # lui-même, à chaque endroit où ce contour traverse la triangulation
        # existante, un sommet à l'altitude exacte de cette triangulation (même
        # mécanisme que pour le contour principal, cf. étape précédente). Ces points
        # servent à DEUX choses : (1) affiner le maillage de terrain global lui-même
        # (ajoutés à ses sublines à un seul point, donc invisibles comme lignes) et
        # (2) construire le contour de chaque maillage de parcelle individuel.
        parcelles_count = 0
        parcel_crossing_points = []  # points bruts (non découpés), pour le terrain global
        parcel_contours = []  # (clipped_polys, z_lookup) par parcelle, pour les maillages individuels
        try:
            bbox_poly = _ShapelyPolygon([
                (min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y),
            ])
            # Polygone 2D (coordonnées locales) de l'emprise réelle du terrain, tel que
            # généré (mêmes sommets que full_boundary) — sert de limite de découpe finale.
            terrain_local_poly = _ShapelyPolygon([(pt['x'], pt['y']) for pt in full_boundary]).buffer(0)

            def _z_on_terrain_edge(px, py):
                """Altitude interpolée le long de full_boundary, pour un point situé
                exactement sur ce contour (bord de découpe d'une parcelle)."""
                n_fb = len(full_boundary)
                best_dist, best_z = None, None
                for i in range(n_fb):
                    a, b = full_boundary[i], full_boundary[(i + 1) % n_fb]
                    dx, dy = b['x'] - a['x'], b['y'] - a['y']
                    length2 = dx * dx + dy * dy
                    if length2 < 1e-9:
                        continue
                    t = max(0.0, min(1.0, ((px - a['x']) * dx + (py - a['y']) * dy) / length2))
                    projx, projy = a['x'] + t * dx, a['y'] + t * dy
                    dist2 = (px - projx) ** 2 + (py - projy) ** 2
                    if best_dist is None or dist2 < best_dist:
                        best_dist, best_z = dist2, a['z'] + t * (b['z'] - a['z'])
                return best_z if best_dist is not None and best_dist < 0.01 else None

            # 'selected' : seulement les parcelles choisies par l'utilisateur dans
            # l'interface (envoyées telles quelles) — beaucoup plus rapide, chaque
            # parcelle nécessitant plusieurs appels Tapir (helper créé/réduit/lu).
            # 'all' (défaut) : toutes les parcelles du périmètre, via l'API cadastre.
            if parcels_scope == 'selected' and selected_parcelles_geojson:
                parcelles_geoms = selected_parcelles_geojson
            else:
                parcelles_geoms = _fetch_parcelles_geometries(perimeter_geojson)

            _archicad_progress.update({'stage': 'Parcelles…', 'done': 0, 'total': len(parcelles_geoms)})
            for parcel_idx, geom in enumerate(parcelles_geoms):
                _archicad_progress['done'] = parcel_idx + 1
                parcel_l93 = _to_l93(geom).buffer(0)
                # Limiter d'abord à l'emprise de la grille récupérée : le maillage helper
                # ne peut être réduit qu'à un contour contenu dans son étendue actuelle.
                parcel_in_grid = parcel_l93.intersection(bbox_poly)
                if parcel_in_grid.is_empty:
                    continue
                parts = list(parcel_in_grid.geoms) if parcel_in_grid.geom_type == 'MultiPolygon' else [parcel_in_grid]

                for part in parts:
                    if part.is_empty or part.geom_type != 'Polygon':
                        continue
                    raw_local = [
                        {'x': x - anchor_x, 'y': y - anchor_y, 'z': to_local(x, y, bilinear_z(x, y))['z']}
                        for x, y in part.exterior.coords
                    ]

                    # Un maillage "helper" FRAIS (pleine emprise + grille), recréé pour
                    # CHAQUE parcelle : Archicad n'insère les sommets de croisement de
                    # triangulation que lors d'un vrai rétrécissement depuis la grille
                    # complète — réutiliser un helper déjà rétréci sur la parcelle
                    # précédente ne redéclenche pas cette insertion pour un contour qui
                    # n'en est pas un sous-ensemble (constaté : une seule parcelle sur 25
                    # obtenait ses points de croisement, les autres gardaient leurs
                    # sommets bruts tels quels).
                    helper_result = _tapir_run_command('CreateMeshes', {
                        'meshesData': [{
                            'level': 0.0,
                            'polygonCoordinates': bbox_corners,
                            'sublines': sublines,
                            'showLines': False,
                            'ridges': 'AllSmooth',
                        }]
                    })
                    helper_id = helper_result['elements'][0]['elementId']
                    _tapir_run_command('ModifyMeshes', {
                        'meshesData': [{'elementId': helper_id, 'meshData': {'polygonCoordinates': raw_local}}]
                    })
                    d = _tapir_run_command('GetDetailsOfElements', {'elements': [{'elementId': helper_id}]})
                    parcel_full_boundary = d['detailsOfElements'][0]['details']['polygonCoordinates']
                    _tapir_run_command('DeleteElements', {'elements': [{'elementId': helper_id}]})

                    parcel_crossing_points.extend(parcel_full_boundary)

                    # Table de correspondance (x,y arrondis) → z, pour retrouver l'altitude
                    # exacte calculée par Archicad (sommets bruts + croisements de grille).
                    z_lookup = {(round(pt['x'], 3), round(pt['y'], 3)): pt['z'] for pt in parcel_full_boundary}

                    parcel_2d = _ShapelyPolygon([(pt['x'], pt['y']) for pt in parcel_full_boundary]).buffer(0)
                    clipped = parcel_2d.intersection(terrain_local_poly)
                    if clipped.is_empty:
                        continue
                    clipped_polys = list(clipped.geoms) if clipped.geom_type == 'MultiPolygon' else [clipped]
                    parcel_contours.append((clipped_polys, z_lookup))
        except Exception as e:
            errors.append(f'Points parcelles : erreur — {e}')

        # Une fois le contour façonné, on remplace les sublines de grille (lignes,
        # visibles même avec showLines à false) par leur équivalent en points isolés —
        # une "subline" à un seul point n'a rien à tracer comme ligne, mais reste prise
        # en compte pour la forme du maillage. Le relief intérieur est conservé, sans
        # ligne de grille visible. Les limites de parcelle, elles, sont ajoutées comme
        # de VRAIES sublines multi-points (donc visibles comme lignes) : chaque contour
        # de parcelle découpé (cf. ci-dessus) devient une subline dont chaque sommet
        # reprend l'altitude par la MÊME interpolation bilinéaire que le reste du
        # terrain (bilinear_z, sur la grille déjà chargée) — pas un mélange de sources
        # (correspondance exacte / projection sur bord / requête indépendante) : c'est
        # ce mélange qui faisait dévier la forme du terrain d'une parcelle à l'autre.
        parcel_sublines = []
        for clipped_polys, _z_lookup in parcel_contours:
            for poly in clipped_polys:
                if poly.is_empty or poly.geom_type != 'Polygon':
                    continue
                coords = [
                    {'x': x, 'y': y, 'z': to_local(x + anchor_x, y + anchor_y, bilinear_z(x + anchor_x, y + anchor_y))['z']}
                    for x, y in poly.exterior.coords
                ]
                if len(coords) >= 2:
                    parcel_sublines.append({'coordinates': coords})

        # Les points de grille/parcelle qui tombent exactement sur un sommet du
        # contour (même x,y) donnent parfois une z différente de celle du contour
        # (constaté : 404 conflits sur un terrain réel) — deux altitudes contradictoires
        # au même endroit, ce qui peut faire échouer/bloquer la triangulation
        # d'Archicad. Le contour fait autorité : on retire ces points en double plutôt
        # que de les laisser contredire sa cote.
        BOUNDARY_SNAP = 0.02
        boundary_xy = [(pt['x'], pt['y']) for pt in full_boundary]

        def _on_boundary(x, y):
            return any(abs(x - bx) < BOUNDARY_SNAP and abs(y - by) < BOUNDARY_SNAP for bx, by in boundary_xy)

        grid_points_local = [to_local(x, y, z) for row in rows for x, y, z in row]
        grid_points_local = [pt for pt in grid_points_local if not _on_boundary(pt['x'], pt['y'])]

        point_sublines = (
            [{'coordinates': [pt]} for pt in grid_points_local]
            + parcel_sublines
        )
        _tapir_run_command('ModifyMeshes', {
            'meshesData': [{'elementId': mesh_element_id, 'meshData': {'sublines': point_sublines}}]
        }, timeout=120)
        # Remplacer les sublines modifie de façon imprévisible certains sommets du
        # contour (vérifié empiriquement) — on réapplique donc le contour complet après.
        _tapir_run_command('ModifyMeshes', {
            'meshesData': [{'elementId': mesh_element_id, 'meshData': {'polygonCoordinates': full_boundary}}]
        })
        mesh_points = sum(len(row) for row in rows)

        # ── Maillages de parcelles individuels : DÉSACTIVÉS TEMPORAIREMENT ───────
        # (à la demande explicite : on revient aux seuls points d'intersection
        # injectés dans le maillage de terrain ci-dessus, sans créer de maillage
        # séparé par parcelle pour l'instant). parcel_contours reste calculé et
        # disponible si on les réactive.
        GENERATE_INDIVIDUAL_PARCEL_MESHES = False
        if GENERATE_INDIVIDUAL_PARCEL_MESHES:
            try:
                PARCEL_MESH_OFFSET = 0.02  # au-dessus du terrain, évite le conflit visuel (z-fighting)
                parcel_meshes_data = []
                for clipped_polys, z_lookup in parcel_contours:
                    for poly in clipped_polys:
                        if poly.is_empty or poly.geom_type != 'Polygon':
                            continue
                        coords = []
                        for x, y in poly.exterior.coords:
                            key = (round(x, 3), round(y, 3))
                            if key in z_lookup:
                                z = z_lookup[key]
                            else:
                                z = _z_on_terrain_edge(x, y)
                                if z is None:
                                    z = to_local(x + anchor_x, y + anchor_y, bilinear_z(x + anchor_x, y + anchor_y))['z']
                            coords.append({'x': x, 'y': y, 'z': z + PARCEL_MESH_OFFSET})
                        if len(coords) >= 4:
                            parcel_meshes_data.append({
                                'level': 0.0,
                                'polygonCoordinates': coords,
                                'showLines': True,
                                'ridges': 'UserDefined',
                            })

                if parcel_meshes_data:
                    _tapir_run_command('CreateMeshes', {'meshesData': parcel_meshes_data}, timeout=120)
                    parcelles_count = len(parcel_meshes_data)
            except Exception as e:
                errors.append(f'Maillages parcelles : erreur — {e}')
        else:
            parcelles_count = len(parcel_contours)
    except Exception as e:
        errors.append(f'Maillage terrain : erreur — {e}')

    # ── Bâtiments (hauteur BD TOPO, 3 m par défaut) — sous forme de maillages ──
    # (les dalles/CreateSlabs faisaient planter Archicad ; un maillage plat par
    # bâtiment, à la cote de la hauteur, donne une approximation de masse sans risque.)
    # Ne garde que les bâtiments dans le périmètre dessiné — les feuilles cadastrales
    # débordent souvent largement de la zone sélectionnée — et découpe ceux qui
    # chevauchent sa limite.
    building_count = 0
    solid_links_count = 0
    cancelled = False
    try:
        if skip_buildings:
            raise _SkipBuildingsSentinel()
        perimeter_l93_buildings = _to_l93(perimeter_geojson).buffer(0)
        cadastral_polys = _extract_cadastral_building_polys_from_feuilles(feuilles)

        # Recherche BD TOPO sur le périmètre élargi (marge de 20 m) : la géométrie
        # BD TOPO d'un bâtiment est parfois légèrement décalée par rapport au cadastre,
        # et pourrait sinon tomber juste hors d'un périmètre dessiné au plus juste,
        # faussant l'appariement (mauvaise hauteur, ou hauteur par défaut à tort).
        search_geojson = _shapely_to_wgs84_geojson(perimeter_l93_buildings.buffer(20))
        batiments = _fetch_batiments_bdtopo(search_geojson)
        bd_topo_l93 = [(_to_l93(geom).buffer(0), hauteur) for geom, hauteur in batiments]
        matches = _match_building_heights(cadastral_polys, bd_topo_l93)

        building_parts = []  # (part, hauteur)
        for poly, hauteur in matches:
            if not poly.intersects(perimeter_l93_buildings):
                continue
            clipped = _only_polygonal(poly.intersection(perimeter_l93_buildings))
            if clipped.is_empty:
                continue
            parts = list(clipped.geoms) if clipped.geom_type == 'MultiPolygon' else [clipped]
            for part in parts:
                if part.exterior is not None and len(part.exterior.coords) >= 4:
                    building_parts.append((part, hauteur if hauteur else 3.0))

        # Altitude réelle du sol sous chaque bâtiment : le point le plus bas du
        # maillage de terrain DANS l'emprise du contour du bâtiment (pas le centroïde,
        # pas une requête altimétrique indépendante — on reprojette le contour sur la
        # même grille/interpolation que le terrain généré, pour rester cohérent avec
        # sa forme réelle telle que posée dans Archicad).
        def _min_terrain_z_under_footprint(poly_l93):
            minx, miny, maxx, maxy = poly_l93.bounds
            zs = []
            for row in rows:
                for x, y, z in row:
                    if z is None or x < minx or x > maxx or y < miny or y > maxy:
                        continue
                    if poly_l93.covers(_ShapelyPoint(x, y)):
                        zs.append(z)
            for x, y in poly_l93.exterior.coords:
                z = bilinear_z(x, y)
                if z is not None:
                    zs.append(z)
            return min(zs) if zs else None

        ground_z_by_part = {
            i: _min_terrain_z_under_footprint(part) for i, (part, _h) in enumerate(building_parts)
        }

        # Confirmation dans Archicad même avant de lancer la boucle (potentiellement
        # longue) : donne un point d'arrêt natif, en plus du bouton "Annuler" du navigateur.
        if building_parts:
            try:
                alert = _tapir_run_command('ShowAlert', {
                    'alertType': 'information',
                    'title': 'Cadastre Tool',
                    'message': f'{len(building_parts)} bâtiment(s) vont être générés (maillages).',
                    'subMessage': 'Continuer la génération ?',
                    'button1': 'Continuer',
                    'button2': 'Annuler',
                })
                if alert and alert.get('clickedButton') == 2:
                    cancelled = True
                    building_parts = []
            except Exception:
                pass  # ShowAlert indisponible (version Tapir plus ancienne) : on continue sans confirmation

        _archicad_progress.update({'stage': 'Bâtiments…', 'done': 0, 'total': len(building_parts)})

        def _mesh_payload(part, h, i):
            ground_z = ground_z_by_part.get(i)
            base_z = (ground_z - z_ref) if ground_z is not None else 0.0
            # z des sommets à 0 : Archicad additionne 'level' et le z de chaque sommet
            # (confirmé empiriquement — les deux à la même valeur donnait 2x la hauteur).
            # 'level' seul porte donc la cote du toit.
            coords = [{'x': x - anchor_x, 'y': y - anchor_y, 'z': 0.0} for x, y in part.exterior.coords]
            # skirtLevel = profondeur de la jupe sous 'level' (confirmé empiriquement :
            # bas de la jupe = level - skirtLevel). En la réglant à la hauteur du
            # bâtiment, le bas tombe exactement sur l'altitude locale du terrain,
            # donnant un vrai volume posé au sol avec un seul maillage.
            return {
                'level': base_z + h,
                'polygonCoordinates': coords,
                'skirtType': 'SolidBodyWithSkirt',
                'skirtLevel': h,
            }

        building_element_ids = []
        try:
            # Tous les bâtiments en un seul appel — bien plus rapide qu'une requête
            # par bâtiment. Repli sur la boucle une par une si l'appel groupé échoue.
            meshes_data = [_mesh_payload(part, h, i) for i, (part, h) in enumerate(building_parts)]
            if meshes_data:
                create_res = _tapir_run_command('CreateMeshes', {'meshesData': meshes_data})
                building_element_ids = [e['elementId'] for e in create_res.get('elements', [])]
                building_count = len(meshes_data)
                _archicad_progress['done'] = building_count
        except Exception as e:
            errors.append(f"Création groupée échouée ({e}) — reprise bâtiment par bâtiment.")
            for i, (part, h) in enumerate(building_parts):
                if _archicad_cancel.is_set():
                    cancelled = True
                    break
                _archicad_progress['done'] = i + 1
                try:
                    create_res = _tapir_run_command('CreateMeshes', {'meshesData': [_mesh_payload(part, h, i)]})
                    building_element_ids.extend(e['elementId'] for e in create_res.get('elements', []))
                    building_count += 1
                except Exception as e2:
                    errors.append(f'Bâtiment ignoré (erreur maillage) : {e2}')

        # ── Opération d'éléments solides : terrain (cible) - bâtiments (opérateurs) ──
        # 'SubtractionUpwards' = le volume de l'opérateur est projeté vers le haut à
        # travers la cible (extrusion vers le haut), creusant le terrain pour
        # accueillir chaque bâtiment plutôt que de le laisser simplement posé dessus.
        terrain_element_id = locals().get('mesh_element_id')
        if solid_operations and terrain_element_id and building_element_ids:
            try:
                solid_links = [
                    {'targetId': terrain_element_id, 'operatorId': bid, 'operation': 'SubtractionUpwards'}
                    for bid in building_element_ids
                ]
                _tapir_run_command('CreateSolidElementLinks', {'solidLinks': solid_links}, timeout=60)
                solid_links_count = len(solid_links)
            except Exception as e:
                errors.append(f'Opération solide terrain/bâtiments : erreur — {e}')
    except _SkipBuildingsSentinel:
        pass
    except Exception as e:
        errors.append(f'Bâtiments : erreur — {e}')

    _archicad_progress.update({'stage': 'Terminé', 'done': 0, 'total': 0})

    return jsonify({
        'mesh_points':  mesh_points,
        'parcelles':    parcelles_count,
        'buildings':    building_count,
        'solid_links':  solid_links_count,
        'cancelled':    cancelled,
        'errors':       errors,
    })


@app.route('/api/arbres', methods=['GET'])
def get_arbres():
    """
    Proxy vers Overpass (OpenStreetMap) pour récupérer les arbres (natural=tree)
    dans une emprise donnée. Nécessaire car Overpass ne renvoie pas d'en-têtes CORS
    (un fetch direct depuis le navigateur échouerait).
    """
    try:
        lat_min = float(request.args['lat_min'])
        lon_min = float(request.args['lon_min'])
        lat_max = float(request.args['lat_max'])
        lon_max = float(request.args['lon_max'])
    except (KeyError, ValueError):
        return jsonify({'error': 'Paramètres requis : lat_min, lon_min, lat_max, lon_max'}), 400

    query = (
        f'[out:json][timeout:20];'
        f'node["natural"="tree"]({lat_min},{lon_min},{lat_max},{lon_max});'
        f'out;'
    )
    resp = None
    last_err = None
    for base_url in OVERPASS_MIRRORS:
        try:
            resp = requests.post(
                base_url, data={'data': query}, timeout=25,
                headers={'User-Agent': 'cadastre-tool/1.0 (agence architecture)'},
            )
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            last_err = e
            resp = None
            continue

    if resp is None:
        return jsonify({'error': f'Erreur Overpass (tous miroirs indisponibles): {last_err}'}), 502

    elements = resp.json().get('elements', [])
    arbres = []
    for el in elements:
        if el.get('type') != 'node':
            continue
        tags = el.get('tags', {})
        arbres.append({
            'lat':     el.get('lat'),
            'lon':     el.get('lon'),
            'espece':  tags.get('species:fr') or tags.get('species') or tags.get('genus', ''),
            'hauteur': tags.get('height', ''),
        })

    return jsonify({'arbres': arbres, 'count': len(arbres)})


def _patch_extents(content, min_x, min_y, max_x, max_y):
    """Corrige $EXTMIN/$EXTMAX dans le contenu DXF texte (ezdxf les réinitialise à ±1e20)."""
    def replace_point(text, varname, x, y):
        pattern = (
            r'(  9\r?\n\s*\$' + varname + r'\r?\n'
            r' 10\r?\n)[^\n]+(\r?\n'
            r' 20\r?\n)[^\n]+(\r?\n'
            r' 30\r?\n)[^\n]+'
        )
        repl = r'\g<1>' + f'{x:.6f}' + r'\g<2>' + f'{y:.6f}' + r'\g<3>' + '0.0'
        return re.sub(pattern, repl, text, count=1)

    content = replace_point(content, 'EXTMIN', min_x, min_y)
    content = replace_point(content, 'EXTMAX', max_x, max_y)
    return content


def _download_dxf(commune_path, com_abs, section, numero, projection='l93'):
    """
    Télécharge et extrait le DXF d'une feuille depuis cadastre.data.gouv.fr.
    projection: 'l93' (Lambert 93) ou 'cc' (Coniques Conformes).
    Retourne les bytes du DXF ou None si non trouvé.
    """
    dept = commune_path[:2]
    prefix = 'dxf-cc' if projection == 'cc' else 'dxf'
    base   = DXF_BASES.get(projection, DXF_BASES['l93'])
    filename = f'{prefix}-{commune_path}{com_abs}{section}{numero}.tar.bz2'
    url = f'{base}/{dept}/{commune_path}/{filename}'

    try:
        resp = requests.get(url, timeout=60)
        if resp.status_code != 200:
            return None, f'HTTP {resp.status_code} pour {filename}'
    except requests.RequestException as e:
        return None, str(e)

    try:
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode='r:bz2') as tf:
            for member in tf.getmembers():
                if member.name.upper().endswith('.DXF'):
                    return tf.extractfile(member).read(), None
        return None, 'Aucun fichier DXF dans l\'archive'
    except tarfile.TarError as e:
        return None, f'Erreur archive: {e}'


_ortho_layer_name: str | None = None

def _discover_ortho_layer() -> str:
    """Découvre dynamiquement le calque orthophoto courant via GetCapabilities."""
    global _ortho_layer_name
    if _ortho_layer_name:
        return _ortho_layer_name

    PREFERRED = ['HR.ORTHOIMAGERY.ORTHOPHOTOS', 'ORTHOIMAGERY.ORTHOPHOTOS']
    BLOCKLIST  = ('HIST', 'ARCHIV', 'ANCIEN')

    try:
        r = requests.get(
            'https://data.geopf.fr/wms-r',
            params={'SERVICE': 'WMS', 'VERSION': '1.3.0', 'REQUEST': 'GetCapabilities'},
            timeout=15
        )
        if r.ok:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(r.content)
            # Ne lire que les <Name> directement dans un <Layer>
            ortho_layers = []
            for layer_el in root.iter('{http://www.opengis.net/wms}Layer'):
                name_el = layer_el.find('{http://www.opengis.net/wms}Name')
                if name_el is not None and name_el.text and 'ORTHO' in name_el.text.upper():
                    ortho_layers.append(name_el.text)
            print(f'[wms] calques ortho disponibles : {ortho_layers}')
            # 1. Favoris exacts
            for p in PREFERRED:
                if p in ortho_layers:
                    _ortho_layer_name = p
                    print(f'[wms] calque sélectionné : {p}')
                    return p
            # 2. Premier calque courant (sans historique)
            for n in ortho_layers:
                if not any(b in n.upper() for b in BLOCKLIST):
                    _ortho_layer_name = n
                    print(f'[wms] calque sélectionné : {n}')
                    return n
    except Exception as e:
        print(f'[wms-caps] {e}')

    _ortho_layer_name = 'HR.ORTHOIMAGERY.ORTHOPHOTOS'
    return _ortho_layer_name


def _fetch_ortho_wms(min_x, min_y, max_x, max_y, max_dim=4096):
    """
    Télécharge l'orthophoto IGN (WMS EPSG:2154) sur le bbox Lambert-93 fourni.
    Retourne (bytes, error, width_px, height_px).
    """
    dx, dy = max_x - min_x, max_y - min_y
    if dx <= 0 or dy <= 0:
        return None, 'Bbox invalide', 0, 0

    if dx >= dy:
        w, h = max_dim, max(1, round(dy * max_dim / dx))
    else:
        h, w = max_dim, max(1, round(dx * max_dim / dy))

    layer = _discover_ortho_layer()
    params = {
        'SERVICE': 'WMS', 'VERSION': '1.3.0', 'REQUEST': 'GetMap',
        'LAYERS': layer, 'STYLES': '',
        'CRS': 'EPSG:2154',
        'BBOX': f'{min_x},{min_y},{max_x},{max_y}',
        'WIDTH': w, 'HEIGHT': h, 'FORMAT': 'image/jpeg',
    }
    url = 'https://data.geopf.fr/wms-r'
    print(f'[ortho] GET {url} BBOX={params["BBOX"]} {w}x{h}px')
    try:
        r = requests.get(url, params=params, timeout=60)
        print(f'[ortho] réponse HTTP {r.status_code} Content-Type={r.headers.get("Content-Type")}')
        if r.status_code != 200:
            body = r.text[:400] if r.text else ''
            print(f'[ortho] corps erreur : {body}')
            return None, f'WMS HTTP {r.status_code} — {body}', 0, 0
        if 'image' not in r.headers.get('Content-Type', ''):
            preview = r.text[:300] if r.text else ''
            print(f'[ortho] corps non-image : {preview}')
            return None, f'WMS non-image ({r.headers.get("Content-Type")}) : {preview}', 0, 0
        print(f'[ortho] image reçue : {len(r.content)} octets')
        return r.content, None, w, h
    except requests.RequestException as e:
        print(f'[ortho] erreur réseau : {e}')
        return None, str(e), 0, 0


def _add_image_entity(doc, msp, jpeg_name, min_x, min_y, max_x, max_y, w, h):
    """Insère une entité IMAGE (référence externe JPEG) dans le DXF."""
    if 'ORTHOPHOTO_IGN' not in doc.layers:
        doc.layers.new('ORTHOPHOTO_IGN', dxfattribs={'color': 7})
    image_def = doc.add_image_def(filename=jpeg_name, size_in_pixel=(w, h))
    msp.add_image(
        insert=(min_x, min_y, 0),
        size_in_units=(max_x - min_x, max_y - min_y),
        image_def=image_def,
        rotation=0,
        dxfattribs={'layer': 'ORTHOPHOTO_IGN'},
    )


def _add_hatches(doc, msp, force_color=True):
    """
    Génère des hachures solides à partir des polylignes sur 3BATIDUR et 3BATILEGER.
    - 3BATIDUR  → HACHURES_BATIDUR   : noir (RGB 0,0,0 si force_color, sinon bylayer)
    - 3BATILEGER → HACHURES_BATILEGER : gris (RGB 128,128,128 si force_color, sinon bylayer)
    Retourne le nombre de hachures créées.
    """
    LAYER_CONFIG = {
        '3BATIDUR':   ('HACHURES_BATIDUR',   (0,   0,   0)),
        '3BATILEGER': ('HACHURES_BATILEGER',  (128, 128, 128)),
    }
    if 'HACHURES_BATIDUR' not in doc.layers:
        doc.layers.new('HACHURES_BATIDUR',   dxfattribs={'color': 7})
    if 'HACHURES_BATILEGER' not in doc.layers:
        doc.layers.new('HACHURES_BATILEGER', dxfattribs={'color': 9})

    # Collecter les polylignes avant d'ajouter des entités (évite la modification en cours d'itération)
    pending = []
    for entity in msp:
        dxftype = entity.dxftype()
        if dxftype not in ('LWPOLYLINE', 'POLYLINE'):
            continue
        src_layer = entity.dxf.get('layer', '0')
        if src_layer not in LAYER_CONFIG:
            continue
        try:
            if dxftype == 'LWPOLYLINE':
                pts = [(p[0], p[1]) for p in entity.get_points()]
                closed = bool(entity.closed)
            else:
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                closed = bool(entity.is_closed)
            if len(pts) >= 3:
                hatch_layer, rgb = LAYER_CONFIG[src_layer]
                pending.append((hatch_layer, pts, closed, rgb))
        except Exception:
            pass

    count = 0
    for hatch_layer, pts, closed, rgb in pending:
        try:
            hatch = msp.add_hatch(dxfattribs={'layer': hatch_layer})
            hatch.set_solid_fill()
            hatch.paths.add_polyline_path(pts, is_closed=True)
            if force_color:
                hatch.rgb = rgb
            count += 1
        except Exception:
            pass
    return count


def _copy_resources(src_doc, merged_doc, merged_msp):
    """
    Copie layers, linetypes, styles, blocs et entités depuis src_doc vers merged_doc.
    La copie des blocs est nécessaire pour que les INSERT soient valides (sinon ArchiCAD
    signale une corruption car les blocs référencés sont introuvables).
    """
    # Layers
    for layer in src_doc.layers:
        name = layer.dxf.name
        if name not in merged_doc.layers:
            nl = merged_doc.layers.new(name)
            if layer.dxf.hasattr('color'):
                nl.dxf.color = layer.dxf.color

    # APPIDs — indispensable pour que les XDATA des entités PCI soient lisibles
    # (QUPL, COPL, EOR, ICL, IDU, etc.) ; leur absence cause "Ne peut lire élément lié"
    for appid in src_doc.appids:
        name = appid.dxf.name
        if name not in merged_doc.appids:
            try:
                merged_doc.appids.new(name)
            except Exception:
                pass

    # Linetypes
    for lt in src_doc.linetypes:
        if lt.dxf.name not in merged_doc.linetypes:
            try:
                merged_doc.linetypes.new(lt.dxf.name)
            except Exception:
                pass

    # Text styles
    for style in src_doc.styles:
        if style.dxf.name not in merged_doc.styles:
            try:
                merged_doc.styles.new(style.dxf.name)
            except Exception:
                pass

    # Block definitions (nécessaires pour les entités INSERT)
    for block in src_doc.blocks:
        if block.name.startswith('*') or block.name in merged_doc.blocks:
            continue
        try:
            new_block = merged_doc.blocks.new(block.name)
            for e in block:
                if e.dxftype() in ('ENDBLK', 'SEQEND'):
                    continue
                try:
                    new_block.add_entity(e.copy())
                except Exception:
                    pass
        except Exception:
            pass

    # Entités du modelspace
    count = 0
    for entity in src_doc.modelspace():
        try:
            merged_msp.add_entity(entity.copy())
            count += 1
        except Exception:
            pass
    return count



@app.route('/api/download', methods=['POST'])
def download_and_merge():
    """Télécharge les DXF des feuilles et les fusionne avec ezdxf."""
    data = request.get_json()
    if not data or 'feuilles' not in data:
        return jsonify({'error': 'Corps JSON requis avec clé "feuilles"'}), 400

    feuilles = data['feuilles']
    if not feuilles:
        return jsonify({'error': 'Aucune feuille fournie'}), 400

    projection = data.get('projection', 'l93')
    if projection not in DXF_BASES:
        projection = 'l93'

    merged = ezdxf.new(dxfversion='R2010')
    merged.header['$INSUNITS'] = 6   # mètres
    merged_msp = merged.modelspace()

    downloaded, errors = [], []
    # Accumuler les extents depuis les sources (avant copie)
    min_x = min_y = float('inf')
    max_x = max_y = float('-inf')

    for f in feuilles:
        commune_path = f.get('commune_path', '')
        com_abs      = f.get('com_abs', '000')
        section      = f.get('section', '')
        numero       = f.get('numero', '01')
        label        = f.get('label', f"{commune_path} {section}{numero}")

        dxf_bytes, err = _download_dxf(commune_path, com_abs, section, numero, projection)
        if dxf_bytes is None:
            errors.append(f'{label}: {err}')
            continue

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
                tmp.write(dxf_bytes)
                tmp_path = tmp.name
            src_doc = ezdxf.readfile(tmp_path)
        except Exception as e:
            errors.append(f'{label}: lecture DXF échouée — {e}')
            continue
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # Extents depuis le header source (coordonnées Lambert-93 valides)
        try:
            emin = src_doc.header['$EXTMIN']
            emax = src_doc.header['$EXTMAX']
            if abs(emin[0]) < 1e15:
                min_x = min(min_x, emin[0]); min_y = min(min_y, emin[1])
                max_x = max(max_x, emax[0]); max_y = max(max_y, emax[1])
        except Exception:
            pass

        _copy_resources(src_doc, merged, merged_msp)
        downloaded.append(label)

    if not downloaded:
        return jsonify({'error': 'Aucun DXF téléchargé', 'details': errors}), 502

    # ── Hachures bâtiments (3BATIDUR / 3BATILEGER) ──────────────────────────
    if data.get('add_hatches', False):
        force_color = data.get('hatch_force_color', True)
        n = _add_hatches(merged, merged_msp, force_color=force_color)
        if n:
            downloaded.append(f'Hachures ({n})')
        else:
            errors.append('Hachures : aucune polyligne 3BATIDUR/3BATILEGER trouvée')

    # ── Chaussée / trottoir (approximatif, BD TOPO + cadastre) ──────────────
    if data.get('generate_voirie', False):
        perimeter_geojson = data.get('perimeter_geojson')
        all_parcelles = data.get('all_parcelles_in_perimeter') or []
        if not _SHAPELY_OK or not _PYPROJ_OK:
            errors.append('Voirie : shapely/pyproj indisponible')
        elif not perimeter_geojson:
            errors.append('Voirie : périmètre manquant')
        else:
            try:
                n, voirie_bounds = _generate_voirie(merged, merged_msp, perimeter_geojson, all_parcelles)
                if n:
                    downloaded.append(f'Voirie approx. ({n} calques)')
                    if voirie_bounds:
                        vx0, vy0, vx1, vy1 = voirie_bounds
                        min_x = min(min_x, vx0); min_y = min(min_y, vy0)
                        max_x = max(max_x, vx1); max_y = max(max_y, vy1)
                else:
                    errors.append('Voirie : aucune route trouvée sur le périmètre')
            except Exception as e:
                errors.append(f'Voirie : erreur — {e}')

    # ── Hauteur des bâtiments (BD TOPO, jointure spatiale) ──────────────────
    if data.get('add_building_heights', False):
        perimeter_geojson = data.get('perimeter_geojson')
        if not _SHAPELY_OK or not _PYPROJ_OK:
            errors.append('Hauteur bâtiments : shapely/pyproj indisponible')
        elif not perimeter_geojson:
            errors.append('Hauteur bâtiments : périmètre manquant')
        else:
            try:
                n = _add_building_height_labels(merged, merged_msp, perimeter_geojson)
                if n:
                    downloaded.append(f'Hauteurs bâtiments ({n})')
                else:
                    errors.append('Hauteur bâtiments : aucune correspondance trouvée avec la BD TOPO')
            except Exception as e:
                errors.append(f'Hauteur bâtiments : erreur — {e}')

    # ── Ancrage Archicad — point choisi par l'utilisateur sur la carte ──────
    anchor_x = anchor_y = None
    anchor_point = data.get('anchor_point')
    if anchor_point:
        converted = _point_wgs84_to_l93(anchor_point['lon'], anchor_point['lat'])
        if converted:
            anchor_x, anchor_y = converted
            _add_anchor_marker(merged, merged_msp, anchor_x, anchor_y)
        else:
            errors.append("Point d'ancrage non calculé (pyproj indisponible)")

    slug = re.sub(r'[^A-Za-z0-9]', '_', '_'.join(downloaded[:3]))
    dxf_filename = f'cadastre_{slug}.dxf'

    # Orthophoto optionnelle — non disponible en CC (coordonnées L93 requises pour le positionnement WMS)
    include_ortho = data.get('include_ortho', False) and projection == 'l93'
    ortho_added = False
    ortho_bytes = jpeg_filename = None

    if include_ortho:
        # Emprise WMS : tracé utilisateur (converti L93 via pyproj si dispo), sinon bbox des feuilles
        shape_bbox = data.get('shape_bbox')
        ox_min = None
        if shape_bbox:
            converted = _bbox_wgs84_to_l93(
                shape_bbox['lon_min'], shape_bbox['lat_min'],
                shape_bbox['lon_max'], shape_bbox['lat_max'],
            )
            if converted:
                ox_min, oy_min, ox_max, oy_max = converted
            elif min_x != float('inf'):
                ox_min, oy_min, ox_max, oy_max = min_x, min_y, max_x, max_y
        elif min_x != float('inf'):
            ox_min, oy_min, ox_max, oy_max = min_x, min_y, max_x, max_y

        if ox_min is not None:
            ortho_bytes, ortho_err, ortho_w, ortho_h = _fetch_ortho_wms(
                ox_min, oy_min, ox_max, oy_max
            )
            if ortho_bytes:
                jpeg_filename = f'cadastre_{slug}.jpg'
                _add_image_entity(
                    merged, merged_msp, jpeg_filename,
                    ox_min, oy_min, ox_max, oy_max, ortho_w, ortho_h
                )
                ortho_added = True
            else:
                errors.append(f'Orthophoto IGN non disponible : {ortho_err}')

    # Écriture DXF via saveas() (chemin éprouvé) sur fichier temporaire → lecture bytes → suppression
    with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
        tmp_path = tmp.name
    merged.saveas(tmp_path)

    # ezdxf réinitialise $EXTMIN/$EXTMAX pendant saveas() — patch texte
    with open(tmp_path, 'r', encoding='latin-1') as f:
        dxf_content = f.read()
    os.unlink(tmp_path)

    if min_x != float('inf'):
        dxf_content = _patch_extents(dxf_content, min_x, min_y, max_x, max_y)

    dxf_bytes = dxf_content.encode('latin-1')

    # Construction du livrable final (DXF seul ou ZIP avec orthophoto)
    if ortho_added:
        return_filename = f'cadastre_{slug}.zip'
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(dxf_filename, dxf_bytes)
            zf.writestr(jpeg_filename, ortho_bytes)
        file_bytes = zip_buf.getvalue()
        mime = 'application/zip'
    else:
        return_filename = dxf_filename
        file_bytes = dxf_bytes
        mime = 'application/dxf'

    token = str(uuid.uuid4())
    _pending[token] = (file_bytes, return_filename, mime)

    return jsonify({
        'token':          token,
        'filename':       return_filename,
        'downloaded':     downloaded,
        'errors':         errors,
        'has_ortho':      ortho_added,
        'anchor_l93':     {'x': anchor_x, 'y': anchor_y} if anchor_x is not None else None,
    })


@app.route('/api/result/<token>')
def get_result(token):
    """Sert le DXF ou ZIP depuis la mémoire (usage unique)."""
    entry = _pending.pop(token, None)
    if not entry:
        return jsonify({'error': 'Fichier non trouvé ou déjà téléchargé'}), 404
    file_bytes, filename, mime = entry
    return send_file(
        io.BytesIO(file_bytes),
        mimetype=mime,
        as_attachment=True,
        download_name=filename,
    )


if __name__ == '__main__':
    print('Cadastre Tool — http://localhost:5057')
    # threaded=True : permet de traiter la requête d'annulation pendant qu'une
    # génération Archicad (longue, plusieurs dizaines de secondes) est en cours.
    app.run(host='127.0.0.1', port=5057, debug=False, threaded=True)
