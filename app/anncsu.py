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


def civici_per_indirizzo(comune: str, indirizzi) -> pd.DataFrame:
    """Per ogni indirizzo Neta: VIA_ANNCSU (odonimo abbinato, '' se la via
    non c'e'), CIVICO_ESISTE (True/False, None se via o civico mancano),
    CIV_LAT/CIV_LON (posizione del civico, NaN se non c'e')."""
    indirizzi = list(indirizzi)
    d = _dati()
    d = d[d["COMUNE"] == comune.strip().upper()]
    esito = pd.DataFrame({"VIA_ANNCSU": [""] * len(indirizzi), "CIVICO_ESISTE": [None] * len(indirizzi),
                          "CIV_LAT": [float("nan")] * len(indirizzi), "CIV_LON": [float("nan")] * len(indirizzi)})
    if d.empty:
        return esito
    nv = [stradario.normalizza_indirizzo(i) for i in indirizzi]
    abbinate = vie_osm.abbina(sorted({v for v, _ in nv} - {""}), sorted(set(d["ODONIMO"])))
    civici = set(zip(d["ODONIMO"], d["CIVICO"].dropna().astype(int)))
    pos = d.dropna(subset=["CIVICO", "LAT"]).assign(CIVICO=lambda x: x["CIVICO"].astype(int)) \
        .groupby(["ODONIMO", "CIVICO"])[["LAT", "LON"]].median()
    posizioni = dict(zip(pos.index, zip(pos["LAT"], pos["LON"])))
    via_a, esiste, lat, lon = [], [], [], []
    for via, civico in nv:
        odonimo = abbinate.get(via, "")
        via_a.append(odonimo)
        if not odonimo or civico is None:
            esiste.append(None)
            lat.append(float("nan"))
            lon.append(float("nan"))
            continue
        esiste.append((odonimo, civico) in civici)
        la, lo = posizioni.get((odonimo, civico), (float("nan"), float("nan")))
        lat.append(la)
        lon.append(lo)
    esito = pd.DataFrame({"VIA_ANNCSU": via_a, "CIVICO_ESISTE": esiste, "CIV_LAT": lat, "CIV_LON": lon})
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
    return d.assign(CIVICO=d["CIVICO"].astype(int)).groupby(["ODONIMO", "CIVICO"], as_index=False)[["LAT", "LON"]].median()
