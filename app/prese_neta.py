"""
Registro degli invii a Neta e file per Neta (distretti confermati). Staccato da app/prese.py il
27/09/2026 (solo spostato, stesso codice).
"""
from __future__ import annotations

from datetime import datetime
import io

import numpy as np
import pandas as pd

from app import database
from app.prese_coordinate import (
    LEGENDA_COORDINATE,
    _excel,
    coordinate_da_verificare,
)
from app.prese import (
    MOTIVI,
    ORIGINI,
    _numero,
    _versione_conferme,
    _versione_dati,
    assicura_tabelle,
    prese_da_assegnare,
)


def invii_per_presa(comune: str, tipo: str) -> dict[str, tuple[int, str]]:
    """{chiave presa: (quante volte inviata, data ultimo invio)} per il comune."""
    with database.connessione() as conn:
        assicura_tabelle(conn)
        return {
            chiave: (n, ultimo) for chiave, n, ultimo in conn.execute("""
                SELECT p.CHIAVE, COUNT(*), MAX(i.QUANDO) FROM invii_neta_prese p
                JOIN invii_neta i ON i.ID = p.ID_INVIO
                WHERE p.LOCALITA = ? AND i.TIPO = ? GROUP BY p.CHIAVE""", (comune.strip().upper(), tipo))
        }

def _da_inviare(tipo: str, comune: str) -> pd.DataFrame:
    """Prese del comune da mettere nel file per Neta: conferme non ancora
    recepite, oppure coordinate da verificare. Colonna VALORE = cosa si
    chiede a Neta (distretto, o il problema della coordinata)."""
    if tipo == "distretti":
        p = prese_da_assegnare(comune)
        if p.empty:
            return p
        p = p[p["CONFERMATO"].notna() & ~p["RECEPITO"]]
        return p.assign(VALORE=p["CONFERMATO"])
    p = coordinate_da_verificare(comune)
    return p if p.empty else p.assign(VALORE=p["PROBLEMA"])

def registra_invio(tipo: str, comuni: list[str], utente: str) -> tuple[int, int, bytes]:
    """Prepara il file per Neta (solo cio' che e' ancora aperto) e registra
    l'invio. Restituisce (id invio, prese, Excel). Nel file: quante volte
    ogni presa era gia' stata inviata e quando la prima volta, per i
    solleciti."""
    if tipo not in ("distretti", "coordinate"):
        raise ValueError("Tipo di invio sconosciuto.")
    parti = []
    for comune in comuni:
        p = _da_inviare(tipo, comune)
        if p.empty:
            continue
        with database.connessione() as conn:
            assicura_tabelle(conn)
            prima = {
                chiave: (n, primo) for chiave, n, primo in conn.execute("""
                    SELECT p.CHIAVE, COUNT(*), MIN(i.QUANDO) FROM invii_neta_prese p
                    JOIN invii_neta i ON i.ID = p.ID_INVIO
                    WHERE p.LOCALITA = ? AND i.TIPO = ? GROUP BY p.CHIAVE""", (comune.strip().upper(), tipo))
            }
        parti.append(p.assign(
            COMUNE=comune.strip().upper(),
            N_PRIMA=[prima.get(k, (0, ""))[0] for k in p["CHIAVE"]],
            PRIMO_INVIO=[(prima.get(k, (0, ""))[1] or "")[:10] for k in p["CHIAVE"]],
        ))
    if not parti:
        raise ValueError("Niente da inviare: nessuna presa aperta per questi comuni.")
    df = pd.concat(parti, ignore_index=True)
    adesso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with database.connessione() as conn:
        assicura_tabelle(conn)
        cur = conn.execute(
            "INSERT INTO invii_neta (QUANDO, UTENTE, TIPO, COMUNI, N_PRESE) VALUES (?,?,?,?,?)",
            (adesso, utente, tipo, ", ".join(sorted(df["COMUNE"].unique())), len(df)),
        )
        id_invio = cur.lastrowid
        conn.executemany(
            "INSERT INTO invii_neta_prese VALUES (?,?,?,?,?,?)",
            [(id_invio, r.COMUNE, r.CHIAVE, r.VALORE, _numero(r.LAT), _numero(r.LON)) for r in df.itertuples(index=False)],
        )
        conn.commit()

    df["DP"] = np.where(df["DP"] == "", "(servizio senza presa)", df["DP"])
    df["INVIATA_PRIMA"] = [f"{n} volte, la prima il {d}" if n else "" for n, d in zip(df["N_PRIMA"], df["PRIMO_INVIO"])]
    comuni_col = {
        "COMUNE": "Comune", "DP": "Presa (DP)", "INDIRIZZO": "Indirizzo", "CAP": "CAP",
        "SERVIZI": "Codici servizio", "N_SERVIZI": "N. servizi", "DISTRETTO": "Distretto attuale",
    }
    if tipo == "distretti":
        df["ORIGINE_TESTO"] = df["ORIGINE"].map(ORIGINI).fillna("")
        df["MOTIVO_TESTO"] = df["MOTIVO"].map(MOTIVI).fillna("")
        colonne = {**COLONNE_NETA_DISTRETTI, "INVIATA_PRIMA": "Gia' inviata", "LAT": "Latitudine", "LON": "Longitudine"}
    else:
        colonne = {**comuni_col, "VALORE": "Problema", "DISTANZA_M": "Distanza (m)", "INVIATA_PRIMA": "Gia' inviata",
                   "LAT": "Latitudine", "LON": "Longitudine",
                   "LAT_CORRETTA": "Latitudine corretta (proposta)", "LON_CORRETTA": "Longitudine corretta (proposta)",
                   "AFFIDABILITA": "Affidabilita' della proposta", "NOTA_PROPOSTA": "Fonte e controlli della proposta"}
    return id_invio, len(df), _excel(df[list(colonne)].rename(columns=colonne), "Distretti" if tipo == "distretti" else "Coordinate",
                                     None if tipo == "distretti" else LEGENDA_COORDINATE)

