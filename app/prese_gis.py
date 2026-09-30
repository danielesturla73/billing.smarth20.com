"""
Segnalazioni per il GIS (Daniele, 30/09/2026): punti di erogazione (DP) gia' CONFERMATI in un
distretto ma che stanno fuori dal confine di quel distretto, cioe' probabili confini da rivedere.
Non va a Neta: va a chi gestisce il GIS.

Solo i DP confermati (spostati a mano, dalla proposta, con una zona, o "mantieni attuale"): se il
distretto e' confermato giusto e il punto sta fuori dal suo poligono, il problema e' il confine, non
l'assegnazione di Neta. Niente soglia sulla distanza (si ordina per distanza). I DP NO DISTRETTO
(NODMA), in Neta o confermati, non entrano: essere fuori confine e' corretto.

Posizione usata: il civico ANNCSU (la fonte piu' autorevole); se il civico manca, la coordinata di Neta.
"""
from __future__ import annotations

import io
import json

import numpy as np
import pandas as pd

from app import database
from app.prese import (
    ORIGINI,
    _dentro_confini,
    _dentro_e_distanza,
    _poligoni,
    assegnazioni,
    prese_comune,
)

CATEGORIE = {
    "fuori": "Fuori dal confine del distretto",
    "senza_poligono": "Distretto senza confine disegnato",
}


def fuori_confine_confermati(comuni: list[str]) -> pd.DataFrame:
    """Una riga per DP confermato in un distretto con confine e posizione fuori dal suo poligono.
    Colonne: COMUNE, DISTRETTO, DP, INDIRIZZO, SERVIZI, ORIGINE, NOTA, CONFERMATO_DA, CONFERMATO_IL,
    FONTE_POSIZIONE, LAT, LON, DISTANZA_M, CADE_IN, LAT_NETA, LON_NETA, DISTANZA_NETA_M, CATEGORIA."""
    con_poligono = {c for c, _, _ in _poligoni()}
    righe = []
    for comune in comuni:
        p = prese_comune(comune)
        if p.empty:
            continue
        with database.connessione() as conn:
            a = assegnazioni(conn, comune)
        if a.empty:
            continue
        a = a[a["DISTRETTO"].astype(str).str.strip() != ""]
        a = a[~a["DISTRETTO"].str.upper().str.startswith("NO")]  # NODMA fuori confine: corretto
        m = p.merge(a[["DP", "DISTRETTO", "UTENTE", "QUANDO", "ORIGINE", "NOTA"]].rename(columns={
            "DP": "CHIAVE", "DISTRETTO": "DISTRETTO_CONF"}), on="CHIAVE", how="inner")
        for codice, g in m.groupby("DISTRETTO_CONF"):
            civico = g["CIV_LAT"].notna() & g["CIV_LON"].notna()
            usa_neta = ~civico & g["COORD_VALIDE"]
            lat = np.where(civico, g["CIV_LAT"], np.where(usa_neta, g["LAT"], np.nan)).astype(float)
            lon = np.where(civico, g["CIV_LON"], np.where(usa_neta, g["LON"], np.nan)).astype(float)
            ok = ~np.isnan(lat)
            if not ok.any():
                continue
            if codice not in con_poligono:
                categoria = np.full(len(g), "senza_poligono", dtype=object)
                dentro = np.zeros(len(g), dtype=bool)
                dist = np.full(len(g), np.nan)
            else:
                d_in = np.zeros(len(g), dtype=bool)
                d_dist = np.full(len(g), np.nan)
                d_in[ok], d_dist[ok] = _dentro_e_distanza(lat[ok], lon[ok], codice)
                dentro = d_in
                dist = d_dist
                categoria = np.where(dentro, "", "fuori").astype(object)
            # Coordinata di Neta, per confronto.
            neta_ok = g["COORD_VALIDE"].to_numpy()
            dist_neta = np.full(len(g), np.nan)
            if codice in con_poligono and neta_ok.any():
                dentro_n, dn = _dentro_e_distanza(g["LAT"].to_numpy(float)[neta_ok], g["LON"].to_numpy(float)[neta_ok], codice)
                dist_neta[neta_ok] = np.where(dentro_n, 0.0, dn)
            cade = np.array([""] * len(g), dtype=object)
            cade[ok] = _dentro_confini(lat[ok], lon[ok])
            for i, r in enumerate(g.itertuples(index=False)):
                if not ok[i] or not categoria[i]:
                    continue
                righe.append({
                    "COMUNE": comune, "DISTRETTO": codice, "DP": r.DP or "(servizio senza DP)", "INDIRIZZO": r.INDIRIZZO,
                    "SERVIZI": r.SERVIZI, "ORIGINE": ORIGINI.get(r.ORIGINE or "proposta", r.ORIGINE or ""),
                    "NOTA": r.NOTA or "", "CONFERMATO_DA": r.UTENTE or "", "CONFERMATO_IL": str(r.QUANDO or "")[:10],
                    "FONTE_POSIZIONE": "civico ANNCSU" if civico.iloc[i] else "coordinata di Neta",
                    "LAT": round(float(lat[i]), 6), "LON": round(float(lon[i]), 6),
                    "DISTANZA_M": None if np.isnan(dist[i]) else int(round(float(dist[i]))),
                    "CADE_IN": cade[i] or "fuori da ogni distretto",
                    "LAT_NETA": None if not neta_ok[i] else round(float(r.LAT), 6),
                    "LON_NETA": None if not neta_ok[i] else round(float(r.LON), 6),
                    "DISTANZA_NETA_M": None if np.isnan(dist_neta[i]) else int(round(float(dist_neta[i]))),
                    "CATEGORIA": CATEGORIE[categoria[i]],
                })
    colonne = ["COMUNE", "DISTRETTO", "DP", "INDIRIZZO", "SERVIZI", "ORIGINE", "NOTA", "CONFERMATO_DA", "CONFERMATO_IL",
               "FONTE_POSIZIONE", "LAT", "LON", "DISTANZA_M", "CADE_IN", "LAT_NETA", "LON_NETA", "DISTANZA_NETA_M", "CATEGORIA"]
    df = pd.DataFrame(righe, columns=colonne)
    if not df.empty:
        df = df.sort_values(["COMUNE", "DISTRETTO", "DISTANZA_M"], ascending=[True, True, False], na_position="last").reset_index(drop=True)
    return df


