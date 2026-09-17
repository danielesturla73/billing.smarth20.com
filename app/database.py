"""
Archivio storico delle letture — persistenza su SQLite.

Sostituisce i CSV per-comune usati finora (archivio_letture.csv,
archivio_letture_mortara.csv, ecc. — vedi motore_calcolo.carica_archivio/
aggiorna_archivio, ancora presenti nel codice ma non piu' usati
dall'applicativo web: restano solo come riferimento/CLI di comodo).

Un solo file DB (archivio/archivio.db) con TUTTI i comuni in un'unica
tabella "letture", distinti dalla colonna LOCALITA: la separazione "un
comune alla volta" richiesta dal calcolo (vedi app/main.py,
_risultati_per_comune — combinare comuni diversi confonderebbe il
controllo "utenze scomparse") resta a carico di chi legge, non e' un
limite dello storage: e' la stessa tabella che in futuro ospitera' anche
le tabelle di riferimento comuni/distretti e gli utenti (vedi specifiche,
sezione 6.1).

L'algoritmo di deduplica (a parita' di chiave CODICE_SERVIZIO+DATA_LETTURA
+TIPO_LETTURA vince la riga gia' presente) e' lo stesso, identico, di
motore_calcolo.aggiorna_archivio: cambia solo dove i dati vivono (una
tabella SQLite filtrata per comune, non un file CSV per comune).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from app import motore_calcolo

DB_PATH = Path("archivio") / "archivio.db"

COLONNE_LETTURE = motore_calcolo.COLONNE_ATTESE + ["FILE_ORIGINE"]
COLONNE_DATA = [
    "DATA_LETTURA", "DATA_FATTURAZ_LETTURA",
    "DATA_INIZIO_FORNITURA", "DATA_FINE_FORNITURA", "DATA_DECORR_BC",
]
CHIAVE_LETTURE = motore_calcolo.CHIAVE_ARCHIVIO  # CODICE_SERVIZIO, DATA_LETTURA, TIPO_LETTURA


def tabella_esiste(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='letture'"
    ).fetchone() is not None


def assicura_indici(conn: sqlite3.Connection) -> None:
    """Crea gli indici se la tabella esiste già. La tabella stessa NON
    viene creata qui vuota: deve nascere dal primo inserimento di dati
    reali (to_sql su un DataFrame non vuoto), perché solo allora pandas
    può dedurre i tipi giusti per colonna (numeri come INTEGER/REAL, non
    tutto TEXT) — esattamente come succedeva con l'inferenza automatica
    di pandas leggendo il CSV. Creare la tabella in anticipo da un
    DataFrame vuoto la farebbe nascere con tutte le colonne TEXT, e le
    letture numeriche (LETTURA, CONSUMO, GG_LETT_PREC...) tornerebbero
    come stringhe, rompendo i confronti numerici nel motore di calcolo.
    """
    if not tabella_esiste(conn):
        return
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_letture_chiave "
        "ON letture (CODICE_SERVIZIO, DATA_LETTURA, TIPO_LETTURA)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_letture_comune ON letture (LOCALITA)")
    conn.commit()


@contextmanager
def connessione():
    """Apre una connessione SQLite (WAL mode, cosi' le letture non
    vengono bloccate da una scrittura in corso), assicura che gli indici
    esistano (se la tabella già c'è), e la chiude sempre alla fine. Una
    connessione nuova per ogni operazione: sqlite3 non permette di
    condividerne una tra thread diversi, e ogni richiesta FastAPI puo'
    girare su un thread diverso (endpoint sincroni).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        assicura_indici(conn)
        yield conn
    finally:
        conn.close()


def _riapplica_date(df: pd.DataFrame) -> pd.DataFrame:
    for col in COLONNE_DATA:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def elenco_comuni(conn: sqlite3.Connection) -> list[str]:
    if not tabella_esiste(conn):
        return []
    righe = conn.execute(
        "SELECT DISTINCT LOCALITA FROM letture WHERE LOCALITA IS NOT NULL ORDER BY LOCALITA"
    ).fetchall()
    return [r[0] for r in righe]


def carica_letture(conn: sqlite3.Connection, comune: str) -> pd.DataFrame:
    """Legge tutte le letture di un comune, con le stesse colonne data
    riconvertite a datetime che aveva carica_archivio() per il CSV.
    """
    if not tabella_esiste(conn):
        return pd.DataFrame(columns=COLONNE_LETTURE)
    df = pd.read_sql("SELECT * FROM letture WHERE LOCALITA = ?", conn, params=(comune,))
    return _riapplica_date(df)


def aggiorna_letture(nuovi_file: list[str | Path], comune: str) -> tuple[pd.DataFrame, dict]:
    """Aggiunge una o piu' nuove estrazioni allo storico di un comune,
    con la stessa logica (e le stesse garanzie) di
    motore_calcolo.aggiorna_archivio: legge quanto gia' presente per quel
    comune, unisce le righe nuove, elimina i duplicati sulla chiave
    CODICE_SERVIZIO+DATA_LETTURA+TIPO_LETTURA (a parita' di chiave vince
    la riga gia' in archivio), e sostituisce lo storico di quel comune
    con il risultato deduplicato.
    """
    with connessione() as conn:
        esistenti = carica_letture(conn, comune)
        righe_prima = len(esistenti)

        nuovi_df = [motore_calcolo.carica_estrazione(p) for p in nuovi_file]
        aggiunte = pd.concat(nuovi_df, ignore_index=True) if nuovi_df else pd.DataFrame(columns=COLONNE_LETTURE)

        combinato = aggiunte.copy() if esistenti.empty else pd.concat([esistenti, aggiunte], ignore_index=True)
        prima_dedup = len(combinato)
        combinato = combinato.sort_values("DATA_LETTURA").drop_duplicates(subset=CHIAVE_LETTURE, keep="first")
        duplicate_scartate = prima_dedup - len(combinato)

        if tabella_esiste(conn):
            conn.execute("DELETE FROM letture WHERE LOCALITA = ?", (comune,))
        # Le colonne data vanno scritte come testo ISO (stesso formato che
        # aveva il CSV): sqlite non ha un tipo data nativo, e vogliamo
        # poterle rileggere con lo stesso pd.to_datetime di _riapplica_date.
        da_scrivere = combinato.copy()
        for col in COLONNE_DATA:
            if col in da_scrivere.columns:
                da_scrivere[col] = da_scrivere[col].dt.strftime("%Y-%m-%d %H:%M:%S")
        da_scrivere.to_sql("letture", conn, if_exists="append", index=False)
        conn.commit()
        assicura_indici(conn)

    stats = {
        "righe_archivio_prima": righe_prima,
        "righe_lette_dai_nuovi_file": len(aggiunte),
        "righe_duplicate_scartate": duplicate_scartate,
        "righe_archivio_dopo": len(combinato),
        "righe_nuove_aggiunte_davvero": len(combinato) - righe_prima,
    }
    return combinato, stats
