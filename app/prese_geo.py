"""
Geometria del tab Prese: confini dei distretti e dei comuni ISTAT, punto dentro/fuori, distanze,
distretto proposto dalla posizione. Staccato da app/prese.py il 27/09/2026 (solo spostato, stesso codice).
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import unicodedata

import numpy as np

from app import motore_calcolo


# Fuori da ogni confine si propone il distretto piu' vicino solo entro questa
# distanza: oltre, la proposta sarebbe un tiro a indovinare. Ridotta da 300 a
# 100 m (Daniele, 30/09/2026: Casa Bernini a Broni, un DP a 117 m dal confine
# in una frazione tutta NO DISTRETTO riceveva la proposta DBRN05).
DISTANZA_MAX_PROPOSTA_M = 100

# Controllo "Diverso dalla posizione": una presa fuori da ogni confine si segnala
# solo se sta a piu' di tanti metri dal confine del proprio distretto. Resta a
# 300 m: la riduzione a 100 m riguarda solo le proposte (30/09/2026).
DISTANZA_MAX_POSIZIONE_M = 300

# Controllo "Diverso dalla posizione": una presa a meno di questi metri dal
# confine del proprio distretto non si segnala, lo scarto puo' essere solo
# l'errore della coordinata (Daniele, 25/09/2026).
TOLLERANZA_BORDO_M = 30

_CACHE_CONFINI: dict = {"versione": None, "poligoni": []}

def _poligoni():
    """[(codice, [anelli come array Nx2 lon/lat], bbox)] dal GeoJSON dei
    confini, riletto solo se il file cambia."""
    percorso = motore_calcolo.PERCORSO_CONFINI_DISTRETTI
    if not percorso.exists():
        return []
    st = percorso.stat()
    versione = (st.st_mtime_ns, st.st_size)
    if _CACHE_CONFINI["versione"] != versione:
        dati = json.loads(percorso.read_text(encoding="utf-8"))
        poligoni = []
        for f in dati.get("features", []):
            codice = (f.get("properties") or {}).get("codice_distretto", "")
            g = f.get("geometry") or {}
            parti = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []
            for parte in parti:
                anelli = [np.asarray(a, dtype=float)[:, :2] for a in parte if len(a) >= 3]
                if anelli:
                    esterno = anelli[0]
                    bbox = (esterno[:, 0].min(), esterno[:, 1].min(), esterno[:, 0].max(), esterno[:, 1].max())
                    poligoni.append((codice, anelli, bbox))
        _CACHE_CONFINI.update(versione=versione, poligoni=poligoni)
    return _CACHE_CONFINI["poligoni"]

def _dentro_anello(x: np.ndarray, y: np.ndarray, anello: np.ndarray) -> np.ndarray:
    """Ray casting vettoriale: quali punti (x, y) cadono dentro l'anello."""
    x1, y1 = anello[:, 0][None, :], anello[:, 1][None, :]
    x2, y2 = np.roll(anello[:, 0], -1)[None, :], np.roll(anello[:, 1], -1)[None, :]
    dy = np.where(y2 == y1, 1e-300, y2 - y1)
    dentro = np.zeros(len(x), dtype=bool)
    for i in range(0, len(x), 500):  # a blocchi: matrice punti x lati
        px, py = x[i:i + 500, None], y[i:i + 500, None]
        attraversa = (y1 > py) != (y2 > py)
        xi = x1 + (py - y1) * (x2 - x1) / dy
        dentro[i:i + 500] = (np.count_nonzero(attraversa & (px < xi), axis=1) % 2) == 1
    return dentro