def elenco_invii(limite: int = 50) -> list[dict]:
    """Invii registrati, dal piu' recente, con quante prese sono ancora
    aperte oggi (conferma non recepita, o coordinata ancora da verificare)
    e quante risolte da Neta."""
    with database.connessione() as conn:
        assicura_tabelle(conn)
        invii = pd.read_sql("SELECT * FROM invii_neta ORDER BY ID DESC LIMIT ?", conn, params=(limite,))
        if invii.empty:
            return []
        prese_inviate = pd.read_sql(
            f"SELECT ID_INVIO, LOCALITA, CHIAVE FROM invii_neta_prese WHERE ID_INVIO IN ({','.join('?' * len(invii))})",
            conn, params=[int(i) for i in invii["ID"]])
    aperte: dict[tuple[str, str], set[str]] = {}
    for tipo, comune in {(r.TIPO, c) for r in invii.itertuples() for c in set(
            prese_inviate.loc[prese_inviate["ID_INVIO"] == r.ID, "LOCALITA"])}:
        aperte[(tipo, comune)] = set(_aperte_in_memoria(tipo, comune))
    righe = []
    for r in invii.itertuples(index=False):
        mie = prese_inviate[prese_inviate["ID_INVIO"] == r.ID]
        ancora = sum(k in aperte.get((r.TIPO, c), set()) for c, k in zip(mie["LOCALITA"], mie["CHIAVE"]))
        righe.append({
            "id": int(r.ID), "quando": r.QUANDO[:16], "utente": r.UTENTE, "tipo": r.TIPO, "comuni": r.COMUNI,
            "n_prese": int(r.N_PRESE), "aperte": int(ancora), "risolte": int(r.N_PRESE - ancora),
        })
    return righe

_CACHE_APERTE: dict = {"versione": None, "dati": {}}

def _aperte_in_memoria(tipo: str, comune: str) -> list[str]:
    """Chiavi delle prese ancora aperte per tipo e comune, in memoria finche'
    i dati non cambiano (le coordinate di tutti i comuni costano secondi).
    Per i distretti conta anche la versione delle conferme del comune."""
    versione = _versione_dati()
    if _CACHE_APERTE["versione"] != versione:
        _CACHE_APERTE.update(versione=versione, dati={})
    chiave = (tipo, comune)
    extra = _versione_conferme(comune) if tipo == "distretti" else ()
    if chiave not in _CACHE_APERTE["dati"] or _CACHE_APERTE["dati"][chiave][0] != extra:
        p = _da_inviare(tipo, comune)
        _CACHE_APERTE["dati"][chiave] = (extra, [] if p.empty else list(p["CHIAVE"]))
    return _CACHE_APERTE["dati"][chiave][1]

