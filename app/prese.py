"""
Prese da associare a un distretto — tab "Prese" (richiesto da Daniele il
24/09/2026).

Neta H2O associa i distretti alle PRESE (colonna DP), non ai singoli codici
servizio: questa pagina elenca, per un comune, le prese il cui distretto e'
NODMA ("NO DISTRETTO"), "*"/vuoto, un codice di un altro comune non
associabile (stessa regola di motore_calcolo.classifica_distretto), oppure
un distretto valido che non torna con la posizione della presa (dentro il
confine di un altro distretto, o lontana dal suo; Daniele, 25/09/2026). Le
prese sono fisse: cambia solo l'utenza collegata, e una presa assente
dall'ultima estrazione e' solo senza utenza attiva, non sparita. Le mostra
in mappa sopra i confini dei distretti, propone un distretto dalla posizione
(dentro un confine, o il piu' vicino entro DISTANZA_MAX_PROPOSTA_M) e lascia
all'editor la conferma. Le conferme finiscono in un file Excel da passare a
Neta: NON cambiano i volumi calcolati (Daniele, 24/09/2026) — il distretto
giusto entra nel calcolo quando Neta corregge il CRM e arriva l'estrazione
successiva.

Coordinate: vengono corrette da Neta nel tempo, quindi vale sempre l'ultima
estrazione (Daniele, 24/09/2026). L'archivio letture non basta: a parita' di
chiave CODICE_SERVIZIO+DATA_LETTURA+TIPO_LETTURA tiene la riga gia'
presente, e una coordinata corretta su una lettura gia' caricata andrebbe
persa. Per questo c'e' una tabella a parte, anagrafica_servizi (una riga per
codice servizio), aggiornata a ogni caricamento: vince il file con le letture
piu' recenti (a pari data, l'ultimo caricato), cosi' ricaricare per errore
un'estrazione vecchia non riporta indietro coordinate gia' corrette.

Dal 27/09/2026 una parte del codice sta in moduli a se' (solo spostata,
stesso codice): prese_geo (confini, distanze, proposta dalla posizione),
prese_coordinate (coordinate da verificare), prese_neta (invii e file per
Neta). Tutto resta raggiungibile come prese.<nome>.
"""
from __future__ import annotations

import collections
import math
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app import anncsu, cache_disco, database, motore_calcolo, stradario, vie_osm

# DP segnaposto di Neta, non una presa vera: non raggruppa nulla, ogni
# servizio resta una riga a se'. L'estrazione di Belgioioso di mag-giu 2026
# lo aveva su TUTTE le righe (2.726 servizi), mentre le precedenti avevano la
# presa vera (Daniele, 25/09/2026): un DP segnaposto o vuoto non sovrascrive
# mai una presa vera gia' nota, ne' in anagrafica ne' nella riparazione.
DP_SEGNAPOSTO = "301801000000000"

# Rettangolo largo attorno alla provincia di Pavia: fuori da qui la coordinata
# e' assurda (0,0, virgola persa tipo 8742620 invece di 8.742620...) e la
# presa non va in mappa, resta solo nell'elenco.
LAT_VALIDA = (44.5, 45.7)
LON_VALIDA = (8.3, 9.7)


# Distretti soppressi, fusi in un altro (elenco in motore_calcolo, che nel
# calcolo li unisce gia' al distretto nuovo): qui le loro prese vanno
# associate in automatico al distretto nuovo e finiscono nel file per Neta
# (vecchio -> nuovo), senza conferma a mano.
DISTRETTI_FUSI = motore_calcolo.DISTRETTI_FUSI

MOTIVI = {
    "NODMA": "NO DISTRETTO",
    "ND": "Distretto non indicato (*)",
    "ALTRO": "Distretto di un altro comune",
    "POSIZIONE": "Distretto diverso dalla posizione",
    "FUSO": "Distretto soppresso (fuso in un altro)",
    "VIA": "Via di un altro distretto",
}

COLONNE_ANAGRAFICA = [
    "CODICE_SERVIZIO", "DP", "LOCALITA", "INDIRIZZO_UBICAZIONE", "CAP_UBICAZIONE",
    "Latitudine", "Longitudine", "DISTRETTO", "STATO_SERVIZIO",
]


# ---------------------------------------------------------------------------
# Tabelle
# ---------------------------------------------------------------------------