def _distanza_anello_m(x: np.ndarray, y: np.ndarray, anello: np.ndarray, lat0: float) -> np.ndarray:
    """Distanza minima (metri, proiezione locale) di ogni punto dal bordo."""
    kx, ky = 111_320 * math.cos(math.radians(lat0)), 110_540
    px, py = x[:, None] * kx, y[:, None] * ky
    ax, ay = anello[:-1, 0][None, :] * kx, anello[:-1, 1][None, :] * ky
    bx, by = anello[1:, 0][None, :] * kx, anello[1:, 1][None, :] * ky
    dx, dy = bx - ax, by - ay
    lung2 = dx * dx + dy * dy
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / np.where(lung2 == 0, 1, lung2), 0, 1)
    return np.sqrt((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2).min(axis=1)

def _dentro_confini(lat: np.ndarray, lon: np.ndarray, trovato: np.ndarray | None = None) -> list[str]:
    """Per ogni punto il codice del distretto il cui confine lo contiene
    ('' se nessuno o punto non valido). trovato, se passato, viene
    aggiornato: True dove il punto cade dentro un confine."""
    n = len(lat)
    codici = [""] * n
    if trovato is None:
        trovato = np.zeros(n, dtype=bool)
    validi = ~(np.isnan(lat) | np.isnan(lon))
    for codice, anelli, (x0, y0, x1, y1) in _poligoni():
        candidati = validi & ~trovato & (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1)
        idx = np.nonzero(candidati)[0]
        if not len(idx):
            continue
        dentro = _dentro_anello(lon[idx], lat[idx], anelli[0])
        for buco in anelli[1:]:
            dentro &= ~_dentro_anello(lon[idx], lat[idx], buco)
        for i in idx[dentro]:
            codici[i] = codice
        trovato[idx[dentro]] = True
    return codici

# Confini dei comuni ISTAT (unita' amministrative a fini statistici al
# 01/01/2025, generalizzati), convertiti una volta in GeoJSON lon/lat per i
# comuni attorno alla provincia di Pavia (Daniele, 25/09/2026): dicono se la
# coordinata di una presa sta davvero nel suo comune.
PERCORSO_CONFINI_COMUNI = Path("project_docs/comuni_confini.geojson")

# I confini generalizzati hanno un errore di qualche decina di metri.
TOLLERANZA_CONFINE_COMUNE_M = 100

_CACHE_COMUNI: dict = {"versione": None, "poligoni": []}

def _nome_comune(nome: str) -> str:
    """'Gambolò' e "GAMBOLO'" -> 'GAMBOLO' (senza accenti, apostrofi, spazi)."""
    testo = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode()
    return "".join(c for c in testo.upper() if c.isalnum())

def _poligoni_comuni():
    """[(nome normalizzato, "Nome (SIGLA)", anelli, bbox)], riletti solo se
    il file cambia; [] se il file non c'e'."""
    if not PERCORSO_CONFINI_COMUNI.exists():
        return []
    st = PERCORSO_CONFINI_COMUNI.stat()
    versione = (st.st_mtime_ns, st.st_size)
    if _CACHE_COMUNI["versione"] != versione:
        dati = json.loads(PERCORSO_CONFINI_COMUNI.read_text(encoding="utf-8"))
        poligoni = []
        for f in dati.get("features", []):
            nome = f["properties"]["comune"]
            sigla = f["properties"].get("provincia", "")
            etichetta = f"{nome} ({sigla})" if sigla else nome
            for parte in f["geometry"]["coordinates"]:
                anelli = [np.asarray(a, dtype=float)[:, :2] for a in parte if len(a) >= 3]
                if anelli:
                    e = anelli[0]
                    poligoni.append((_nome_comune(nome), etichetta, anelli, (e[:, 0].min(), e[:, 1].min(), e[:, 0].max(), e[:, 1].max())))
        _CACHE_COMUNI.update(versione=versione, poligoni=poligoni)
    return _CACHE_COMUNI["poligoni"]

def _comune_della_posizione(lat: np.ndarray, lon: np.ndarray) -> list[str]:
    """"Nome (SIGLA)" del comune in cui cade ogni punto ('' se nessuno)."""
    esito = [""] * len(lat)
    libero = np.ones(len(lat), dtype=bool)
    for _, nome, anelli, (x0, y0, x1, y1) in _poligoni_comuni():
        idx = np.nonzero(libero & (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1))[0]
        if not len(idx):
            continue
        dentro = _dentro_anello(lon[idx], lat[idx], anelli[0])
        for buco in anelli[1:]:
            dentro &= ~_dentro_anello(lon[idx], lat[idx], buco)
        for i in idx[dentro]:
            esito[i] = nome
        libero[idx[dentro]] = False
    return esito

def _distanza_dal_comune_m(lat: np.ndarray, lon: np.ndarray, comune: str) -> np.ndarray | None:
    """Distanza di ogni punto dal confine del comune (None se il comune non
    e' nel file ISTAT)."""
    chiave = _nome_comune(comune)
    parti = [anelli for n, _, anelli, _ in _poligoni_comuni() if n == chiave]
    if not parti:
        return None
    distanza = np.full(len(lat), np.inf)
    lat0 = float(np.mean(lat)) if len(lat) else 45.0
    for anelli in parti:
        for anello in anelli:
            for i in range(0, len(lat), 500):
                distanza[i:i + 500] = np.minimum(
                    distanza[i:i + 500], _distanza_anello_m(lon[i:i + 500], lat[i:i + 500], anello, lat0))
    return distanza

def _dentro_e_distanza(lat: np.ndarray, lon: np.ndarray, codice: str) -> tuple[np.ndarray, np.ndarray]:
    """Per ogni punto: se cade dentro un confine del distretto codice, e la
    distanza in metri dal bordo piu' vicino di quel distretto (dentro o
    fuori). Distretto senza confine: (False, inf)."""
    n = len(lat)
    dentro = np.zeros(n, dtype=bool)
    distanza = np.full(n, np.inf)
    if n == 0:
        return dentro, distanza
    lat0 = float(np.mean(lat))
    for c, anelli, _ in _poligoni():
        if c != codice:
            continue
        d = _dentro_anello(lon, lat, anelli[0])
        for buco in anelli[1:]:
            d &= ~_dentro_anello(lon, lat, buco)
        dentro |= d
        for anello in anelli:
            for i in range(0, n, 500):  # a blocchi: matrice punti x lati
                distanza[i:i + 500] = np.minimum(
                    distanza[i:i + 500], _distanza_anello_m(lon[i:i + 500], lat[i:i + 500], anello, lat0))
    return dentro, distanza

def proponi_distretti(lat: np.ndarray, lon: np.ndarray) -> list[tuple[str, int | None]]:
    """Per ogni punto: (codice, None) se cade dentro un confine, (codice,
    distanza_m) se il piu' vicino e' entro DISTANZA_MAX_PROPOSTA_M, ("", None)
    altrimenti. I punti non validi (NaN) restano senza proposta."""
    n = len(lat)
    esito: list[tuple[str, int | None]] = [("", None)] * n
    if n == 0:
        return esito
    poligoni = _poligoni()
    validi = ~(np.isnan(lat) | np.isnan(lon))
    trovato = np.zeros(n, dtype=bool)
    for i, codice in enumerate(_dentro_confini(lat, lon, trovato)):
        if codice:
            esito[i] = (codice, None)

    fuori = np.nonzero(validi & ~trovato)[0]
    if len(fuori):
        lat0 = float(np.nanmean(lat[fuori]))
        margine_lat = DISTANZA_MAX_PROPOSTA_M / 110_540
        margine_lon = DISTANZA_MAX_PROPOSTA_M / (111_320 * math.cos(math.radians(lat0)))
        migliore = np.full(len(fuori), np.inf)
        codice_migliore = [""] * len(fuori)
        for codice, anelli, (x0, y0, x1, y1) in poligoni:
            vicini = (
                (lon[fuori] >= x0 - margine_lon) & (lon[fuori] <= x1 + margine_lon)
                & (lat[fuori] >= y0 - margine_lat) & (lat[fuori] <= y1 + margine_lat)
            )
            j = np.nonzero(vicini)[0]
            if not len(j):
                continue
            d = _distanza_anello_m(lon[fuori][j], lat[fuori][j], anelli[0], lat0)
            meglio = d < migliore[j]
            migliore[j[meglio]] = d[meglio]
            for k in j[meglio]:
                codice_migliore[k] = codice
        for k, i in enumerate(fuori):
            if migliore[k] <= DISTANZA_MAX_PROPOSTA_M:
                esito[i] = (codice_migliore[k], int(round(migliore[k])))
    return esito

def geojson_attorno(lat_min, lat_max, lon_min, lon_max, margine=0.01) -> dict:
    """Confini dei distretti che toccano il rettangolo delle prese (piu' un
    margine), con nome e comune dall'elenco distretti — per la mappa."""
    percorso = motore_calcolo.PERCORSO_CONFINI_DISTRETTI
    if not percorso.exists():
        return {"type": "FeatureCollection", "features": []}
    dati = json.loads(percorso.read_text(encoding="utf-8"))
    elenco = motore_calcolo.carica_mappa_distretti_df().set_index("codice_distretto")
    tenuti = []
    for f in dati.get("features", []):
        g = f.get("geometry") or {}
        coords = g.get("coordinates", [])
        punti = [p for anello in (coords if g.get("type") == "Polygon" else [a for parte in coords for a in parte]) for p in anello]
        if not punti:
            continue
        xs, ys = [p[0] for p in punti], [p[1] for p in punti]
        if max(xs) < lon_min - margine or min(xs) > lon_max + margine or max(ys) < lat_min - margine or min(ys) > lat_max + margine:
            continue
        codice = f["properties"].get("codice_distretto", "")
        f["properties"] = {
            "codice_distretto": codice,
            "nome_distretto": elenco["nome_distretto"].get(codice) or codice,
            "comune": elenco["comune_ufficiale"].get(codice, ""),
        }
        tenuti.append(f)
    return {"type": "FeatureCollection", "features": tenuti}

def _distretti_sicuri(lat: np.ndarray, lon: np.ndarray) -> list[str]:
    """Distretto in cui cade ogni punto, '' se fuori, NaN o a meno di
    TOLLERANZA_BORDO_M dal confine (li' il punto non decide)."""
    esito = [""] * len(lat)
    ok = np.nonzero(~(np.isnan(lat) | np.isnan(lon)))[0]
    if not len(ok):
        return esito
    dentro = np.array(_dentro_confini(lat[ok], lon[ok]), dtype=object)
    for codice in set(dentro) - {""}:
        j = np.nonzero(dentro == codice)[0]
        _, prof = _dentro_e_distanza(lat[ok][j], lon[ok][j], codice)
        for jj in j[prof > TOLLERANZA_BORDO_M]:
            esito[ok[jj]] = codice
    return esito