# Colonne del file distretti per Neta (Daniele, 26/09/2026): quello che va a
# Neta e' gia' validato, quindi niente "distretto da assegnare", "stato" ne'
# "distretto dall'indirizzo"; il distretto confermato si chiama "Distretto
# corretto" ed e' la terza colonna, quello di Neta "Vecchio distretto".
COLONNE_NETA_DISTRETTI = {
    "COMUNE": "Comune", "DP": "Presa (DP)", "CONFERMATO": "Distretto corretto", "DISTRETTO": "Vecchio distretto",
    "INDIRIZZO": "Indirizzo", "CAP": "CAP", "SERVIZI": "Codici servizio", "N_SERVIZI": "N. servizi",
    "MOTIVO_TESTO": "Motivo", "ORIGINE_TESTO": "Origine", "NOTA": "Nota",
    "PROPOSTA_DA": "Proposto da", "FONTI": "Fonti",
}

def esporta_excel(comuni: list[str], solo_confermate: bool) -> bytes:
    """Una riga per presa, codici servizio in una sola cella (Daniele,
    24/09/2026). solo_confermate: il file da mandare a Neta; altrimenti
    tutto l'elenco con proposte, per lavorarci fuori dall'app."""
    parti = []
    for comune in comuni:
        p = prese_da_assegnare(comune)
        if p.empty:
            continue
        if solo_confermate:
            # Le validate ("mantieni attuale") non chiedono niente a Neta.
            p = p[p["CONFERMATO"].notna() & ~p["VALIDATA"]]
        if p.empty:
            continue
        p = p.assign(COMUNE=comune)
        parti.append(p)
    colonne = {
        "COMUNE": "Comune", "DP": "Presa (DP)", "INDIRIZZO": "Indirizzo", "CAP": "CAP",
        "SERVIZI": "Codici servizio", "N_SERVIZI": "N. servizi",
        "DISTRETTO": "Distretto attuale", "MOTIVO_TESTO": "Motivo",
        "CONFERMATO": "Distretto da assegnare", "ORIGINE_TESTO": "Origine", "NOTA": "Nota", "STATO": "Stato",
        "PROPOSTA": "Distretto proposto", "PROPOSTA_DA": "Proposto da", "FONTI": "Fonti", "PROPOSTA_COME": "Posizione",
        "DISTRETTO_VIA": "Distretto dall'indirizzo",
        "CONFERMATO_DA": "Confermato da", "CONFERMATO_IL": "Confermato il",
        "LAT": "Latitudine", "LON": "Longitudine",
    }
    if parti:
        df = pd.concat(parti, ignore_index=True)
        df["DP"] = np.where(df["DP"] == "", "(servizio senza presa)", df["DP"])
        df["MOTIVO_TESTO"] = df["MOTIVO"].map(MOTIVI).fillna("")
        df["ORIGINE_TESTO"] = df["ORIGINE"].map(ORIGINI).fillna("")
        df["STATO"] = np.where(
            df["VALIDATA"], "Validata: distretto attuale corretto",
            np.where(df["RECEPITO"], "Già recepito da Neta",
            np.where(df["CONFERMATO"].notna(), "Da recepire", "Da verificare")),
        )
        df["PROPOSTA_COME"] = [
            "" if not prop or da.startswith("via") else ("dentro il confine" if pd.isna(d) else f"fuori, a {int(d)} m")
            for prop, d, da in zip(df["PROPOSTA"], df["DISTANZA_M"], df["PROPOSTA_DA"])
        ]
        df.loc[~df["COORD_VALIDE"], "PROPOSTA_COME"] = "coordinate non valide"
        if solo_confermate:
            colonne = {**COLONNE_NETA_DISTRETTI, "LAT": "Latitudine", "LON": "Longitudine"}
        df = df[list(colonne)].rename(columns=colonne)
    else:
        if solo_confermate:
            colonne = {**COLONNE_NETA_DISTRETTI, "LAT": "Latitudine", "LON": "Longitudine"}
        df = pd.DataFrame(columns=list(colonne.values()))
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Prese")
        foglio = writer.sheets["Prese"]
        for i, col in enumerate(df.columns, start=1):
            larghezza = min(60, max(10, len(col) + 2, *(len(str(v)) + 2 for v in df[col].head(500))))
            foglio.column_dimensions[foglio.cell(row=1, column=i).column_letter].width = larghezza
        foglio.freeze_panes = "A2"
        foglio.auto_filter.ref = foglio.dimensions
    return buffer.getvalue()