def assicura_tabelle(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS anagrafica_servizi (
            CODICE_SERVIZIO TEXT PRIMARY KEY,
            DP TEXT, LOCALITA TEXT, INDIRIZZO_UBICAZIONE TEXT, CAP_UBICAZIONE TEXT,
            Latitudine REAL, Longitudine REAL, DISTRETTO TEXT, STATO_SERVIZIO TEXT,
            FILE_ORIGINE TEXT, DATA_ESTRAZIONE TEXT, AGGIORNATO_IL TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_anagrafica_comune ON anagrafica_servizi (LOCALITA)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prese_assegnazioni (
            LOCALITA TEXT NOT NULL, DP TEXT NOT NULL,
            DISTRETTO TEXT NOT NULL, UTENTE TEXT, QUANDO TEXT,
            PRIMARY KEY (LOCALITA, DP)
        )""")
    # VALIDATA = 1: "mantieni attuale", il distretto che la presa ha gia' in
    # Neta e' giusto e la segnalazione era un falso allarme (Daniele,
    # 25/09/2026). Conta come recepita, non va nel file per Neta.
    colonne = {r[1] for r in conn.execute("PRAGMA table_info(prese_assegnazioni)")}
    if "VALIDATA" not in colonne:
        conn.execute("ALTER TABLE prese_assegnazioni ADD COLUMN VALIDATA INTEGER NOT NULL DEFAULT 0")
    # ORIGINE: 'proposta' (dell'app), 'manuale' (presa spostata a mano),
    # 'zona' (spostata con una zona disegnata in mappa), 'mantieni'; NOTA:
    # il perche', scritto da chi sposta (Daniele, 26/09/2026). Vanno nel file
    # per Neta.
    if "ORIGINE" not in colonne:
        conn.execute("ALTER TABLE prese_assegnazioni ADD COLUMN ORIGINE TEXT NOT NULL DEFAULT ''")
    if "NOTA" not in colonne:
        conn.execute("ALTER TABLE prese_assegnazioni ADD COLUMN NOTA TEXT NOT NULL DEFAULT ''")
    # Registro degli invii a Neta (Daniele, 25/09/2026): cosa e' stato
    # mandato, quando e da chi, per vedere cosa Neta ha recepito e cosa
    # sollecitare. TIPO: 'distretti' (conferme) o 'coordinate'.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invii_neta (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            QUANDO TEXT NOT NULL, UTENTE TEXT, TIPO TEXT NOT NULL,
            COMUNI TEXT, N_PRESE INTEGER
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invii_neta_prese (
            ID_INVIO INTEGER NOT NULL, LOCALITA TEXT NOT NULL, CHIAVE TEXT NOT NULL,
            VALORE TEXT, LAT REAL, LON REAL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invii_prese ON invii_neta_prese (LOCALITA, CHIAVE)")
    conn.commit()


def _testo_codice(v) -> str:
    """Codici numerici lunghi (DP, CODICE_SERVIZIO) come testo senza '.0'."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _numero(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def aggiorna_anagrafica(df: pd.DataFrame, nome_file: str) -> int:
    """Porta in anagrafica_servizi l'ultima riga (per DATA_LETTURA) di ogni
    servizio di UN file di estrazione. Sovrascrive solo se il file non e'
    piu' vecchio di quello che aveva gia' scritto quel servizio."""
    if df.empty:
        return 0
    data_estrazione = pd.to_datetime(df["DATA_LETTURA"]).max().strftime("%Y-%m-%d %H:%M:%S")
    adesso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ultime = df.sort_values("DATA_LETTURA").drop_duplicates("CODICE_SERVIZIO", keep="last")
    righe = [
        (
            _testo_codice(r.CODICE_SERVIZIO), _testo_codice(r.DP), str(r.LOCALITA).strip().upper(),
            None if pd.isna(r.INDIRIZZO_UBICAZIONE) else str(r.INDIRIZZO_UBICAZIONE),
            _testo_codice(r.CAP_UBICAZIONE),
            _numero(r.Latitudine), _numero(r.Longitudine),
            str(r.DISTRETTO).strip(), None if pd.isna(r.STATO_SERVIZIO) else str(r.STATO_SERVIZIO),
            nome_file, data_estrazione, adesso,
        )
        for r in ultime[COLONNE_ANAGRAFICA].itertuples(index=False)
    ]
    with database.connessione() as conn:
        assicura_tabelle(conn)
        conn.executemany("""
            INSERT INTO anagrafica_servizi VALUES (:c0,:c1,:c2,:c3,:c4,:c5,:c6,:c7,:c8,:c9,:c10,:c11)
            ON CONFLICT(CODICE_SERVIZIO) DO UPDATE SET
                DP=CASE WHEN excluded.DP IN (:segnaposto, '')
                         AND anagrafica_servizi.DP NOT IN (:segnaposto, '')
                        THEN anagrafica_servizi.DP ELSE excluded.DP END,
                LOCALITA=excluded.LOCALITA,
                INDIRIZZO_UBICAZIONE=excluded.INDIRIZZO_UBICAZIONE, CAP_UBICAZIONE=excluded.CAP_UBICAZIONE,
                Latitudine=excluded.Latitudine, Longitudine=excluded.Longitudine,
                DISTRETTO=excluded.DISTRETTO, STATO_SERVIZIO=excluded.STATO_SERVIZIO,
                FILE_ORIGINE=excluded.FILE_ORIGINE, DATA_ESTRAZIONE=excluded.DATA_ESTRAZIONE,
                AGGIORNATO_IL=excluded.AGGIORNATO_IL
            WHERE excluded.DATA_ESTRAZIONE >= anagrafica_servizi.DATA_ESTRAZIONE
        """, [{**{f"c{i}": v for i, v in enumerate(r)}, "segnaposto": DP_SEGNAPOSTO} for r in righe])
        conn.commit()
    return len(righe)


def ripara_dp_segnaposto() -> tuple[int, int]:
    """Rimette la presa vera ai servizi che in anagrafica hanno il DP
    segnaposto (o vuoto), prendendo l'ultima presa vera dalle letture in
    archivio, e sposta sulla presa le conferme gia' fatte sul singolo
    servizio (chiave 'S' + codice). Idempotente, gira a ogni avvio.
    Restituisce (servizi riparati, conferme spostate)."""
    with database.connessione() as conn:
        assicura_tabelle(conn)
        if not database.tabella_esiste(conn):
            return 0, 0
        da_riparare = [r[0] for r in conn.execute(
            "SELECT CODICE_SERVIZIO FROM anagrafica_servizi WHERE DP IN (?, '') OR DP IS NULL",
            (DP_SEGNAPOSTO,))]
        if not da_riparare:
            return 0, 0
        # In letture DP e CODICE_SERVIZIO sono interi: confronto come testo.
        veri = {}
        for codice, dp in conn.execute("""
            SELECT CAST(CODICE_SERVIZIO AS TEXT), CAST(DP AS TEXT) FROM letture
            WHERE DP IS NOT NULL AND CAST(DP AS TEXT) NOT IN (?, '')
            ORDER BY DATA_LETTURA""", (DP_SEGNAPOSTO,)):
            veri[_testo_codice(codice)] = _testo_codice(dp)  # vince l'ultima per data
        riparati = spostate = 0
        for codice in da_riparare:
            dp = veri.get(codice)
            if not dp:
                continue
            conn.execute("UPDATE anagrafica_servizi SET DP=? WHERE CODICE_SERVIZIO=?", (dp, codice))
            riparati += 1
            # La conferma sul servizio passa alla presa, se la presa non ne ha gia' una.
            spostate += conn.execute("""
                UPDATE OR IGNORE prese_assegnazioni SET DP=? WHERE DP=?""", (dp, "S" + codice)).rowcount
            conn.execute("DELETE FROM prese_assegnazioni WHERE DP=?", ("S" + codice,))
        conn.commit()
    if riparati:
        print(f"[prese] DP segnaposto: {riparati} servizi riparati, {spostate} conferme spostate sulla presa")
    return riparati, spostate


# Stato della ricostruzione iniziale (dai file gia' caricati), per la pagina.
STATO_RICOSTRUZIONE = {"in_corso": False, "fatti": 0, "totale": 0}


def anagrafica_vuota() -> bool:
    with database.connessione() as conn:
        assicura_tabelle(conn)
        return conn.execute("SELECT COUNT(*) FROM anagrafica_servizi").fetchone()[0] == 0


def ricostruisci_anagrafica() -> None:
    """Una volta sola, quando la tabella e' vuota (primo avvio con questa
    funzione): rilegge dalla cartella input/ i file gia' presenti in
    archivio, nell'ordine in cui erano stati caricati (data del file su
    disco). Qualche minuto, in background: la pagina Prese intanto lo dice."""
    if STATO_RICOSTRUZIONE["in_corso"] or not anagrafica_vuota():
        return
    with database.connessione() as conn:
        if not database.tabella_esiste(conn):
            return
        nomi = [r[0] for r in conn.execute("SELECT DISTINCT FILE_ORIGINE FROM letture")]
    percorsi = sorted(
        (p for p in (Path("input") / n for n in nomi if n) if p.exists()),
        key=lambda p: p.stat().st_mtime,
    )
    STATO_RICOSTRUZIONE.update(in_corso=True, fatti=0, totale=len(percorsi))
    try:
        for p in percorsi:
            try:
                aggiorna_anagrafica(motore_calcolo.carica_estrazione(p), p.name)
            except Exception as exc:  # un file illeggibile non blocca gli altri
                print(f"[prese] anagrafica: {p.name} saltato ({exc})")
            STATO_RICOSTRUZIONE["fatti"] += 1
    finally:
        STATO_RICOSTRUZIONE["in_corso"] = False


def avvia_ricostruzione_se_serve() -> None:
    def _lavoro():
        ricostruisci_anagrafica()
        ripara_dp_segnaposto()
        with database.connessione() as conn:
            assicura_tabelle(conn)
            comuni = [r[0] for r in conn.execute("SELECT DISTINCT LOCALITA FROM anagrafica_servizi ORDER BY 1")]
        riepilogo_comuni(comuni)
    threading.Thread(target=_lavoro, daemon=True).start()


# ---------------------------------------------------------------------------
# Prese di un comune
# ---------------------------------------------------------------------------

def _servizi_comune(conn, comune: str) -> pd.DataFrame:
    assicura_tabelle(conn)
    return pd.read_sql(
        "SELECT * FROM anagrafica_servizi WHERE LOCALITA = ?", conn, params=(comune.strip().upper(),)
    )


def assegnazioni(conn, comune: str | None = None) -> pd.DataFrame:
    assicura_tabelle(conn)
    if comune:
        return pd.read_sql("SELECT * FROM prese_assegnazioni WHERE LOCALITA = ?", conn, params=(comune.strip().upper(),))
    return pd.read_sql("SELECT * FROM prese_assegnazioni", conn)


def _coordinate_valide(lat: pd.Series, lon: pd.Series) -> pd.Series:
    return lat.between(*LAT_VALIDA) & lon.between(*LON_VALIDA)


_CACHE_PRESE: dict = {"versione": None, "dati": {}}
_LOCK_PRESE = threading.Lock()


def prese_comune(comune: str) -> pd.DataFrame:
    """Come _prese_comune, in memoria e su disco finche' i dati non cambiano
    (27/09/2026: si rifaceva a ogni richiesta, e piu' volte per richiesta;
    Voghera ~2,5 s ogni volta). Restituisce una copia."""
    versione = _versione_dati()
    with _LOCK_PRESE:
        if _CACHE_PRESE["versione"] != versione:
            _CACHE_PRESE.update(versione=versione, dati={})
        if comune in _CACHE_PRESE["dati"]:
            return _CACHE_PRESE["dati"][comune].copy()
    p = cache_disco.carica(f"prese_{comune}", (comune, versione))
    if p is None:
        p = _prese_comune(comune)
        cache_disco.salva(f"prese_{comune}", (comune, versione), p)
    with _LOCK_PRESE:
        if _CACHE_PRESE["versione"] == versione:
            _CACHE_PRESE["dati"][comune] = p
    return p.copy()


def _prese_comune(comune: str) -> pd.DataFrame:
    """Una riga per presa del comune (i servizi senza presa vera restano una
    riga ciascuno), escluse quelle con tutti i servizi cessati e gia'
    fatturati. Colonne: CHIAVE, DP, INDIRIZZO, CAP, SERVIZI (testo), N_SERVIZI,
    DISTRETTO (attuale, piu' codici separati da ' / '), MOTIVO (NODMA / ND /
    ALTRO / POSIZIONE / FUSO / VIA / '' se a posto), LAT, LON, COORD_VALIDE,
    DISTRETTO_VIA (dallo stradario, '' se manca),
    AUTOMATICO (distretto nuovo per le prese di un distretto fuso, o '')."""
    with database.connessione() as conn:
        s = _servizi_comune(conn, comune)
    if s.empty:
        return pd.DataFrame()
    s = s[~s["STATO_SERVIZIO"].fillna("").str.startswith("CFAT")].copy()
    if s.empty:
        return pd.DataFrame()

    # Stessa classificazione del motore (valido / case_sparse / anomalia),
    # sul distretto dell'ultima estrazione di ogni servizio.
    s["DISTRETTO"] = s["DISTRETTO"].fillna("").astype(str).str.strip()
    s = motore_calcolo.classifica_distretto(s)
    vuoto = s["DISTRETTO"].str.upper().isin(["*", "", "NAN", "NONE"])
    s["MOTIVO"] = np.select(
        [s["CATEGORIA_DISTRETTO"] == "case_sparse", vuoto, s["CATEGORIA_DISTRETTO"] == "anomalia"],
        ["NODMA", "ND", "ALTRO"], default="",
    )
    s["DISTRETTO"] = s["DISTRETTO"].where(~vuoto, "*")
    s.loc[s["DISTRETTO"].str.upper().isin(DISTRETTI_FUSI), "MOTIVO"] = "FUSO"
    s["CHIAVE"] = np.where(s["DP"].isin([DP_SEGNAPOSTO, ""]), "S" + s["CODICE_SERVIZIO"], s["DP"])
    s = s.sort_values(["DATA_ESTRAZIONE", "CODICE_SERVIZIO"])

    # Raggruppamento in Python semplice: groupby di pandas, un gruppo alla
    # volta, impiegava ~5 s su Voghera (~9.000 prese).
    priorita = {"ND": 5, "ALTRO": 4, "FUSO": 3, "POSIZIONE": 2, "NODMA": 1, "": 0}
    gruppi: dict[str, list[dict]] = {}
    for r in s[["CHIAVE", "CODICE_SERVIZIO", "INDIRIZZO_UBICAZIONE", "CAP_UBICAZIONE", "DISTRETTO",
                "MOTIVO", "Latitudine", "Longitudine"]].to_dict("records"):
        gruppi.setdefault(r["CHIAVE"], []).append(r)
    righe = []
    for chiave, g in gruppi.items():
        ultima = g[-1]  # s e' ordinato per estrazione: l'ultima e' la piu' recente
        distretti = [r["DISTRETTO"] for r in g]
        righe.append({
            "CHIAVE": chiave,
            "DP": "" if chiave.startswith("S") else chiave,
            "INDIRIZZO": ultima["INDIRIZZO_UBICAZIONE"] or "",
            "CAP": ultima["CAP_UBICAZIONE"] or "",
            "SERVIZI": ", ".join(r["CODICE_SERVIZIO"] for r in g),
            "N_SERVIZI": len(g),
            "DISTRETTO": " / ".join(dict.fromkeys(distretti)),
            "DISTRETTO_PRINCIPALE": max(dict.fromkeys(distretti), key=distretti.count),
            "MOTIVO": max((r["MOTIVO"] for r in g), key=priorita.get),
            "LAT": ultima["Latitudine"],
            "LON": ultima["Longitudine"],
        })
    p = pd.DataFrame(righe)
    p["LAT"] = pd.to_numeric(p["LAT"], errors="coerce")
    p["LON"] = pd.to_numeric(p["LON"], errors="coerce")
    p["COORD_VALIDE"] = _coordinate_valide(p["LAT"], p["LON"])

    # Distretto della via dallo stradario del comune (se generato). Prima
    # l'indirizzo, poi la posizione (Daniele, 25/09/2026, Via Trento 21 a
    # Belgioioso: Neta e via dicono DBLG02, la coordinata sbagliata cade in
    # DBLG03 e veniva proposto DBLG03): se la via ha un distretto decide la
    # via, il controllo sulla posizione vale solo per le prese senza.
    # Fonti indipendenti del distretto dall'indirizzo, incrociate e sempre
    # dichiarate (Daniele, 25/09/2026), dalla piu' affidabile:
    # - civico ANNCSU: il distretto in cui cade il civico vero (non si usa se
    #   il civico e' a meno di TOLLERANZA_BORDO_M da un confine);
    # - stradario: il distretto della via o del suo tratto di civici;
    # - via OSM: il tracciato OpenStreetMap della via sta (quasi) tutto in un
    #   distretto.
    # DISTRETTO_VIA = il distretto dalla prima fonte disponibile, FONTE_INDIRIZZO
    # quale fonte. D_POSIZIONE = dove cade la coordinata di Neta.
    strade = stradario.carica(comune)
    p["D_STRADARIO"] = stradario.distretti_da_via(strade, p["INDIRIZZO"]) if not strade.empty else ""
    civ = anncsu.civici_per_indirizzo(comune, p["INDIRIZZO"])
    p["CIV_LAT"] = civ["CIV_LAT"].to_numpy()
    p["CIV_LON"] = civ["CIV_LON"].to_numpy()
    p["CIVICO_ESISTE"] = civ["CIVICO_ESISTE"].to_numpy()
    p["CIV_METODO"] = civ["CIV_METODO"].astype(str).to_numpy()
    p["D_CIVICO"] = _distretti_sicuri(p["CIV_LAT"].to_numpy(dtype=float), p["CIV_LON"].to_numpy(dtype=float))
    osm_via = _distretto_osm_unico(comune, [stradario.normalizza_indirizzo(i)[0] for i in p["INDIRIZZO"]])
    p["D_OSM"] = [osm_via.get(stradario.normalizza_indirizzo(i)[0], "") for i in p["INDIRIZZO"]]
    p["D_POSIZIONE"] = [""] * len(p)
    v = np.nonzero(p["COORD_VALIDE"].to_numpy())[0]
    if len(v):
        for i, d in zip(v, _dentro_confini(p["LAT"].to_numpy(dtype=float)[v], p["LON"].to_numpy(dtype=float)[v])):
            p.iat[i, p.columns.get_loc("D_POSIZIONE")] = d
    fonti = [("civico ANNCSU", "D_CIVICO"), ("stradario", "D_STRADARIO"), ("via OSM", "D_OSM")]
    p["DISTRETTO_VIA"] = [next((r[c] for _, c in fonti if r[c]), "") for r in p[[c for _, c in fonti]].to_dict("records")]
    p["FONTE_INDIRIZZO"] = [next((n for n, c in fonti if r[c]), "") for r in p[[c for _, c in fonti]].to_dict("records")]

    # Distretto valido ma posizione che non torna (Daniele, 25/09/2026), per
    # ogni distretto della presa (piu' codici se i servizi non concordano):
    # - presa dentro il confine di un ALTRO distretto (es. a Belgioioso prese
    #   codificate DBLG02 Santa Margherita che stanno in DBLG03 Centro);
    # - presa fuori da ogni confine e a piu' di DISTANZA_MAX_PROPOSTA_M dal
    #   confine del suo distretto;
    # - distretto senza confine disegnato (Casteggio, Voghera in parte) e
    #   presa dentro il confine di un altro distretto, diverso da quello in
    #   cui cade la maggior parte delle prese di quel distretto.
    # Non si segnala se la presa e' a meno di TOLLERANZA_BORDO_M dal confine
    # del suo distretto (o, senza confine, dal bordo di quello in cui cade).
    con_confine = {c for c, _, _ in _poligoni()}
    da_controllare = np.nonzero(((p["MOTIVO"] == "") & p["COORD_VALIDE"] & (p["DISTRETTO_VIA"] == "")).to_numpy())[0]
    if len(da_controllare):
        lat = p["LAT"].to_numpy(dtype=float)[da_controllare]
        lon = p["LON"].to_numpy(dtype=float)[da_controllare]
        dal_confine = np.array(_dentro_confini(lat, lon), dtype=object)
        attuali = [set(d.split(" / ")) for d in p["DISTRETTO"].to_numpy()[da_controllare]]
        sbagliato = np.zeros(len(da_controllare), dtype=bool)
        for codice in set().union(*attuali):
            k = np.array([codice in a and dal_confine[i] != codice for i, a in enumerate(attuali)])
            if not k.any():
                continue
            k = np.nonzero(k)[0]
            if codice in con_confine:
                dentro, dist = _dentro_e_distanza(lat[k], lon[k], codice)
                lontana = np.where(dal_confine[k] != "", dist > TOLLERANZA_BORDO_M, dist > DISTANZA_MAX_PROPOSTA_M)
                sbagliato[k[~dentro & lontana]] = True
            else:
                # La zona abituale (dove cade la maggior parte delle sue
                # prese) non si segnala: e' il confine che manca, non il
                # distretto sbagliato. Era il caso di DVH02/DVH03 in DVH05 e
                # DCT04 in DCT13 (899 falsi allarmi), poi risultati fusi:
                # vedi DISTRETTI_FUSI. Un distretto senza confine e' con ogni
                # probabilita' un altro distretto fuso da aggiungere li'.
                abituale = collections.Counter(dal_confine[k]).most_common(1)[0][0]
                for pos in set(dal_confine[k]) - {"", abituale}:
                    j = k[dal_confine[k] == pos]
                    _, profondita = _dentro_e_distanza(lat[j], lon[j], pos)
                    sbagliato[j[profondita > TOLLERANZA_BORDO_M]] = True
        p.loc[p.index[da_controllare[sbagliato]], "MOTIVO"] = "POSIZIONE"

    # Via di un altro distretto: il distretto della via, o del suo tratto di
    # civici, non e' tra quelli della presa. Anche per prese senza coordinate
    # valide, e trova quelle con la coordinata sbagliata (Daniele, 25/09/2026).
    if not strade.empty:
        diversa = np.array([
            m == "" and dv != "" and dv not in att.split(" / ")
            for m, dv, att in zip(p["MOTIVO"], p["DISTRETTO_VIA"], p["DISTRETTO"])
        ])
        # Presa dentro il distretto della via o a meno di TOLLERANZA_BORDO_M
        # dal suo confine: e' il confine, non un errore (a Belgioioso Via
        # Trieste corre lungo il confine DBLG02/DBLG03).
        coord = p["COORD_VALIDE"].to_numpy()
        for dv in set(p["DISTRETTO_VIA"][diversa & coord]):
            j = np.nonzero(diversa & coord & (p["DISTRETTO_VIA"] == dv).to_numpy())[0]
            dentro, dist = _dentro_e_distanza(p["LAT"].to_numpy(dtype=float)[j], p["LON"].to_numpy(dtype=float)[j], dv)
            diversa[j[dentro | (dist <= TOLLERANZA_BORDO_M)]] = False
        p.loc[diversa, "MOTIVO"] = "VIA"

    # Distretto fuso: associazione automatica al distretto nuovo, salvo se
    # la presa cade chiaramente (oltre TOLLERANZA_BORDO_M) dentro il confine
    # di un altro distretto: allora resta da confermare a mano.
    p["AUTOMATICO"] = ""
    fusi = np.nonzero((p["MOTIVO"] == "FUSO").to_numpy())[0]
    if len(fusi):
        nuovi = np.array([
            next(DISTRETTI_FUSI[d] for d in att.upper().split(" / ") if d in DISTRETTI_FUSI)
            for att in p["DISTRETTO"].to_numpy()[fusi]
        ], dtype=object)
        ok = np.ones(len(fusi), dtype=bool)
        coord = p["COORD_VALIDE"].to_numpy()[fusi]
        if coord.any():
            c = np.nonzero(coord)[0]
            lat = p["LAT"].to_numpy(dtype=float)[fusi][c]
            lon = p["LON"].to_numpy(dtype=float)[fusi][c]
            pos = np.array(_dentro_confini(lat, lon), dtype=object)
            for nuovo in set(nuovi[c]):
                j = np.nonzero((nuovi[c] == nuovo) & (pos != "") & (pos != nuovo))[0]
                if len(j):
                    _, dist = _dentro_e_distanza(lat[j], lon[j], nuovo)
                    ok[c[j[dist > TOLLERANZA_BORDO_M]]] = False
        p.loc[p.index[fusi[ok]], "AUTOMATICO"] = nuovi[ok]
    return p


_CACHE_OSM_UNICO: dict = {}


def _distretto_osm_unico(comune: str, vie_neta: list[str]) -> dict[str, str]:
    """{via Neta: distretto} per le vie il cui tracciato OSM sta per almeno
    il 95% in un solo distretto (in memoria finche' i file non cambiano)."""
    chiave = (comune, tuple(sorted(set(vie_neta) - {""})),
              *(p.stat().st_mtime_ns if p.exists() else 0 for p in (vie_osm.PERCORSO_VIE_OSM, motore_calcolo.PERCORSO_CONFINI_DISTRETTI)))
    if chiave not in _CACHE_OSM_UNICO:
        esito = {}
        for via, (_, quote) in _quote_osm(comune, list(chiave[1])).items():
            if quote and quote[0][0] != "fuori" and quote[0][1] >= 0.95:
                esito[via] = quote[0][0]
        _CACHE_OSM_UNICO[chiave] = esito
    return _CACHE_OSM_UNICO[chiave]


def prese_da_assegnare(comune: str, con_proposta: bool = True) -> pd.DataFrame:
    """Le prese con un motivo (NODMA/ND/ALTRO/POSIZIONE/FUSO) piu' quelle gia' confermate
    in passato (anche se nel frattempo Neta le ha corrette: cosi' si vede
    cosa e' stato recepito), con proposta e conferma."""
    p = prese_comune(comune)
    if p.empty:
        return p
    with database.connessione() as conn:
        a = assegnazioni(conn, comune)
    p = p.merge(
        a[["DP", "DISTRETTO", "UTENTE", "QUANDO", "VALIDATA", "ORIGINE", "NOTA"]].rename(columns={
            "DP": "CHIAVE", "DISTRETTO": "CONFERMATO", "UTENTE": "CONFERMATO_DA", "QUANDO": "CONFERMATO_IL",
        }),
        on="CHIAVE", how="left",
    )
    auto = p["CONFERMATO"].isna() & (p["AUTOMATICO"] != "")
    p.loc[auto, "CONFERMATO"] = p.loc[auto, "AUTOMATICO"]
    p.loc[auto, "CONFERMATO_DA"] = "automatico (distretto fuso)"
    p["ORIGINE"] = p["ORIGINE"].fillna("")
    p["NOTA"] = p["NOTA"].fillna("")
    p = p[(p["MOTIVO"] != "") | p["CONFERMATO"].notna()].reset_index(drop=True)
    lat = p["LAT"].where(p["COORD_VALIDE"]).to_numpy(dtype=float)
    lon = p["LON"].where(p["COORD_VALIDE"]).to_numpy(dtype=float)
    proposte = proponi_distretti(lat, lon) if con_proposta else [("", None)] * len(p)
    # Prima l'indirizzo (civico ANNCSU, stradario, via OSM), poi la posizione
    # (Daniele, 25/09/2026). PROPOSTA_DA = la fonte della proposta; FONTI =
    # tutte le fonti disponibili con il loro distretto, ✓ se concordano con
    # la proposta; CONCORDI = almeno due fonti e tutte d'accordo (le sole
    # proposte confermabili in blocco).
    p["PROPOSTA"] = [dv or c for dv, (c, _) in zip(p["DISTRETTO_VIA"], proposte)]
    p["DISTANZA_M"] = [None if dv else d for dv, (_, d) in zip(p["DISTRETTO_VIA"], proposte)]
    p["PROPOSTA_DA"] = [f or ("posizione" if c else "") for f, (c, _) in zip(p["FONTE_INDIRIZZO"], proposte)]
    fonti, concordi = [], []
    for r, (c, d) in zip(p[["PROPOSTA", "D_CIVICO", "D_STRADARIO", "D_OSM"]].to_dict("records"), proposte):
        elenco = [("civico ANNCSU", r["D_CIVICO"]), ("stradario", r["D_STRADARIO"]), ("via OSM", r["D_OSM"]),
                  ("posizione", c if d is None else "")]
        elenco = [(n, x) for n, x in elenco if x]
        fonti.append(" · ".join(f"{n} {x} {'✓' if x == r['PROPOSTA'] else '✗'}" for n, x in elenco))
        concordi.append(bool(r["PROPOSTA"]) and len(elenco) >= 2 and all(x == r["PROPOSTA"] for _, x in elenco))
    p["FONTI"] = fonti
    p["CONCORDI"] = concordi
    # Recepito: Neta ha gia' messo sulla presa il distretto confermato.
    p["RECEPITO"] = p["CONFERMATO"].notna() & (p["DISTRETTO"] == p["CONFERMATO"])
    # Validata ("mantieni attuale") e ancora con lo stesso distretto in Neta.
    p["VALIDATA"] = (pd.to_numeric(p["VALIDATA"], errors="coerce").fillna(0).astype(int) == 1) & p["RECEPITO"]
    inviate = invii_per_presa(comune, "distretti")
    p["N_INVII"] = [inviate.get(k, (0, None))[0] for k in p["CHIAVE"]]
    p["ULTIMO_INVIO"] = [inviate.get(k, (0, None))[1] for k in p["CHIAVE"]]
    return p


_CACHE_RIEPILOGO: dict = {"versione": None, "righe": {}}
_LOCK_RIEPILOGO = threading.Lock()


def _versione_dati() -> tuple:
    """Cambia quando cambia qualcosa che vale per tutti i comuni: anagrafica
    (caricamento di un'estrazione) e file di riferimento (confini, elenco
    distretti, stradario, comuni ISTAT, ANNCSU, vie OSM). Le conferme no:
    hanno una versione per comune (_versione_conferme), cosi' una conferma
    non fa ricalcolare tutti i comuni ne' il file coordinate (Daniele,
    26/09/2026: "quando confermo quando rifai i calcoli?")."""
    with database.connessione() as conn:
        assicura_tabelle(conn)
        dati = (
            tuple(conn.execute("SELECT COUNT(*), MAX(AGGIORNATO_IL), MAX(DATA_ESTRAZIONE) FROM anagrafica_servizi").fetchone()),
        )
    file = tuple(
        (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
        for p in (motore_calcolo.PERCORSO_CONFINI_DISTRETTI, motore_calcolo.PERCORSO_MAPPA_DISTRETTI,
                  stradario.PERCORSO_STRADARIO, PERCORSO_CONFINI_COMUNI, anncsu.PERCORSO_ANNCSU, vie_osm.PERCORSO_VIE_OSM)
    )
    return dati + file


def _versione_conferme(comune: str) -> tuple:
    """Cambia quando cambiano le conferme (o gli invii) di UN comune."""
    with database.connessione() as conn:
        assicura_tabelle(conn)
        return tuple(conn.execute(
            "SELECT COUNT(*), MAX(QUANDO), TOTAL(LENGTH(DISTRETTO || DP) + VALIDATA) FROM prese_assegnazioni WHERE LOCALITA = ?",
            (comune.strip().upper(),)).fetchone()) + tuple(conn.execute(
            "SELECT COUNT(*) FROM invii_neta_prese WHERE LOCALITA = ?", (comune.strip().upper(),)).fetchone())


def riepilogo_comuni(comuni: list[str]) -> list[dict]:
    """Conteggi per la pagina Prese senza comune scelto, tenuti in memoria
    e ricalcolati solo quando cambiano i dati (Daniele, 25/09/2026: prima
    ~4 s a ogni apertura, ~10 s con le coordinate). Una conferma fa
    ricalcolare solo il suo comune."""
    with _LOCK_RIEPILOGO:
        versione = _versione_dati()
        if _CACHE_RIEPILOGO["versione"] != versione:
            _CACHE_RIEPILOGO.update(versione=versione, righe={})
        righe = _CACHE_RIEPILOGO["righe"]
        conferme = {c: _versione_conferme(c) for c in comuni}
        mancanti = [c for c in comuni if c not in righe or righe[c][0] != conferme[c]]
        for comune in mancanti:
            chiave = (comune, versione, conferme[comune])
            riga = cache_disco.carica(f"riepilogo_prese_{comune}", chiave)
            if riga is None:
                riga = _calcola_riepilogo([comune])[0]
                cache_disco.salva(f"riepilogo_prese_{comune}", chiave, riga)
            righe[comune] = (conferme[comune], riga)
        return [righe[c][1] for c in comuni]


def aggiorna_riepilogo_in_background(comuni: list[str]) -> None:
    """Dopo un caricamento (o all'avvio): prepara il riepilogo, cosi' la
    pagina si apre subito."""
    threading.Thread(target=lambda: riepilogo_comuni(comuni), daemon=True).start()


def _calcola_riepilogo(comuni: list[str]) -> list[dict]:
    righe = []
    for comune in comuni:
        p = prese_da_assegnare(comune, con_proposta=False)
        if p.empty:
            righe.append({"comune": comune, "NODMA": 0, "ND": 0, "ALTRO": 0, "POSIZIONE": 0, "FUSO": 0, "VIA": 0,
                          "confermate": 0, "recepite": 0, "validate": 0, "coordinate": 0})
            continue
        aperte = p[(p["MOTIVO"] != "") & ~p["VALIDATA"]]
        righe.append({
            "comune": comune,
            "NODMA": int((aperte["MOTIVO"] == "NODMA").sum()),
            "ND": int((aperte["MOTIVO"] == "ND").sum()),
            "ALTRO": int((aperte["MOTIVO"] == "ALTRO").sum()),
            "POSIZIONE": int((aperte["MOTIVO"] == "POSIZIONE").sum()),
            "FUSO": int((aperte["MOTIVO"] == "FUSO").sum()),
            "VIA": int((aperte["MOTIVO"] == "VIA").sum()),
            "confermate": int((p["CONFERMATO"].notna() & ~p["VALIDATA"]).sum()),
            "recepite": int((p["RECEPITO"] & ~p["VALIDATA"]).sum()),
            "validate": int(p["VALIDATA"].sum()),
            "coordinate": len(_aperte_in_memoria("coordinate", comune)),
        })
    return righe


# ---------------------------------------------------------------------------
# Conferme ed esportazione
# ---------------------------------------------------------------------------


def _quote_osm(comune: str, vie_neta: list[str]) -> dict[str, tuple[str, list[tuple[str, float]]]]:
    """{via Neta: (nome OSM, [(distretto, quota della lunghezza)])}, in
    ordine di quota; 'fuori' = fuori da ogni confine."""
    osm = vie_osm.vie_comune(comune)
    if not osm:
        return {}
    esito = {}
    for via, nome in vie_osm.abbina(vie_neta, list(osm)).items():
        punti = []
        for tr in osm[nome]:
            for (x1, y1), (x2, y2) in zip(tr[:-1], tr[1:]):
                lung = math.hypot((x2 - x1) * 111_320 * math.cos(math.radians(y1)), (y2 - y1) * 110_540)
                n = max(1, int(lung // 15))
                punti += [(y1 + (y2 - y1) * k / n, x1 + (x2 - x1) * k / n) for k in range(n)]
        if not punti:
            continue
        arr = np.asarray(punti)
        dove = [d or "fuori" for d in _dentro_confini(arr[:, 0], arr[:, 1])]
        esito[via] = (nome, [(d, c / len(dove)) for d, c in collections.Counter(dove).most_common()])
    return esito


def distretti_osm_per_via(comune: str, vie_neta: list[str]) -> dict[str, tuple[str, str]]:
    """{via Neta: (nome OSM, 'DBLG02 100%' o 'DBLG02 70%, DBLG03 30%')}: i
    distretti che il tracciato OSM della via attraversa, in proporzione alla
    lunghezza (punti ogni ~15 m). Indipendente dalle coordinate di Neta:
    serve a controllare lo stradario."""
    return {
        via: (nome, ", ".join(f"{d} {round(100 * q)}%" for d, q in quote if q >= 0.03))
        for via, (nome, quote) in _quote_osm(comune, vie_neta).items()
    }


def genera_stradario(comune: str) -> pd.DataFrame:
    """Genera (una tantum) lo stradario del comune e lo salva, sostituendo
    quello che c'era per il comune. Dove ANNCSU ha i civici posizionati
    votano i civici veri, altrimenti le prese (vedi sotto); la fonte e'
    nella colonna note."""
    p = prese_comune(comune)
    if p.empty:
        raise ValueError(f"Nessun punto di erogazione per il comune '{comune}'.")
    # Votano solo le prese chiaramente dentro un distretto: a meno di
    # TOLLERANZA_BORDO_M dal confine la posizione non e' una prova (a
    # Belgioioso palazzi sul confine diventavano eccezioni della via).
    pos = np.array([""] * len(p), dtype=object)
    validi = np.nonzero(p["COORD_VALIDE"].to_numpy())[0]
    lat = p["LAT"].to_numpy(dtype=float)[validi]
    lon = p["LON"].to_numpy(dtype=float)[validi]
    dentro = np.array(_dentro_confini(lat, lon), dtype=object)
    for codice in set(dentro) - {""}:
        j = np.nonzero(dentro == codice)[0]
        _, profondita = _dentro_e_distanza(lat[j], lon[j], codice)
        pos[validi[j[profondita > TOLLERANZA_BORDO_M]]] = codice
    # Le prese con la coordinata lontana dal resto della via o segnaposto
    # non votano.
    escluse = set(coordinate_sbagliate_per_via(p, comune)) | set(coordinate_condivise(p))
    pos[[i for i, k in enumerate(p["CHIAVE"]) if k in escluse]] = ""

    # Vie con i civici ANNCSU posizionati: votano i civici veri, non le prese
    # (Daniele, 25/09/2026). Le altre vie restano alle prese.
    vie_prese = [stradario.normalizza_indirizzo(i)[0] for i in p["INDIRIZZO"]]
    civici = anncsu.civici_con_coordinate(comune)
    da_anncsu = set()
    righe_civici = pd.DataFrame(columns=["INDIRIZZO"])
    pos_civici: list[str] = []
    if not civici.empty:
        abbinate = anncsu.abbinamento_vie(comune, sorted(set(vie_prese) - {""}))
        per_odonimo = {o: v for v, o in abbinate.items()}
        c = civici[civici["ODONIMO"].isin(per_odonimo)]
        if not c.empty:
            da_anncsu = set(per_odonimo[o] for o in c["ODONIMO"])
            righe_civici = pd.DataFrame({"INDIRIZZO": [f"{per_odonimo[o]}, {n}" for o, n in zip(c["ODONIMO"], c["CIVICO"])]})
            pos_civici = _distretti_sicuri(c["LAT"].to_numpy(dtype=float), c["LON"].to_numpy(dtype=float))
            pos[[i for i, v in enumerate(vie_prese) if v in da_anncsu]] = ""
    tutte = pd.concat([p[["INDIRIZZO"]], righe_civici], ignore_index=True)
    nuovo = stradario.genera_stradario(tutte, list(pos) + pos_civici, comune)
    fonte = ["civici ANNCSU" if v in da_anncsu else "punti di erogazione Neta" for v in nuovo["via"]]
    nuovo["note"] = [f"{n}; da {f}" if n else f"da {f}" for n, f in zip(nuovo["note"], fonte)]
    stradario.salva(comune, nuovo)
    return nuovo


# ---------------------------------------------------------------------------
# Distretti noti, riassegnazioni per il calcolo, conferme
# ---------------------------------------------------------------------------

def distretti_noti() -> set[str]:
    codici = set(motore_calcolo.carica_mappa_distretti_df()["codice_distretto"].str.upper())
    return codici | {c.upper() for c, _, _ in _poligoni()}


def riassegnazioni_calcolo(comune: str) -> dict[str, str]:
    """{codice servizio: distretto} per il calcolo dei volumi: le conferme
    del tab Prese (non le "mantieni attuale", che non cambiano niente)
    applicate a TUTTI i servizi che sono stati sulla presa, anche cessati,
    e a tutta la loro storia (Daniele, 26/09/2026: la conferma entra nel
    calcolo subito, senza aspettare Neta)."""
    comune = comune.strip().upper()
    with database.connessione() as conn:
        a = assegnazioni(conn, comune)
        a = a[(a["VALIDATA"] == 0) & (a["DISTRETTO"] != "")]
        if a.empty:
            return {}
        s = pd.read_sql("SELECT CODICE_SERVIZIO, DP FROM anagrafica_servizi WHERE LOCALITA = ?", conn, params=(comune,))
    s["CHIAVE"] = np.where(s["DP"].isin([DP_SEGNAPOSTO, ""]) | s["DP"].isna(), "S" + s["CODICE_SERVIZIO"], s["DP"])
    distretto = dict(zip(a["DP"], a["DISTRETTO"]))
    return {c: distretto[k] for c, k in zip(s["CODICE_SERVIZIO"], s["CHIAVE"]) if k in distretto}


def conferme_comune(comune: str) -> dict[str, dict]:
    """{chiave presa: {"DISTRETTO", "ORIGINE", "VALIDATA", "NOTA"}} delle
    conferme salvate del comune (per la vista Mappa prese)."""
    with database.connessione() as conn:
        a = assegnazioni(conn, comune)
    return {r["DP"]: {"DISTRETTO": r["DISTRETTO"], "ORIGINE": r["ORIGINE"], "VALIDATA": int(r["VALIDATA"]), "NOTA": r["NOTA"]}
            for r in a.to_dict("records")}


ORIGINI = {"proposta": "proposta dell'app", "manuale": "spostato a mano", "zona": "spostato con una zona",
           "mantieni": "mantieni attuale", "": ""}


def salva_assegnazioni(comune: str, voci: list[dict], utente: str) -> tuple[int, int]:
    """voci = [{"chiave", "distretto", "validata"?, "origine"?, "nota"?}];
    distretto vuoto = togli la conferma. validata = "mantieni attuale": il
    distretto e' quello che la presa ha gia' (anche NO DISTRETTO), accettato
    anche se non e' nell'elenco distretti. origine: vedi ORIGINI.
    Restituisce (salvate, tolte). Codici sconosciuti rifiutati."""
    noti = distretti_noti()
    comune = comune.strip().upper()
    adesso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    salvate = tolte = 0
    with database.connessione() as conn:
        assicura_tabelle(conn)
        for voce in voci:
            chiave = voce["chiave"]
            distretto = (voce.get("distretto") or "").strip().upper()
            validata = bool(voce.get("validata"))
            origine = "mantieni" if validata else (voce.get("origine") or "proposta")
            if origine not in ORIGINI:
                raise ValueError(f"Origine '{origine}' sconosciuta.")
            nota = (voce.get("nota") or "").strip()[:500]
            if not distretto:
                tolte += conn.execute(
                    "DELETE FROM prese_assegnazioni WHERE LOCALITA=? AND DP=?", (comune, chiave)
                ).rowcount
                continue
            if not validata and distretto not in noti:
                raise ValueError(f"Distretto '{distretto}' non presente nell'elenco distretti né nei confini.")
            conn.execute(
                "INSERT INTO prese_assegnazioni (LOCALITA, DP, DISTRETTO, UTENTE, QUANDO, VALIDATA, ORIGINE, NOTA) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(LOCALITA, DP) DO UPDATE SET DISTRETTO=excluded.DISTRETTO, UTENTE=excluded.UTENTE, "
                "QUANDO=excluded.QUANDO, VALIDATA=excluded.VALIDATA, ORIGINE=excluded.ORIGINE, NOTA=excluded.NOTA",
                (comune, chiave, distretto, utente, adesso, int(validata), origine, nota),
            )
            salvate += 1
        conn.commit()
    return salvate, tolte


# Funzioni e costanti spostate nei moduli prese_geo, prese_coordinate e prese_neta (27/09/2026):
# restano raggiungibili come prese.<nome>, quindi il resto dell'app non cambia.
from app.prese_geo import (  # noqa: E402
    DISTANZA_MAX_PROPOSTA_M,
    TOLLERANZA_BORDO_M,
    _CACHE_CONFINI,
    _poligoni,
    _dentro_anello,
    _distanza_anello_m,
    _dentro_confini,
    PERCORSO_CONFINI_COMUNI,
    TOLLERANZA_CONFINE_COMUNE_M,
    _CACHE_COMUNI,
    _nome_comune,
    _poligoni_comuni,
    _comune_della_posizione,
    _distanza_dal_comune_m,
    _dentro_e_distanza,
    proponi_distretti,
    geojson_attorno,
    _distretti_sicuri,
)
from app.prese_coordinate import (  # noqa: E402
    DISTANZA_FUORI_COMUNE_M,
    DISTANZA_FUORI_COMUNE_NODMA_M,
    DISTANZA_FUORI_VIA_M,
    VICINI_PER_LATO,
    FATTORE_DISPERSIONE_VIA,
    MIN_VIE_COORDINATA_CONDIVISA,
    DISTANZA_MAX_DA_VIA_OSM_M,
    DISTANZA_MAX_DA_CIVICO_M,
    DISTANZA_PROPOSTA_DA_VIA_OSM_M,
    DISTANZA_RIFERIMENTO_DA_VIA_M,
    GAP_INTERPOLAZIONE_MEDIA_M,
    GAP_INTERPOLAZIONE_MAX_M,
    fuori_dalla_via,
    distanze_da_via_osm,
    coordinate_sbagliate_per_via,
    interpolazione_lungo_via,
    _affidabilita_proposte,
    coordinate_condivise,
    distretti_del_comune,
    _scala,
    _coordinata_non_valida,
    _CACHE_COORDINATE,
    _LOCK_COORDINATE,
    coordinate_da_verificare,
    _coordinate_da_verificare,
    esporta_coordinate_excel,
    LEGENDA_COORDINATE,
    _excel,
)
from app.prese_neta import (  # noqa: E402
    invii_per_presa,
    _da_inviare,
    registra_invio,
    elenco_invii,
    _CACHE_APERTE,
    _aperte_in_memoria,
    COLONNE_NETA_DISTRETTI,
    esporta_excel,
)
