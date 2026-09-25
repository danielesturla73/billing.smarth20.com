"""
Scarica l'indirizzario ANNCSU della Lombardia (Archivio Nazionale dei Numeri
Civici delle Strade Urbane, Agenzia delle Entrate e ISTAT, open data CC-BY
4.0, aggiornato ogni mese) e tiene solo i civici dei comuni in archivio, in
project_docs/anncsu_civici.csv (richiesto da Daniele il 25/09/2026).

Serve come fonte indipendente da Neta: la coordinata vera di un civico dice
in quale distretto cade e se la coordinata della presa e' sbagliata. Non
tutti i comuni hanno posizionato i civici (a settembre 2026 12 su 22 si',
Mortara solo il 3%): senza coordinate il civico serve solo a dire se
l'indirizzo esiste.

    docker exec -w /app -e PYTHONPATH=/app billing python3 scripts/aggiorna_anncsu.py

Fonte: ANNCSU - Agenzia delle Entrate e ISTAT, licenza CC-BY 4.0.
"""
from __future__ import annotations

import io
import json
import time
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

from app import database, prese

URL = "https://anncsu.open.agenziaentrate.gov.it/age-inspire/opendata/anncsu/getds.php?INDIR_LOMB"
PERCORSO = Path("project_docs/anncsu_civici.csv")
USER_AGENT = "Mozilla/5.0 (billing-analisi-consumi; stradario distretti)"


def main() -> None:
    with database.connessione() as conn:
        comuni = [r[0] for r in conn.execute("SELECT DISTINCT LOCALITA FROM anagrafica_servizi ORDER BY 1")]
    confini = json.loads(prese.PERCORSO_CONFINI_COMUNI.read_text(encoding="utf-8"))
    istat = {prese._nome_comune(f["properties"]["comune"]): f["properties"]["pro_com"] for f in confini["features"]}
    codici = {istat[prese._nome_comune(c)]: c for c in comuni if prese._nome_comune(c) in istat}

    richiesta = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(richiesta, timeout=900) as r:
        nome_file = r.headers.get("Content-Disposition", "").split("filename=")[-1].strip('"')
        contenuto = r.read()
    archivio = zipfile.ZipFile(io.BytesIO(contenuto))
    csv = archivio.open(archivio.namelist()[0])
    df = pd.read_csv(io.TextIOWrapper(csv, encoding="utf-8", errors="replace"), sep=";", dtype=str)
    df = df[df["CODICE_ISTAT"].isin(codici)]
    out = pd.DataFrame({
        "COMUNE": df["CODICE_ISTAT"].map(codici),
        "ODONIMO": df["ODONIMO"].str.strip(),
        "CIVICO": pd.to_numeric(df["CIVICO"], errors="coerce").astype("Int64"),
        "ESPONENTE": df["ESPONENTE"].fillna("").str.strip(),
        "LAT": pd.to_numeric(df["COORD_Y_COMUNE"].str.replace(",", "."), errors="coerce").round(7),
        "LON": pd.to_numeric(df["COORD_X_COMUNE"].str.replace(",", "."), errors="coerce").round(7),
        # 1 = rilievo sul campo < 5 m, 2 = rilievo >= 5 m, 3-5 = da cartografia/ortofoto
        # (vedi metadati ANNCSU).
        "METODO": df["METODO"].fillna(""),
    })
    out = out.sort_values(["COMUNE", "ODONIMO", "CIVICO", "ESPONENTE"])
    PERCORSO.write_text(
        f"# ANNCSU - Agenzia delle Entrate e ISTAT, licenza CC-BY 4.0; file {nome_file}, filtrato il {time.strftime('%Y-%m-%d')}\n"
        + out.to_csv(index=False),
        encoding="utf-8",
    )
    per_comune = out.groupby("COMUNE").agg(civici=("ODONIMO", "size"), con_coordinate=("LAT", "count"))
    print(per_comune.to_string())
    print(f"{len(out)} civici salvati in {PERCORSO}")


if __name__ == "__main__":
    main()
