"""
Controllo dei numeri prima/dopo una modifica al codice (Daniele,
26/09/2026): non e' una suite di test, e' una "fotografia" di cio' che
l'app calcola oggi, da confrontare dopo ogni modifica per accorgersi subito
se un numero e' cambiato senza volerlo.

Nella fotografia, per ogni comune:
- Import_WMS (volume per mese e distretto) e prospetto dei volumi spostati;
- tab Prese: per ogni presa motivo, distretto dall'indirizzo, proposta,
  fonti; conteggi del riepilogo;
- file coordinate: problema, coordinata proposta e affidabilita' per presa.

    # prima della modifica
    docker exec -w /app -e PYTHONPATH=/app billing python3 scripts/controllo_numeri.py fotografa
    # dopo la modifica (e il deploy)
    docker exec -w /app -e PYTHONPATH=/app billing python3 scripts/controllo_numeri.py confronta

La fotografia contiene dati dei clienti: sta in archivio/controlli/ (fuori
da git). Il calcolo si rifa' da zero, senza cache, per provare il codice.
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

import pandas as pd

from app import consolidamento, database, motore_calcolo, prese

CARTELLA = Path("archivio") / "controlli"
FOTO = CARTELLA / "fotografia.pkl"
TOLLERANZA_M3 = 0.005


def _import_wms(comune: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    with database.connessione() as conn:
        df = database.carica_letture(conn, comune)
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
    r = motore_calcolo.elabora_dataframe(df, prese.riassegnazioni_calcolo(comune))
    v = consolidamento.applica(r.volumi_distretto_mese, comune)
    v = v.assign(Mese=v["Mese"].astype(str))[["Mese", "Codice Distretto", "Volume Fatturato (m3)", "Note"]]
    return v.sort_values(["Mese", "Codice Distretto"]).reset_index(drop=True), r.volumi_riassegnati


def _prese(comune: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    p = prese.prese_da_assegnare(comune)
    colonne = ["CHIAVE", "MOTIVO", "DISTRETTO", "DISTRETTO_VIA", "PROPOSTA", "PROPOSTA_DA", "FONTI", "CONCORDI",
               "CONFERMATO", "VALIDATA"]
    p = p[[c for c in colonne if c in p.columns]].sort_values("CHIAVE").reset_index(drop=True) if not p.empty else p
    c = prese._coordinate_da_verificare(comune)
    colonne_c = ["CHIAVE", "PROBLEMA", "LAT_CORRETTA", "LON_CORRETTA", "AFFIDABILITA"]
    c = c[[x for x in colonne_c if x in c.columns]].sort_values(["CHIAVE", "PROBLEMA"]).reset_index(drop=True) if not c.empty else c
    return p, c


def fotografa() -> dict:
    with database.connessione() as conn:
        comuni = database.elenco_comuni(conn)
    foto = {}
    for comune in comuni:
        t = time.time()
        wms, spostati = _import_wms(comune)
        p, c = _prese(comune)
        foto[comune] = {"wms": wms, "spostati": spostati, "prese": p, "coordinate": c}
        print(f"{comune:25s} {len(wms):4d} righe WMS, {len(p):5d} prese, {len(c):5d} coordinate  {time.time() - t:.0f}s", flush=True)
    return foto


def _confronta_tabella(nome: str, a: pd.DataFrame, b: pd.DataFrame) -> list[str]:
    if a.empty and b.empty:
        return []
    if list(a.columns) != list(b.columns) or len(a) != len(b):
        return [f"{nome}: righe {len(a)} -> {len(b)}, colonne {list(a.columns) == list(b.columns)}"]
    diversi = []
    for col in a.columns:
        x, y = a[col], b[col]
        if pd.api.types.is_bool_dtype(x) or pd.api.types.is_bool_dtype(y):
            if not (x.astype(str) == y.astype(str)).all():
                diversi.append(f"{nome}.{col}: {int((x.astype(str) != y.astype(str)).sum())} valori diversi")
        elif pd.api.types.is_numeric_dtype(x) and pd.api.types.is_numeric_dtype(y):
            d = (x.fillna(0) - y.fillna(0)).abs()
            if (d > TOLLERANZA_M3).any() or (x.isna() != y.isna()).any():
                diversi.append(f"{nome}.{col}: {int((d > TOLLERANZA_M3).sum())} valori diversi, massimo {d.max():.3f}")
        elif not (x.astype(str) == y.astype(str)).all():
            n = int((x.astype(str) != y.astype(str)).sum())
            esempio = (x.astype(str) != y.astype(str)).idxmax()
            diversi.append(f"{nome}.{col}: {n} valori diversi (es. riga {esempio}: {x.iloc[esempio]!r} -> {y.iloc[esempio]!r})")
    return diversi


def main(azione: str) -> int:
    CARTELLA.mkdir(parents=True, exist_ok=True)
    if azione == "fotografa":
        foto = fotografa()
        FOTO.write_bytes(pickle.dumps(foto))
        print(f"Fotografia salvata: {FOTO} ({len(foto)} comuni)")
        return 0
    if azione == "confronta":
        vecchia = pickle.loads(FOTO.read_bytes())
        nuova = fotografa()
        problemi = []
        for comune in sorted(set(vecchia) | set(nuova)):
            if comune not in vecchia or comune not in nuova:
                problemi.append(f"{comune}: presente solo in una delle due")
                continue
            for parte in ("wms", "spostati", "prese", "coordinate"):
                problemi += [f"{comune} {x}" for x in _confronta_tabella(parte, vecchia[comune][parte], nuova[comune][parte])]
        if problemi:
            print("DIFFERENZE:")
            print("\n".join(problemi))
            return 1
        print("Nessuna differenza: tutti i numeri sono identici alla fotografia.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
