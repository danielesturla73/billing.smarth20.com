"""
Vie di OpenStreetMap (tracciato con nome) come fonte indipendente da Neta
(richiesto da Daniele il 25/09/2026): dicono se la coordinata di una presa
sta davvero sulla sua via, senza fidarsi delle coordinate di Neta, da cui
Neta ha ricavato anche i distretti (vedi app/stradario.py).

Il file project_docs/vie_osm.geojson si scarica una tantum con
scripts/scarica_vie_osm.py. I nomi di Neta e di OSM sono scritti in modo
diverso (VIA F.LLI CAIROLI / Via Fratelli Cairoli, VIA XX SETTEMBRE / Via
Venti Settembre, VIA CRIMINALI / Via Gerolamo Criminali, VIALE TOGLIATTI /
Via Palmiro Togliatti): si confrontano le parole del nome, senza il tipo
(via, viale...) ne' iniziali e preposizioni. Dati (c) OpenStreetMap
contributors, ODbL.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

import numpy as np

PERCORSO_VIE_OSM = Path("project_docs/vie_osm.geojson")

TIPI = {"VIA", "VIALE", "V", "VLE", "PIAZZA", "PZA", "P", "PIAZZALE", "PLE", "CORSO", "C", "SO", "STRADA", "STR",
        "VICOLO", "VIC", "LARGO", "LGO", "CONTRADA", "PRIVATA", "PASSAGGIO", "BORGO", "LOCALITA", "LOC",
        "REGIONE", "REG", "TRAVERSA", "PROVINCIALE", "COMUNALE", "VICINALE", "STRADONE"}
PAROLE_VUOTE = {"DI", "DEL", "DELLA", "DELLE", "DEI", "DEGLI", "DE", "D", "DA", "E", "LA", "LE", "IL", "LO", "GLI", "L", "AL", "ALLA"}
ABBREVIAZIONI = {"FLLI": "FRATELLI", "LLI": "FRATELLI", "S": "SAN", "SS": "SANTI", "STA": "SANTA",
                 "MONS": "MONSIGNORE", "GEN": "GENERALE", "PROF": "PROFESSORE", "DOTT": "DOTTORE", "ING": "INGEGNERE",
                 "AVV": "AVVOCATO", "CAV": "CAVALIERE", "MAD": "MADONNA", "B": "BEATO", "ON": "ONOREVOLE", "PAPA": "PAPA"}
NUMERI = {"1": "PRIMO", "I": "PRIMO", "2": "DUE", "II": "DUE", "4": "QUATTRO", "IV": "QUATTRO", "8": "OTTO",
          "11": "UNDICI", "20": "VENTI", "XX": "VENTI", "24": "VENTIQUATTRO", "XXIV": "VENTIQUATTRO",
          "25": "VENTICINQUE", "XXV": "VENTICINQUE", "26": "VENTISEI", "XXVI": "VENTISEI", "3": "TRE", "III": "TRE",
          "5": "CINQUE", "V": "CINQUE", "10": "DIECI", "X": "DIECI", "12": "DODICI", "XII": "DODICI"}
# Non sono vie: una cascina o una frazione non ha un tracciato con cui
# confrontare la presa (CASCINA DOSSELLO non e' Via Dossello).
NON_VIE = {"CASCINA", "CASCINE", "FRAZIONE", "FRAZ", "CASE", "PODERE", "FONDO", "CA"}
MESI = {"GENNAIO", "FEBBRAIO", "MARZO", "APRILE", "MAGGIO", "GIUGNO", "LUGLIO", "AGOSTO", "SETTEMBRE",
        "OTTOBRE", "NOVEMBRE", "DICEMBRE"}


def parole(nome: str) -> tuple[str, frozenset[str]]:
    """(tipo, parole significative del nome). 'VIA F.LLI CAIROLI' ->
    ('VIA', {'FRATELLI', 'CAIROLI'}); le iniziali puntate spariscono, i
    numeri delle date diventano parole (XX SETTEMBRE -> VENTI SETTEMBRE)."""
    testo = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode().upper()
    testo = testo.replace("F.LLI", "FLLI ").replace("P.LE", "PIAZZALE ").replace("V.LE", "VIALE ").replace("C.SO", "CORSO ")
    grezze = [p for p in re.split(r"[^A-Z0-9]+", testo) if p]
    if grezze and grezze[0] in NON_VIE:
        return grezze[0], frozenset()
    tipo = ""
    while len(grezze) > 1 and grezze[0] in TIPI:
        tipo = tipo or grezze[0]
        grezze = grezze[1:]
    out = []
    for k, p in enumerate(grezze):
        seguente = grezze[k + 1] if k + 1 < len(grezze) else ""
        if seguente in MESI and p in NUMERI:
            out.append(NUMERI[p])
            continue
        p = ABBREVIAZIONI.get(p, p)
        if p in PAROLE_VUOTE or p in TIPI:
            continue
        if len(p) == 1 and p.isalpha():
            continue  # iniziale (A. SPINELLI, G. VERDI)
        out.append(p)
    return tipo, frozenset(out)


_CACHE: dict = {"versione": None, "vie": {}}


def vie_comune(comune: str) -> dict[str, list[np.ndarray]]:
    """{nome OSM: [tratti come array Nx2 lon/lat]} del comune ({} se il file
    non c'e' o il comune non e' stato scaricato)."""
    if not PERCORSO_VIE_OSM.exists():
        return {}
    st = PERCORSO_VIE_OSM.stat()
    versione = (st.st_mtime_ns, st.st_size)
    if _CACHE["versione"] != versione:
        vie: dict[str, dict[str, list[np.ndarray]]] = {}
        for f in json.loads(PERCORSO_VIE_OSM.read_text(encoding="utf-8"))["features"]:
            pr = f["properties"]
            vie.setdefault(pr["comune"], {}).setdefault(pr["nome"], []).append(np.asarray(f["geometry"]["coordinates"], dtype=float))
        _CACHE.update(versione=versione, vie=vie)
    return _CACHE["vie"].get(comune.strip().upper(), {})


def _simili(a: str, b: str) -> bool:
    """Stessa parola, o una lettera di differenza per parole lunghe (refusi
    in OSM: 'Gugliemo Marconi')."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    corta, lunga = sorted((a, b), key=len)
    return any(lunga[:i] + lunga[i + 1:] == corta for i in range(len(lunga)))


def _contenute(pv: frozenset[str], po: frozenset[str]) -> bool:
    """Ogni parola di pv ha una parola simile in po."""
    return all(any(_simili(a, b) for b in po) for a in pv)


_CACHE_ABBINA: dict = {}


def abbina(vie_neta: list[str], nomi_osm: list[str]) -> dict[str, str]:
    """Come _abbina, in memoria per le stesse liste (serve piu' volte per
    comune: ANNCSU, OSM, stradario)."""
    chiave = (tuple(vie_neta), tuple(nomi_osm))
    if chiave not in _CACHE_ABBINA:
        if len(_CACHE_ABBINA) > 200:
            _CACHE_ABBINA.clear()
        _CACHE_ABBINA[chiave] = _abbina(vie_neta, nomi_osm)
    return dict(_CACHE_ABBINA[chiave])


def _abbina(vie_neta: list[str], nomi_osm: list[str]) -> dict[str, str]:
    """{via Neta: nome OSM} per le vie abbinabili senza ambiguita'. Le parole
    del nome Neta devono stare tutte in quello OSM (VIA CRIMINALI ->
    Via Gerolamo Criminali); tra piu' candidati vince quello con meno parole
    in piu', a parita' quello dello stesso tipo; se resta un pareggio la
    via non si abbina."""
    osm = [(n, *parole(n)) for n in nomi_osm]
    esito = {}
    for via in vie_neta:
        tipo, pv = parole(via)
        if not pv:
            continue
        candidati = []
        for nome, t_osm, po in osm:
            if _contenute(pv, po) or (po and _contenute(po, pv) and len(pv) - len(po) <= 1):
                extra = len(po) + len(pv) - 2 * sum(any(_simili(a, b) for b in po) for a in pv)
                candidati.append((extra, 0 if t_osm == tipo else 1, nome))
        if not candidati:
            continue
        candidati.sort()
        if len(candidati) > 1 and candidati[0][:2] == candidati[1][:2]:
            continue
        esito[via] = candidati[0][2]
    return esito


def distanza_m(lat: np.ndarray, lon: np.ndarray, tratti: list[np.ndarray]) -> np.ndarray:
    """Distanza minima (metri) di ogni punto dai tratti di una via."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    lat0 = float(np.mean(lat)) if len(lat) else 45.0
    kx, ky = 111_320 * math.cos(math.radians(lat0)), 110_540
    px, py = lon[:, None] * kx, lat[:, None] * ky
    migliore = np.full(len(lat), np.inf)
    for t in tratti:
        ax, ay = t[:-1, 0][None, :] * kx, t[:-1, 1][None, :] * ky
        bx, by = t[1:, 0][None, :] * kx, t[1:, 1][None, :] * ky
        dx, dy = bx - ax, by - ay
        l2 = np.where(dx * dx + dy * dy == 0, 1, dx * dx + dy * dy)
        s = np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0, 1)
        migliore = np.minimum(migliore, np.sqrt((px - ax - s * dx) ** 2 + (py - ay - s * dy) ** 2).min(axis=1))
    return migliore
