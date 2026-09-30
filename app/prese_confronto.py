"""
Controllo prese a ogni caricamento (Daniele, 29/09/2026): per ogni presa del
file, il distretto scritto da Neta confrontato con quello che abbiamo noi
(la conferma del tab Prese, oppure il distretto che Neta aveva scritto
nell'estrazione precedente). E' ridondante con il tab Prese, ma e' un
controllo: a tendere va a zero, a parte le prese nuove.

Nota (Daniele, 29/09/2026): quello che qui e nel resto dell'app si chiamava
"presa" e' il DP di Neta, il punto di erogazione (delivery point, un
contatore), non la presa vera; nei testi visibili si chiama "punto di
erogazione (DP)". I nomi nel codice sono rimasti.

Il confronto si fa PRIMA di aggiornare anagrafica_servizi con il file (dopo,
il "prima" sarebbe gia' sovrascritto). Ogni controllo resta nella tabella
controlli_prese, per scaricare l'elenco e vedere l'andamento nel tempo.
"""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd

from app import database
from app.prese import DP_SEGNAPOSTO, _testo_codice, assegnazioni, assicura_tabelle
from app.prese_coordinate import _excel

# Stati di una presa nel controllo. I primi quattro sono differenze da
# guardare; RECEPITA e' la buona notizia (Neta ha corretto il CRM).
STATI = {
    "NON_RECEPITA": "Confermato da noi, Neta ha ancora un altro distretto",
    "MANTIENI_CAMBIATA": "\"Mantieni attuale\", ma Neta ha cambiato distretto",
    "CAMBIATA": "Non confermato, Neta ha cambiato distretto rispetto all'estrazione precedente",
    "NUOVA": "Punto di erogazione nuovo, mai visto nelle estrazioni precedenti",
    "RECEPITA": "Neta ha messo il distretto confermato da noi (recepito con questa estrazione)",
}
DIFFERENZE = ["NON_RECEPITA", "MANTIENI_CAMBIATA", "CAMBIATA", "NUOVA"]