def riepilogo_per_distretto(df: pd.DataFrame, comuni: list[str]) -> pd.DataFrame:
    """Per distretto: quanti DP confermati, quanti fuori confine, distanza mediana e massima."""
    if df.empty:
        return pd.DataFrame(columns=["COMUNE", "DISTRETTO", "DP_FUORI_CONFINE", "DISTANZA_MEDIANA_M", "DISTANZA_MASSIMA_M", "DA_CIVICO", "DA_COORDINATA_NETA"])
    g = df.groupby(["COMUNE", "DISTRETTO"])
    r = g.agg(
        DP_FUORI_CONFINE=("DP", "size"),
        DISTANZA_MEDIANA_M=("DISTANZA_M", "median"),
        DISTANZA_MASSIMA_M=("DISTANZA_M", "max"),
        DA_CIVICO=("FONTE_POSIZIONE", lambda x: int((x == "civico ANNCSU").sum())),
        DA_COORDINATA_NETA=("FONTE_POSIZIONE", lambda x: int((x == "coordinata di Neta").sum())),
    ).reset_index()
    r["DISTANZA_MEDIANA_M"] = r["DISTANZA_MEDIANA_M"].round(0)
    return r.sort_values("DP_FUORI_CONFINE", ascending=False).reset_index(drop=True)


