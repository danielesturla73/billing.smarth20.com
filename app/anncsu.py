"""
Civici ANNCSU (Archivio Nazionale dei Numeri Civici delle Strade Urbane,
Agenzia delle Entrate e ISTAT, CC-BY 4.0): la fonte piu' affidabile per la
posizione di un indirizzo (Daniele, 25/09/2026). Il file
project_docs/anncsu_civici.csv si aggiorna con scripts/aggiorna_anncsu.py.

Le vie ANNCSU si abbinano a quelle Neta come quelle di OpenStreetMap (vedi
vie_osm.abbina). Un civico con piu' esponenti (12, 12/A...) ha come
posizione la mediana delle sue coordinate. Non tutti i comuni hanno le
coordinate dei civici: senza, il civico dice solo se l'indirizzo esiste.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from app import stradario, vie_osm

PERCORSO_ANNCSU = Path("project_docs/anncsu_civici.csv")
_CACHE: dict = {"versione": None, "dati": None}


def _dati() -> pd.DataFrame:
    if not PERCORSO_ANNCSU.exists():
        return pd.DataFrame(columns=["COMUNE", "ODONIMO", "CIVICO", "ESPONENTE", "LAT", "LON", "METODO"])
    st = PERCORSO_ANNCSU.stat()
    versione = (st.st_mtime_ns, st.st_size)
    if _CACHE["versione"] != versione:
        df = pd.read_csv(PERCORSO_ANNCSU, comment="#", dtype={"COMUNE": str, "ODONIMO": str, "ESPONENTE": str, "METODO": str},
                         keep_default_na=False, na_values={"CIVICO": [""], "LAT": [""], "LON": [""]})
        df["CIVICO"] = pd.to_numeric(df["CIVICO"], errors="coerce")
        _CACHE.update(versione=versione, dati=df)
    return _CACHE["dati"]


def ha_coordinate(comune: str) -> bool:
    """Il comune ha posizionato i civici (almeno meta' con coordinate)."""
    d = _dati()
    d = d[d["COMUNE"] == comune.strip().upper()]
    return len(d) > 0 and d["LAT"].notna().mean() >= 0.5


# Un civico con piu' posizioni che distano piu' di questo e' ambiguo (in ANNCSU una via con lo stesso nome puo'
# stare in piu' frazioni: "Via Colombara 1" e "Via Colombara 1/A" a Gambolo' stanno a 6 km): non si usa.
SOGLIA_CIVICO_AMBIGUO_M = 150


def _posizioni_civici(con: pd.DataFrame) -> pd.DataFrame:
    """Una riga per (ODONIMO, CIVICO) con LAT, LON, METODO. Se esiste la riga senza esponente si usa solo quella
    (l'"1", non l'"1/A": Neta non scrive l'esponente); altrimenti tutte. Se le posizioni scelte distano piu' di
    SOGLIA_CIVICO_AMBIGUO_M la posizione e' NaN, non la loro mediana (Daniele, 30/09/2026, Gambolo' Via
    Colombara 1: la mediana finiva a 2,4 km dalla casa, in mezzo al nulla)."""
    con = con.copy()
    base = con["ESPONENTE"].fillna("").astype(str).str.strip() == ""
    ha_base = base.groupby([con["ODONIMO"], con["CIVICO"]]).transform("any")
    con = con[~ha_base | base]
    pos = con.groupby(["ODONIMO", "CIVICO"]).agg(
        LAT=("LAT", "median"), LON=("LON", "median"), METODO=("METODO", "max"),
        DLAT=("LAT", lambda x: x.max() - x.min()), DLON=("LON", lambda x: x.max() - x.min()))
    dist = np.hypot(pos["DLAT"] * 110_540, pos["DLON"] * 111_320 * np.cos(np.radians(pos["LAT"])))
    pos.loc[dist > SOGLIA_CIVICO_AMBIGUO_M, ["LAT", "LON"]] = np.nan
    return pos[["LAT", "LON", "METODO"]]


_CACHE_VICINI: dict = {}
FINESTRA_VICINI = 10        # civici con numero entro +-10 dallo stesso odonimo
MIN_VICINI = 4
SOGLIA_MINIMA_ISOLATO_M = 500


def _metri(la1, lo1, la2, lo2):
    return np.hypot((np.asarray(la1) - la2) * 110_540, (np.asarray(lo1) - lo2) * 111_320 * np.cos(np.radians(45.2)))


def vicini_civici(comune: str) -> dict[tuple[str, int], tuple[float, float, float]]:
    """{(odonimo, civico): (lat, lon, soglia_m)}: la posizione mediana dei civici con numero vicino (+-FINESTRA_VICINI) della
    stessa via, e di quanto un civico puo' stare lontano da essa (max(SOGLIA_MINIMA_ISOLATO_M, 4 volte la dispersione dei
    vicini)). Solo per i civici con almeno MIN_VICINI vicini. Serve a riconoscere un civico ANNCSU isolato, posizionato a
    chilometri dal resto della sua via (Giussago Via Fratelli Cairoli 43, a 3,8 km). Da solo non basta a scartarlo: vedi
    prese._prese_comune, dove serve anche che il DP stia con i vicini."""
    st = PERCORSO_ANNCSU.stat() if PERCORSO_ANNCSU.exists() else None
    chiave = (comune.strip().upper(), st.st_mtime_ns if st else 0)
    if chiave in _CACHE_VICINI:
        return _CACHE_VICINI[chiave]
    d = _dati()
    d = d[(d["COMUNE"] == comune.strip().upper()) & d["LAT"].notna() & d["CIVICO"].notna()]
    esito: dict[tuple[str, int], tuple[float, float, float]] = {}
    if not d.empty:
        pos = _posizioni_civici(d.assign(CIVICO=d["CIVICO"].astype(int))).dropna(subset=["LAT"])
        for odonimo, g in pos.groupby(level=0):
            if len(g) < MIN_VICINI + 1:
                continue
            civ = g.index.get_level_values(1).to_numpy()
            la, lo = g["LAT"].to_numpy(), g["LON"].to_numpy()
            for i in range(len(g)):
                v = np.nonzero((np.abs(civ - civ[i]) <= FINESTRA_VICINI) & (np.arange(len(g)) != i))[0]
                if len(v) < MIN_VICINI:
                    continue
                cla, clo = float(np.median(la[v])), float(np.median(lo[v]))
                dispersione = float(np.median(_metri(la[v], lo[v], cla, clo)))
                esito[(odonimo, int(civ[i]))] = (cla, clo, max(float(SOGLIA_MINIMA_ISOLATO_M), 4 * dispersione))
    _CACHE_VICINI.clear()
    _CACHE_VICINI[chiave] = esito
    return esito


def civici_per_indirizzo(comune: str, indirizzi) -> pd.DataFrame:
    """Per ogni indirizzo Neta: VIA_ANNCSU (odonimo abbinato, '' se la via
    non c'e'), CIVICO_ESISTE (True/False, None se via o civico mancano),
    CIV_LAT/CIV_LON (posizione del civico, NaN se non c'e')."""
    indirizzi = list(indirizzi)
    d = _dati()
    d = d[d["COMUNE"] == comune.strip().upper()]
    esito = pd.DataFrame({"VIA_ANNCSU": [""] * len(indirizzi), "CIVICO_ESISTE": [None] * len(indirizzi),
                          "CIV_LAT": [float("nan")] * len(indirizzi), "CIV_LON": [float("nan")] * len(indirizzi),
                          "CIV_METODO": [""] * len(indirizzi)})
    if d.empty:
        return esito
    nv = [stradario.normalizza_indirizzo(i) for i in indirizzi]
    abbinate = vie_osm.abbina(sorted({v for v, _ in nv} - {""}), sorted(set(d["ODONIMO"])))
    civici = set(zip(d["ODONIMO"], d["CIVICO"].dropna().astype(int)))
    con = d.dropna(subset=["CIVICO", "LAT"]).assign(CIVICO=lambda x: x["CIVICO"].astype(int))
    pos = _posizioni_civici(con)
    posizioni = dict(zip(pos.index, zip(pos["LAT"], pos["LON"], pos["METODO"])))
    via_a, esiste, lat, lon, metodo = [], [], [], [], []
    for via, civico in nv:
        odonimo = abbinate.get(via, "")
        via_a.append(odonimo)
        if not odonimo or civico is None:
            esiste.append(None)
            lat.append(float("nan"))
            lon.append(float("nan"))
            metodo.append("")
            continue
        esiste.append((odonimo, civico) in civici)
        la, lo, me = posizioni.get((odonimo, civico), (float("nan"), float("nan"), ""))
        lat.append(la)
        lon.append(lo)
        metodo.append(me)
    # METODO (metadati ANNCSU): 1/2 rilievo sul campo (<5 m / >=5 m), 3/4 da
    # base dati territoriale (<5 m / >=5 m), 5 dal Portale per i Comuni.
    esito = pd.DataFrame({"VIA_ANNCSU": via_a, "CIVICO_ESISTE": esiste, "CIV_LAT": lat, "CIV_LON": lon, "CIV_METODO": metodo})
    return esito


def abbinamento_vie(comune: str, vie_neta: list[str]) -> dict[str, str]:
    """{via Neta: odonimo ANNCSU} per le vie abbinabili."""
    d = _dati()
    d = d[d["COMUNE"] == comune.strip().upper()]
    return vie_osm.abbina(vie_neta, sorted(set(d["ODONIMO"]))) if not d.empty else {}


def civici_con_coordinate(comune: str) -> pd.DataFrame:
    """Civici del comune con posizione (una riga per civico): ODONIMO,
    CIVICO, LAT, LON — per costruire lo stradario dai civici veri."""
    d = _dati()
    d = d[(d["COMUNE"] == comune.strip().upper()) & d["LAT"].notna() & d["CIVICO"].notna()]
    pos = _posizioni_civici(d.assign(CIVICO=d["CIVICO"].astype(int))).dropna(subset=["LAT"]).reset_index()
    return pos[["ODONIMO", "CIVICO", "LAT", "LON"]]