def assicura_tabella(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS controlli_prese (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            QUANDO TEXT NOT NULL, UTENTE TEXT, LOCALITA TEXT NOT NULL, FILE TEXT,
            N_PRESE INTEGER, CONTEGGI TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS controlli_prese_righe (
            ID_CONTROLLO INTEGER NOT NULL, CHIAVE TEXT NOT NULL, STATO TEXT NOT NULL,
            DISTRETTO_NETA TEXT, DISTRETTO_PRIMA TEXT, DISTRETTO_NOSTRO TEXT,
            INDIRIZZO TEXT, SERVIZI TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_controlli_righe ON controlli_prese_righe (ID_CONTROLLO)")
    conn.commit()


def _chiave(codici: pd.Series, dp: pd.Series) -> np.ndarray:
    """La presa: il DP, oppure il servizio da solo ('S' + codice) se il DP e'
    il segnaposto o manca, come nel tab Prese."""
    return np.where(dp.isin([DP_SEGNAPOSTO, ""]) | dp.isna(), "S" + codici, dp)


def _per_presa(s: pd.DataFrame) -> pd.DataFrame:
    """Da una riga per servizio a una per presa. Piu' servizi con distretti
    diversi sulla stessa presa: 'A / B'."""
    s = s.assign(DISTRETTO=s["DISTRETTO"].fillna("").astype(str).str.strip().str.upper())
    return s.groupby("CHIAVE", sort=False).agg(
        DISTRETTO=("DISTRETTO", lambda d: " / ".join(sorted(set(d)))),
        INDIRIZZO=("INDIRIZZO", "last"),
        SERVIZI=("CODICE_SERVIZIO", lambda c: ", ".join(c)),
    )


def confronta(df: pd.DataFrame, comune: str) -> tuple[pd.DataFrame, int] | None:
    """(una riga per presa del file che e' in uno degli STATI, prese nel
    file). None per il primo caricamento del comune (niente con cui
    confrontare) o se il file e' piu' vecchio di quanto gia' caricato: il
    confronto con dati piu' recenti non avrebbe senso."""
    comune = comune.strip().upper()
    with database.connessione() as conn:
        assicura_tabelle(conn)
        prima = pd.read_sql(
            "SELECT CODICE_SERVIZIO, DP, DISTRETTO, INDIRIZZO_UBICAZIONE AS INDIRIZZO, STATO_SERVIZIO, DATA_ESTRAZIONE "
            "FROM anagrafica_servizi WHERE LOCALITA = ?", conn, params=(comune,))
        conferme = assegnazioni(conn, comune)

    if prima.empty:
        return None
    data_file = pd.to_datetime(df["DATA_LETTURA"]).max().strftime("%Y-%m-%d %H:%M:%S")
    if data_file < prima["DATA_ESTRAZIONE"].max():
        return None

    # Servizi cessati (CFAT) esclusi, come nel tab Prese: la presa resta,
    # e' solo senza utenza attiva.
    ultime = df.sort_values("DATA_LETTURA").drop_duplicates("CODICE_SERVIZIO", keep="last")
    ultime = ultime[~ultime["STATO_SERVIZIO"].fillna("").astype(str).str.startswith("CFAT")]
    if ultime.empty:
        return pd.DataFrame(), 0
    nuovo = pd.DataFrame({
        "CODICE_SERVIZIO": [_testo_codice(c) for c in ultime["CODICE_SERVIZIO"]],
        "DP": [_testo_codice(d) for d in ultime["DP"]],
        "DISTRETTO": ultime["DISTRETTO"].to_numpy(),
        "INDIRIZZO": ultime["INDIRIZZO_UBICAZIONE"].fillna("").astype(str).to_numpy(),
    })
    # DP segnaposto nel file (Belgioioso mag-giu 2026): vale la presa vera
    # gia' nota in anagrafica.
    dp_noti = dict(zip(prima["CODICE_SERVIZIO"], prima["DP"].fillna("")))
    segnaposto = nuovo["DP"].isin([DP_SEGNAPOSTO, ""])
    nuovo.loc[segnaposto, "DP"] = nuovo.loc[segnaposto, "CODICE_SERVIZIO"].map(dp_noti).fillna("")
    nuovo["CHIAVE"] = _chiave(nuovo["CODICE_SERVIZIO"], nuovo["DP"])
    p = _per_presa(nuovo)

    prima["CHIAVE"] = _chiave(prima["CODICE_SERVIZIO"], prima["DP"])
    viste = set(prima["CHIAVE"])  # anche con soli servizi cessati: non e' nuova
    attive_prima = prima[~prima["STATO_SERVIZIO"].fillna("").str.startswith("CFAT")]
    p["DISTRETTO_PRIMA"] = (p.index.map(_per_presa(attive_prima)["DISTRETTO"]).fillna("")
                            if not attive_prima.empty else "")
    # Presa con soli servizi cessati prima: il distretto dell'ultimo noto.
    manca = (p["DISTRETTO_PRIMA"] == "") & p.index.isin(viste)
    if manca.any():
        ultimo = prima.sort_values("DATA_ESTRAZIONE").drop_duplicates("CHIAVE", keep="last").set_index("CHIAVE")["DISTRETTO"]
        p.loc[manca, "DISTRETTO_PRIMA"] = p.index[manca].map(ultimo).fillna("").str.strip().str.upper()

    conf = conferme[conferme["DISTRETTO"] != ""].set_index("DP")
    p["CONFERMATO"] = p.index.map(conf["DISTRETTO"]).fillna("")
    p["MANTIENI"] = p.index.map(conf["VALIDATA"]).fillna(0).astype(int) == 1

    confermata = (p["CONFERMATO"] != "") & ~p["MANTIENI"]
    uguale_conf = p["DISTRETTO"] == p["CONFERMATO"]
    cambiata = (p["DISTRETTO_PRIMA"] != "") & (p["DISTRETTO"] != p["DISTRETTO_PRIMA"])
    p["STATO"] = np.select(
        [
            confermata & ~uguale_conf,
            confermata & uguale_conf & (p["DISTRETTO_PRIMA"] != p["CONFERMATO"]),
            p["MANTIENI"] & ~uguale_conf,
            ~p.index.isin(viste),
            ~confermata & ~p["MANTIENI"] & cambiata,
        ],
        ["NON_RECEPITA", "RECEPITA", "MANTIENI_CAMBIATA", "NUOVA", "CAMBIATA"],
        default="",
    )
    # Il distretto che usiamo noi nel calcolo: la conferma, altrimenti Neta.
    p["DISTRETTO_NOSTRO"] = np.where(confermata, p["CONFERMATO"], p["DISTRETTO"])
    esito = p[p["STATO"] != ""].reset_index()
    return esito[["CHIAVE", "STATO", "DISTRETTO", "DISTRETTO_PRIMA", "DISTRETTO_NOSTRO", "INDIRIZZO", "SERVIZI"]], len(p)


def conteggi(esito: pd.DataFrame) -> dict[str, int]:
    presenti = esito["STATO"].value_counts().to_dict() if not esito.empty else {}
    return {s: int(presenti.get(s, 0)) for s in STATI}


def salva(esito: pd.DataFrame, n_prese: int, comune: str, nome_file: str, utente: str) -> int:
    """Registra il controllo e le sue prese. Restituisce l'ID."""
    with database.connessione() as conn:
        assicura_tabella(conn)
        cur = conn.execute(
            "INSERT INTO controlli_prese (QUANDO, UTENTE, LOCALITA, FILE, N_PRESE, CONTEGGI) VALUES (?,?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), utente, comune.strip().upper(), nome_file,
             n_prese, json.dumps(conteggi(esito))),
        )
        id_controllo = cur.lastrowid
        conn.executemany(
            "INSERT INTO controlli_prese_righe VALUES (?,?,?,?,?,?,?,?)",
            [(id_controllo, r.CHIAVE, r.STATO, r.DISTRETTO, r.DISTRETTO_PRIMA, r.DISTRETTO_NOSTRO, r.INDIRIZZO, r.SERVIZI)
             for r in esito.itertuples(index=False)],
        )
        conn.commit()
    return id_controllo


def controlla_e_salva(df: pd.DataFrame, comune: str, nome_file: str, utente: str) -> dict:
    """Per il caricamento: confronto + registrazione. Il dict va nell'esito
    della pagina Carica."""
    risultato = confronta(df, comune)
    if risultato is None:
        return {"saltato": "primo caricamento del comune, o estrazione più vecchia di quanto già caricato: confronto non fatto"}
    esito, n_prese = risultato
    id_controllo = salva(esito, n_prese, comune, nome_file, utente)
    c = conteggi(esito)
    return {"id": id_controllo, "n_prese": n_prese, "conteggi": c,
            "differenze": sum(c[s] for s in DIFFERENZE)}


def elenco_controlli(limite: int = 30) -> list[dict]:
    """Controlli registrati, dal piu' recente: l'andamento verso zero."""
    with database.connessione() as conn:
        assicura_tabella(conn)
        righe = conn.execute(
            "SELECT ID, QUANDO, UTENTE, LOCALITA, FILE, N_PRESE, CONTEGGI FROM controlli_prese ORDER BY ID DESC LIMIT ?",
            (limite,)).fetchall()
    uscita = []
    for id_, quando, utente, comune, file, n, c in righe:
        c = json.loads(c or "{}")
        uscita.append({"id": id_, "quando": quando[:16], "utente": utente, "comune": comune, "file": file,
                       "n_prese": n, "conteggi": c, "differenze": sum(c.get(s, 0) for s in DIFFERENZE)})
    return uscita


def esporta_excel(id_controllo: int) -> tuple[str, bytes]:
    """(nome file, Excel) con le prese di un controllo."""
    with database.connessione() as conn:
        assicura_tabella(conn)
        testa = conn.execute("SELECT LOCALITA, QUANDO FROM controlli_prese WHERE ID = ?", (id_controllo,)).fetchone()
        if testa is None:
            raise ValueError("Controllo non trovato.")
        righe = pd.read_sql("SELECT * FROM controlli_prese_righe WHERE ID_CONTROLLO = ?", conn, params=(id_controllo,))
    ordine = {s: i for i, s in enumerate(STATI)}
    righe = righe.sort_values(["STATO", "CHIAVE"], key=lambda c: c.map(ordine) if c.name == "STATO" else c)
    righe["DP"] = np.where(righe["CHIAVE"].str.startswith("S"), "(servizio senza DP)", righe["CHIAVE"])
    righe["STATO"] = righe["STATO"].map(STATI)
    colonne = {
        "STATO": "Esito", "DP": "Punto di erogazione (DP)", "DISTRETTO_NETA": "Distretto in Neta (questa estrazione)",
        "DISTRETTO_PRIMA": "Distretto in Neta (estrazione precedente)", "DISTRETTO_NOSTRO": "Distretto usato nel calcolo",
        "INDIRIZZO": "Indirizzo", "SERVIZI": "Codici servizio",
    }
    contenuto = _excel(righe[list(colonne)].rename(columns=colonne), "Controllo punti erogazione")
    nome = f"controllo_punti_erogazione_{testa[0].replace(' ', '_')}_{testa[1][:10]}.xlsx"
    return nome, contenuto