LEGENDA = [
    ("Cosa contiene", "Punti di erogazione (DP) gia' CONFERMATI in un distretto che, secondo la posizione usata, stanno FUORI dal confine "
                      "di quel distretto: probabili confini da rivedere. Non va a Neta."),
    ("Perche' solo i confermati", "Se il distretto e' stato confermato giusto (spostato a mano, dalla proposta, con una zona, o "
                                  "'mantieni attuale') e il punto sta fuori dal poligono, il problema e' il confine, non l'assegnazione di Neta."),
    ("Cosa non c'e'", "I DP NO DISTRETTO (NODMA), sia in Neta sia confermati: essere fuori confine e' corretto. Nessuna soglia sulla "
                      "distanza: si ordina per distanza, dal piu' lontano."),
    ("Posizione usata", "Il civico ANNCSU (la fonte piu' autorevole); se il civico manca, la coordinata di Neta. Nel foglio ci sono "
                        "anche la coordinata di Neta e la sua distanza dal confine, per confronto."),
    ("Distanza dal confine (m)", "Quanto il punto sta fuori dal poligono del distretto confermato."),
    ("Cade in", "Il distretto in cui cade il punto, o 'fuori da ogni distretto'."),
    ("Categoria", "'Fuori dal confine del distretto' oppure 'Distretto senza confine disegnato' (il distretto non ha poligono nel file dei confini)."),
    ("Fonti", "ANNCSU - Agenzia delle Entrate e ISTAT (CC-BY 4.0); confini dei distretti: file distretti_confini.geojson del progetto."),
]

COLONNE_EXCEL = {
    "COMUNE": "Comune", "DISTRETTO": "Distretto confermato", "DP": "Punto di erogazione (DP)", "INDIRIZZO": "Indirizzo",
    "SERVIZI": "Codici servizio", "ORIGINE": "Come e' stato confermato", "NOTA": "Nota", "CONFERMATO_DA": "Confermato da",
    "CONFERMATO_IL": "Confermato il", "FONTE_POSIZIONE": "Posizione usata", "LAT": "Latitudine usata", "LON": "Longitudine usata",
    "DISTANZA_M": "Distanza dal confine (m)", "CADE_IN": "Cade in", "LAT_NETA": "Latitudine Neta", "LON_NETA": "Longitudine Neta",
    "DISTANZA_NETA_M": "Distanza Neta dal confine (m)", "CATEGORIA": "Categoria",
}


def esporta_gis_excel(comuni: list[str]) -> bytes:
    df = fuori_confine_confermati(comuni)
    riepilogo = riepilogo_per_distretto(df, comuni)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        riep = riepilogo.rename(columns={
            "COMUNE": "Comune", "DISTRETTO": "Distretto", "DP_FUORI_CONFINE": "DP fuori confine", "DISTANZA_MEDIANA_M": "Distanza mediana (m)",
            "DISTANZA_MASSIMA_M": "Distanza massima (m)", "DA_CIVICO": "Posizione dal civico ANNCSU", "DA_COORDINATA_NETA": "Posizione dalla coordinata di Neta"})
        riep.to_excel(writer, index=False, sheet_name="Riepilogo per distretto")
        det = df[list(COLONNE_EXCEL)].rename(columns=COLONNE_EXCEL)
        det.to_excel(writer, index=False, sheet_name="DP fuori confine")
        pd.DataFrame(LEGENDA, columns=["Voce", "Spiegazione"]).to_excel(writer, index=False, sheet_name="Legenda")
        for nome, tabella in (("Riepilogo per distretto", riep), ("DP fuori confine", det)):
            foglio = writer.sheets[nome]
            for i, col in enumerate(tabella.columns, start=1):
                larghezza = min(60, max(10, len(col) + 2, *(len(str(v)) + 2 for v in tabella[col].head(300))))
                foglio.column_dimensions[foglio.cell(row=1, column=i).column_letter].width = larghezza
            foglio.freeze_panes = "A2"
            foglio.auto_filter.ref = foglio.dimensions
        fl = writer.sheets["Legenda"]
        fl.column_dimensions["A"].width = 30
        fl.column_dimensions["B"].width = 130
    return buffer.getvalue()


def geojson_gis(comuni: list[str]) -> str:
    """Punti (posizione usata) con le stesse proprieta' dell'Excel, da aprire in QGIS con i poligoni."""
    df = fuori_confine_confermati(comuni)
    feature = []
    for r in df.to_dict("records"):
        prop = {COLONNE_EXCEL[k]: (None if pd.isna(v) else v) for k, v in r.items() if k in COLONNE_EXCEL and k not in ("LAT", "LON")}
        feature.append({"type": "Feature", "properties": prop, "geometry": {"type": "Point", "coordinates": [r["LON"], r["LAT"]]}})
    return json.dumps({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
                       "features": feature}, ensure_ascii=False)
