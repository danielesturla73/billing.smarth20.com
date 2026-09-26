"""
Consolidamento annuale del bilancio (Daniele, 26/09/2026): a fine anno si
consolida e se ne tiene una copia fissa; indietro non si torna. Da quel
momento, per i mesi dell'anno consolidato, Import_WMS e' sempre la copia,
anche se poi cambiano letture o conferme del tab Prese. Il ricalcolo di
oggi resta visibile solo per confronto.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from app import database

COLONNE = ["Mese", "Codice Distretto", "Volume Fatturato (m3)", "Source", "Note"]


def assicura_tabelle(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consolidamenti (
            ANNO INTEGER PRIMARY KEY, QUANDO TEXT NOT NULL, UTENTE TEXT, NOTA TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consolidato_volumi (
            ANNO INTEGER NOT NULL, COMUNE TEXT NOT NULL, MESE TEXT NOT NULL, CODICE_DISTRETTO TEXT NOT NULL,
            VOLUME REAL, SOURCE TEXT, NOTE TEXT,
            PRIMARY KEY (ANNO, COMUNE, MESE, CODICE_DISTRETTO)
        )""")
    conn.commit()


def anni_consolidati() -> list[dict]:
    with database.connessione() as conn:
        assicura_tabelle(conn)
        return [dict(zip(("anno", "quando", "utente", "nota"), r))
                for r in conn.execute("SELECT ANNO, QUANDO, UTENTE, NOTA FROM consolidamenti ORDER BY ANNO")]


def consolida(anno: int, risultati: dict[str, pd.DataFrame], utente: str, nota: str = "") -> int:
    """Salva la copia fissa di Import_WMS (volumi_distretto_mese di ogni
    comune, come calcolato adesso) per i mesi dell'anno. Rifiuta un anno
    gia' consolidato. Restituisce quante righe ha salvato."""
    with database.connessione() as conn:
        assicura_tabelle(conn)
        if conn.execute("SELECT 1 FROM consolidamenti WHERE ANNO = ?", (anno,)).fetchone():
            raise ValueError(f"L'anno {anno} è già consolidato: indietro non si torna.")
        righe = []
        for comune, v in risultati.items():
            if v is None or v.empty:
                continue
            v = v[v["Mese"].astype(str).str.startswith(f"{anno}-")]
            righe += [(anno, comune, str(r["Mese"]), r["Codice Distretto"], float(r["Volume Fatturato (m3)"]),
                       r.get("Source", ""), r.get("Note", "")) for r in v.to_dict("records")]
        if not righe:
            raise ValueError(f"Nessun volume per l'anno {anno}.")
        conn.executemany("INSERT INTO consolidato_volumi VALUES (?,?,?,?,?,?,?)", righe)
        conn.execute("INSERT INTO consolidamenti VALUES (?,?,?,?)",
                     (anno, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), utente, nota.strip()[:500]))
        conn.commit()
    return len(righe)


def volumi_consolidati(comune: str | None = None) -> pd.DataFrame:
    with database.connessione() as conn:
        assicura_tabelle(conn)
        sql = """SELECT v.ANNO, v.COMUNE, v.MESE, v.CODICE_DISTRETTO, v.VOLUME, v.SOURCE, v.NOTE, c.QUANDO
                 FROM consolidato_volumi v JOIN consolidamenti c ON c.ANNO = v.ANNO"""
        if comune:
            return pd.read_sql(sql + " WHERE v.COMUNE = ?", conn, params=(comune.strip().upper(),))
        return pd.read_sql(sql, conn)


def applica(volumi: pd.DataFrame, comune: str) -> pd.DataFrame:
    """Import_WMS del comune con i mesi degli anni consolidati sostituiti
    dalla copia fissa (con la data del consolidamento nelle note)."""
    c = volumi_consolidati(comune)
    if c.empty:
        return volumi
    anni = {str(a) for a in c["ANNO"]}
    tenuti = volumi[~volumi["Mese"].astype(str).str[:4].isin(anni)] if not volumi.empty else volumi
    fissi = pd.DataFrame({
        "Mese": c["MESE"], "Codice Distretto": c["CODICE_DISTRETTO"], "Volume Fatturato (m3)": c["VOLUME"],
        "Source": c["SOURCE"],
        "Note": [f"{n}; consolidato il {q[:10]}" if n else f"consolidato il {q[:10]}" for n, q in zip(c["NOTE"].fillna(""), c["QUANDO"])],
    })
    if not volumi.empty and isinstance(volumi["Mese"].dtype, pd.PeriodDtype):
        fissi["Mese"] = pd.PeriodIndex(fissi["Mese"], freq="M")
    colonne = list(volumi.columns) if not volumi.empty else COLONNE
    for col in colonne:
        if col not in fissi.columns:
            fissi[col] = ""
    return pd.concat([tenuti, fissi[colonne]], ignore_index=True).sort_values(["Mese", "Codice Distretto"]).reset_index(drop=True)
