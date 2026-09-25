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
"""
from __future__ import annotations

import collections
import io
import json
import math
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app import database, motore_calcolo, stradario, vie_osm

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

# Fuori da ogni confine si propone il distretto piu' vicino solo entro questa
# distanza: oltre, la proposta sarebbe un tiro a indovinare.
DISTANZA_MAX_PROPOSTA_M = 300

# Controllo "Diverso dalla posizione": una presa a meno di questi metri dal
# confine del proprio distretto non si segnala, lo scarto puo' essere solo
# l'errore della coordinata (Daniele, 25/09/2026).
TOLLERANZA_BORDO_M = 30

# Coordinate da verificare (file separato per Neta): una presa a piu' di
# tanto dal distretto piu' vicino del suo comune e' "fuori comune".
DISTANZA_FUORI_COMUNE_M = 1000
# Le case sparse (NO DISTRETTO) stanno spesso in campagna a 1-2 km dai
# distretti (mediana 1,6 km a settembre 2026): per loro solo oltre 5 km.
DISTANZA_FUORI_COMUNE_NODMA_M = 5000

# Coordinata lontana dal resto della via: un civico a piu' di tanto dal
# punto mediano dei civici vicini per numero (fino a 3 sotto e 3 sopra)
# della stessa via ha la coordinata sbagliata. A Belgioioso Via Molino 24
# (8 prese con la stessa coordinata), 26 e 40 stavano a ~1 km dal resto
# della via, dentro DBLG03, e lo stradario ne faceva un'eccezione (Daniele,
# 25/09/2026: Via Molino e' tutta DBLG02).
DISTANZA_FUORI_VIA_M = 400
VICINI_PER_LATO = 3
# Sulle strade di campagna i civici vicini per numero sono lontani anche nella
# realta': la soglia cresce con la dispersione dei vicini (tante volte la
# loro distanza mediana dal loro centro).
FATTORE_DISPERSIONE_VIA = 3
# Coordinata "segnaposto": lo stesso punto (al metro) usato per prese di
# almeno tante vie diverse (a Voghera un punto per 16 prese di 14 vie).
MIN_VIE_COORDINATA_CONDIVISA = 3

# Distanza massima di una presa dal tracciato OSM della sua via (le case
# possono stare arretrate dalla strada): a Belgioioso mediana 14 m, 90% entro
# 69 m, oltre 150 m 141 prese, tutte coordinate sbagliate a campione.
DISTANZA_MAX_DA_VIA_OSM_M = 150

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
# Confini distretti e proposta
# ---------------------------------------------------------------------------

_CACHE_CONFINI: dict = {"versione": None, "poligoni": []}


def _poligoni():
    """[(codice, [anelli come array Nx2 lon/lat], bbox)] dal GeoJSON dei
    confini, riletto solo se il file cambia."""
    percorso = motore_calcolo.PERCORSO_CONFINI_DISTRETTI
    if not percorso.exists():
        return []
    st = percorso.stat()
    versione = (st.st_mtime_ns, st.st_size)
    if _CACHE_CONFINI["versione"] != versione:
        dati = json.loads(percorso.read_text(encoding="utf-8"))
        poligoni = []
        for f in dati.get("features", []):
            codice = (f.get("properties") or {}).get("codice_distretto", "")
            g = f.get("geometry") or {}
            parti = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []
            for parte in parti:
                anelli = [np.asarray(a, dtype=float)[:, :2] for a in parte if len(a) >= 3]
                if anelli:
                    esterno = anelli[0]
                    bbox = (esterno[:, 0].min(), esterno[:, 1].min(), esterno[:, 0].max(), esterno[:, 1].max())
                    poligoni.append((codice, anelli, bbox))
        _CACHE_CONFINI.update(versione=versione, poligoni=poligoni)
    return _CACHE_CONFINI["poligoni"]


def _dentro_anello(x: np.ndarray, y: np.ndarray, anello: np.ndarray) -> np.ndarray:
    """Ray casting vettoriale: quali punti (x, y) cadono dentro l'anello."""
    x1, y1 = anello[:, 0][None, :], anello[:, 1][None, :]
    x2, y2 = np.roll(anello[:, 0], -1)[None, :], np.roll(anello[:, 1], -1)[None, :]
    dy = np.where(y2 == y1, 1e-300, y2 - y1)
    dentro = np.zeros(len(x), dtype=bool)
    for i in range(0, len(x), 500):  # a blocchi: matrice punti x lati
        px, py = x[i:i + 500, None], y[i:i + 500, None]
        attraversa = (y1 > py) != (y2 > py)
        xi = x1 + (py - y1) * (x2 - x1) / dy
        dentro[i:i + 500] = (np.count_nonzero(attraversa & (px < xi), axis=1) % 2) == 1
    return dentro


def _distanza_anello_m(x: np.ndarray, y: np.ndarray, anello: np.ndarray, lat0: float) -> np.ndarray:
    """Distanza minima (metri, proiezione locale) di ogni punto dal bordo."""
    kx, ky = 111_320 * math.cos(math.radians(lat0)), 110_540
    px, py = x[:, None] * kx, y[:, None] * ky
    ax, ay = anello[:-1, 0][None, :] * kx, anello[:-1, 1][None, :] * ky
    bx, by = anello[1:, 0][None, :] * kx, anello[1:, 1][None, :] * ky
    dx, dy = bx - ax, by - ay
    lung2 = dx * dx + dy * dy
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / np.where(lung2 == 0, 1, lung2), 0, 1)
    return np.sqrt((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2).min(axis=1)


def _dentro_confini(lat: np.ndarray, lon: np.ndarray, trovato: np.ndarray | None = None) -> list[str]:
    """Per ogni punto il codice del distretto il cui confine lo contiene
    ('' se nessuno o punto non valido). trovato, se passato, viene
    aggiornato: True dove il punto cade dentro un confine."""
    n = len(lat)
    codici = [""] * n
    if trovato is None:
        trovato = np.zeros(n, dtype=bool)
    validi = ~(np.isnan(lat) | np.isnan(lon))
    for codice, anelli, (x0, y0, x1, y1) in _poligoni():
        candidati = validi & ~trovato & (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1)
        idx = np.nonzero(candidati)[0]
        if not len(idx):
            continue
        dentro = _dentro_anello(lon[idx], lat[idx], anelli[0])
        for buco in anelli[1:]:
            dentro &= ~_dentro_anello(lon[idx], lat[idx], buco)
        for i in idx[dentro]:
            codici[i] = codice
        trovato[idx[dentro]] = True
    return codici


# Confini dei comuni ISTAT (unita' amministrative a fini statistici al
# 01/01/2025, generalizzati), convertiti una volta in GeoJSON lon/lat per i
# comuni attorno alla provincia di Pavia (Daniele, 25/09/2026): dicono se la
# coordinata di una presa sta davvero nel suo comune.
PERCORSO_CONFINI_COMUNI = Path("project_docs/comuni_confini.geojson")
# I confini generalizzati hanno un errore di qualche decina di metri.
TOLLERANZA_CONFINE_COMUNE_M = 100
_CACHE_COMUNI: dict = {"versione": None, "poligoni": []}


def _nome_comune(nome: str) -> str:
    """'Gambolò' e "GAMBOLO'" -> 'GAMBOLO' (senza accenti, apostrofi, spazi)."""
    testo = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode()
    return "".join(c for c in testo.upper() if c.isalnum())


def _poligoni_comuni():
    """[(nome normalizzato, "Nome (SIGLA)", anelli, bbox)], riletti solo se
    il file cambia; [] se il file non c'e'."""
    if not PERCORSO_CONFINI_COMUNI.exists():
        return []
    st = PERCORSO_CONFINI_COMUNI.stat()
    versione = (st.st_mtime_ns, st.st_size)
    if _CACHE_COMUNI["versione"] != versione:
        dati = json.loads(PERCORSO_CONFINI_COMUNI.read_text(encoding="utf-8"))
        poligoni = []
        for f in dati.get("features", []):
            nome = f["properties"]["comune"]
            sigla = f["properties"].get("provincia", "")
            etichetta = f"{nome} ({sigla})" if sigla else nome
            for parte in f["geometry"]["coordinates"]:
                anelli = [np.asarray(a, dtype=float)[:, :2] for a in parte if len(a) >= 3]
                if anelli:
                    e = anelli[0]
                    poligoni.append((_nome_comune(nome), etichetta, anelli, (e[:, 0].min(), e[:, 1].min(), e[:, 0].max(), e[:, 1].max())))
        _CACHE_COMUNI.update(versione=versione, poligoni=poligoni)
    return _CACHE_COMUNI["poligoni"]


def _comune_della_posizione(lat: np.ndarray, lon: np.ndarray) -> list[str]:
    """"Nome (SIGLA)" del comune in cui cade ogni punto ('' se nessuno)."""
    esito = [""] * len(lat)
    libero = np.ones(len(lat), dtype=bool)
    for _, nome, anelli, (x0, y0, x1, y1) in _poligoni_comuni():
        idx = np.nonzero(libero & (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1))[0]
        if not len(idx):
            continue
        dentro = _dentro_anello(lon[idx], lat[idx], anelli[0])
        for buco in anelli[1:]:
            dentro &= ~_dentro_anello(lon[idx], lat[idx], buco)
        for i in idx[dentro]:
            esito[i] = nome
        libero[idx[dentro]] = False
    return esito


def _distanza_dal_comune_m(lat: np.ndarray, lon: np.ndarray, comune: str) -> np.ndarray | None:
    """Distanza di ogni punto dal confine del comune (None se il comune non
    e' nel file ISTAT)."""
    chiave = _nome_comune(comune)
    parti = [anelli for n, _, anelli, _ in _poligoni_comuni() if n == chiave]
    if not parti:
        return None
    distanza = np.full(len(lat), np.inf)
    lat0 = float(np.mean(lat)) if len(lat) else 45.0
    for anelli in parti:
        for anello in anelli:
            for i in range(0, len(lat), 500):
                distanza[i:i + 500] = np.minimum(
                    distanza[i:i + 500], _distanza_anello_m(lon[i:i + 500], lat[i:i + 500], anello, lat0))
    return distanza


def _dentro_e_distanza(lat: np.ndarray, lon: np.ndarray, codice: str) -> tuple[np.ndarray, np.ndarray]:
    """Per ogni punto: se cade dentro un confine del distretto codice, e la
    distanza in metri dal bordo piu' vicino di quel distretto (dentro o
    fuori). Distretto senza confine: (False, inf)."""
    n = len(lat)
    dentro = np.zeros(n, dtype=bool)
    distanza = np.full(n, np.inf)
    if n == 0:
        return dentro, distanza
    lat0 = float(np.mean(lat))
    for c, anelli, _ in _poligoni():
        if c != codice:
            continue
        d = _dentro_anello(lon, lat, anelli[0])
        for buco in anelli[1:]:
            d &= ~_dentro_anello(lon, lat, buco)
        dentro |= d
        for anello in anelli:
            for i in range(0, n, 500):  # a blocchi: matrice punti x lati
                distanza[i:i + 500] = np.minimum(
                    distanza[i:i + 500], _distanza_anello_m(lon[i:i + 500], lat[i:i + 500], anello, lat0))
    return dentro, distanza


def proponi_distretti(lat: np.ndarray, lon: np.ndarray) -> list[tuple[str, int | None]]:
    """Per ogni punto: (codice, None) se cade dentro un confine, (codice,
    distanza_m) se il piu' vicino e' entro DISTANZA_MAX_PROPOSTA_M, ("", None)
    altrimenti. I punti non validi (NaN) restano senza proposta."""
    n = len(lat)
    esito: list[tuple[str, int | None]] = [("", None)] * n
    if n == 0:
        return esito
    poligoni = _poligoni()
    validi = ~(np.isnan(lat) | np.isnan(lon))
    trovato = np.zeros(n, dtype=bool)
    for i, codice in enumerate(_dentro_confini(lat, lon, trovato)):
        if codice:
            esito[i] = (codice, None)

    fuori = np.nonzero(validi & ~trovato)[0]
    if len(fuori):
        lat0 = float(np.nanmean(lat[fuori]))
        margine_lat = DISTANZA_MAX_PROPOSTA_M / 110_540
        margine_lon = DISTANZA_MAX_PROPOSTA_M / (111_320 * math.cos(math.radians(lat0)))
        migliore = np.full(len(fuori), np.inf)
        codice_migliore = [""] * len(fuori)
        for codice, anelli, (x0, y0, x1, y1) in poligoni:
            vicini = (
                (lon[fuori] >= x0 - margine_lon) & (lon[fuori] <= x1 + margine_lon)
                & (lat[fuori] >= y0 - margine_lat) & (lat[fuori] <= y1 + margine_lat)
            )
            j = np.nonzero(vicini)[0]
            if not len(j):
                continue
            d = _distanza_anello_m(lon[fuori][j], lat[fuori][j], anelli[0], lat0)
            meglio = d < migliore[j]
            migliore[j[meglio]] = d[meglio]
            for k in j[meglio]:
                codice_migliore[k] = codice
        for k, i in enumerate(fuori):
            if migliore[k] <= DISTANZA_MAX_PROPOSTA_M:
                esito[i] = (codice_migliore[k], int(round(migliore[k])))
    return esito


def geojson_attorno(lat_min, lat_max, lon_min, lon_max, margine=0.01) -> dict:
    """Confini dei distretti che toccano il rettangolo delle prese (piu' un
    margine), con nome e comune dall'elenco distretti — per la mappa."""
    percorso = motore_calcolo.PERCORSO_CONFINI_DISTRETTI
    if not percorso.exists():
        return {"type": "FeatureCollection", "features": []}
    dati = json.loads(percorso.read_text(encoding="utf-8"))
    elenco = motore_calcolo.carica_mappa_distretti_df().set_index("codice_distretto")
    tenuti = []
    for f in dati.get("features", []):
        g = f.get("geometry") or {}
        coords = g.get("coordinates", [])
        punti = [p for anello in (coords if g.get("type") == "Polygon" else [a for parte in coords for a in parte]) for p in anello]
        if not punti:
            continue
        xs, ys = [p[0] for p in punti], [p[1] for p in punti]
        if max(xs) < lon_min - margine or min(xs) > lon_max + margine or max(ys) < lat_min - margine or min(ys) > lat_max + margine:
            continue
        codice = f["properties"].get("codice_distretto", "")
        f["properties"] = {
            "codice_distretto": codice,
            "nome_distretto": elenco["nome_distretto"].get(codice) or codice,
            "comune": elenco["comune_ufficiale"].get(codice, ""),
        }
        tenuti.append(f)
    return {"type": "FeatureCollection", "features": tenuti}


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


def prese_comune(comune: str) -> pd.DataFrame:
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
    p["DISTRETTO_VIA"] = ""
    strade = stradario.carica(comune)
    if not strade.empty:
        p["DISTRETTO_VIA"] = stradario.distretti_da_via(strade, p["INDIRIZZO"])

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
        a[["DP", "DISTRETTO", "UTENTE", "QUANDO", "VALIDATA"]].rename(columns={
            "DP": "CHIAVE", "DISTRETTO": "CONFERMATO", "UTENTE": "CONFERMATO_DA", "QUANDO": "CONFERMATO_IL",
        }),
        on="CHIAVE", how="left",
    )
    auto = p["CONFERMATO"].isna() & (p["AUTOMATICO"] != "")
    p.loc[auto, "CONFERMATO"] = p.loc[auto, "AUTOMATICO"]
    p.loc[auto, "CONFERMATO_DA"] = "automatico (distretto fuso)"
    p = p[(p["MOTIVO"] != "") | p["CONFERMATO"].notna()].reset_index(drop=True)
    lat = p["LAT"].where(p["COORD_VALIDE"]).to_numpy(dtype=float)
    lon = p["LON"].where(p["COORD_VALIDE"]).to_numpy(dtype=float)
    proposte = proponi_distretti(lat, lon) if con_proposta else [("", None)] * len(p)
    # Prima l'indirizzo (stradario), poi la posizione (Daniele, 25/09/2026).
    # PROPOSTA_DA: 'via e posizione' (d'accordo), 'via', 'posizione'.
    p["PROPOSTA"] = [dv or c for dv, (c, _) in zip(p["DISTRETTO_VIA"], proposte)]
    p["DISTANZA_M"] = [None if dv else d for dv, (_, d) in zip(p["DISTRETTO_VIA"], proposte)]
    p["PROPOSTA_DA"] = [
        ("via e posizione" if dv == c else "via") if dv else ("posizione" if c else "")
        for dv, (c, _) in zip(p["DISTRETTO_VIA"], proposte)
    ]
    # Recepito: Neta ha gia' messo sulla presa il distretto confermato.
    p["RECEPITO"] = p["CONFERMATO"].notna() & (p["DISTRETTO"] == p["CONFERMATO"])
    # Validata ("mantieni attuale") e ancora con lo stesso distretto in Neta.
    p["VALIDATA"] = (p["VALIDATA"].fillna(0).astype(int) == 1) & p["RECEPITO"]
    inviate = invii_per_presa(comune, "distretti")
    p["N_INVII"] = [inviate.get(k, (0, None))[0] for k in p["CHIAVE"]]
    p["ULTIMO_INVIO"] = [inviate.get(k, (0, None))[1] for k in p["CHIAVE"]]
    return p


_CACHE_RIEPILOGO: dict = {"versione": None, "righe": {}}
_LOCK_RIEPILOGO = threading.Lock()


def _versione_dati() -> tuple:
    """Cambia quando cambia qualcosa che entra nel riepilogo: anagrafica
    (caricamento di un'estrazione), conferme, file di
    riferimento (confini, elenco distretti, stradario, comuni ISTAT)."""
    with database.connessione() as conn:
        assicura_tabelle(conn)
        dati = (
            tuple(conn.execute("SELECT COUNT(*), MAX(AGGIORNATO_IL), MAX(DATA_ESTRAZIONE) FROM anagrafica_servizi").fetchone()),
            tuple(conn.execute("SELECT COUNT(*), MAX(QUANDO), TOTAL(LENGTH(DISTRETTO || DP) + VALIDATA) FROM prese_assegnazioni").fetchone()),
        )
    file = tuple(
        (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
        for p in (motore_calcolo.PERCORSO_CONFINI_DISTRETTI, motore_calcolo.PERCORSO_MAPPA_DISTRETTI,
                  stradario.PERCORSO_STRADARIO, PERCORSO_CONFINI_COMUNI)
    )
    return dati + file


def riepilogo_comuni(comuni: list[str]) -> list[dict]:
    """Conteggi per la pagina Prese senza comune scelto, tenuti in memoria
    e ricalcolati solo quando cambiano i dati (Daniele, 25/09/2026: prima
    ~4 s a ogni apertura, ~10 s con le coordinate)."""
    with _LOCK_RIEPILOGO:
        versione = _versione_dati()
        if _CACHE_RIEPILOGO["versione"] != versione:
            _CACHE_RIEPILOGO.update(versione=versione, righe={})
        righe = _CACHE_RIEPILOGO["righe"]
        mancanti = [c for c in comuni if c not in righe]
        for comune, riga in zip(mancanti, _calcola_riepilogo(mancanti)):
            righe[comune] = riga
        return [righe[c] for c in comuni]


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

def fuori_dalla_via(p: pd.DataFrame) -> dict[str, tuple[int, float, float]]:
    """{chiave presa: (distanza m, lat, lon del punto mediano dei civici
    vicini)} per le prese la cui coordinata sta lontana dal resto della sua
    via: oltre DISTANZA_FUORI_VIA_M, o oltre FATTORE_DISPERSIONE_VIA volte
    la dispersione dei civici vicini se e' maggiore. La distanza e' della
    singola presa (al civico 24 di Via Molino 8 prese sbagliate e 1 giusta).
    Il punto di ogni civico vicino e' la mediana delle sue prese."""
    righe = []
    for chiave, indirizzo, lat, lon, ok in zip(p["CHIAVE"], p["INDIRIZZO"], p["LAT"], p["LON"], p["COORD_VALIDE"]):
        via, civico = stradario.normalizza_indirizzo(indirizzo)
        if ok and via and civico is not None:
            righe.append((chiave, via, civico, float(lat), float(lon)))
    if not righe:
        return {}
    df = pd.DataFrame(righe, columns=["CHIAVE", "VIA", "CIVICO", "LAT", "LON"])
    kx = 111_320 * math.cos(math.radians(float(df["LAT"].median())))

    def metri(lat1, lon1, lat2, lon2):
        return np.hypot((np.asarray(lat1) - lat2) * 110_540, (np.asarray(lon1) - lon2) * kx)

    esito = {}
    df = df.sort_values(["VIA", "CIVICO"], kind="stable")
    punti = df.groupby(["VIA", "CIVICO"], sort=True)[["LAT", "LON"]].median()
    for via, g_punti in punti.groupby(level=0, sort=False):
        if len(g_punti) < 4:
            continue
        civici = g_punti.index.get_level_values(1).to_numpy()
        plat = g_punti["LAT"].to_numpy()
        plon = g_punti["LON"].to_numpy()
        prese_via = df[df["VIA"] == via]
        per_civico = {c: (grp["CHIAVE"].to_numpy(), grp["LAT"].to_numpy(), grp["LON"].to_numpy())
                      for c, grp in prese_via.groupby("CIVICO", sort=False)}
        n = len(civici)
        for k in range(n):
            idx = [x for x in range(max(0, k - VICINI_PER_LATO), min(n, k + 1 + VICINI_PER_LATO)) if x != k]
            if len(idx) < 3:
                continue
            c_lat = float(np.median(plat[idx]))
            c_lon = float(np.median(plon[idx]))
            dispersione = float(np.median(metri(plat[idx], plon[idx], c_lat, c_lon)))
            soglia = max(DISTANZA_FUORI_VIA_M, FATTORE_DISPERSIONE_VIA * dispersione)
            chiavi, la, lo = per_civico[civici[k]]
            for chiave, d in zip(chiavi, metri(la, lo, c_lat, c_lon)):
                if d > soglia:
                    esito[chiave] = (int(round(d)), round(c_lat, 6), round(c_lon, 6))
    return esito


def distanze_da_via_osm(p: pd.DataFrame, comune: str) -> tuple[dict[str, float], set[str]]:
    """({chiave presa: distanza m dal tracciato OSM della sua via}, vie Neta
    abbinate a OSM). Solo prese con coordinate valide e via abbinata; se il
    file OSM manca o il comune non c'e', ({}, set())."""
    osm = vie_osm.vie_comune(comune)
    if not osm:
        return {}, set()
    v = p[p["COORD_VALIDE"]]
    vie = pd.Series([stradario.normalizza_indirizzo(i)[0] for i in v["INDIRIZZO"]], index=v.index)
    abbinate = vie_osm.abbina(sorted(set(vie) - {""}), list(osm))
    esito = {}
    for via, nome in abbinate.items():
        m = (vie == via).to_numpy()
        d = vie_osm.distanza_m(v["LAT"].to_numpy(dtype=float)[m], v["LON"].to_numpy(dtype=float)[m], osm[nome])
        esito.update(zip(v["CHIAVE"].to_numpy()[m], d))
    return esito, set(abbinate)


def coordinate_sbagliate_per_via(p: pd.DataFrame, comune: str) -> dict[str, tuple[str, int | None, float | None, float | None]]:
    """{chiave: (motivo, distanza, lat proposta, lon proposta)} per le prese
    con la coordinata lontana dalla sua via. Dove la via e' abbinata a OSM
    decide OSM (oltre DISTANZA_MAX_DA_VIA_OSM_M); altrimenti il confronto con
    i civici vicini (fuori_dalla_via). La coordinata proposta e' il punto
    mediano dei civici vicini, se c'e'."""
    d_osm, abbinate = distanze_da_via_osm(p, comune)
    vicini = fuori_dalla_via(p)
    esito = {}
    for chiave, indirizzo in zip(p["CHIAVE"], p["INDIRIZZO"]):
        via = stradario.normalizza_indirizzo(indirizzo)[0]
        prop = vicini.get(chiave, (None, None, None))[1:]
        if via in abbinate:
            d = d_osm.get(chiave)
            if d is not None and d > DISTANZA_MAX_DA_VIA_OSM_M:
                esito[chiave] = ("Lontana dalla sua via (OpenStreetMap)", int(round(d)), *prop)
        elif chiave in vicini:
            esito[chiave] = ("Lontana dal resto della via", vicini[chiave][0], *prop)
    return esito


def distretti_osm_per_via(comune: str, vie_neta: list[str]) -> dict[str, tuple[str, str]]:
    """{via Neta: (nome OSM, 'DBLG02 100%' o 'DBLG02 70%, DBLG03 30%')}: i
    distretti che il tracciato OSM della via attraversa, in proporzione alla
    lunghezza (punti ogni ~15 m). Indipendente dalle coordinate di Neta:
    serve a controllare lo stradario."""
    osm = vie_osm.vie_comune(comune)
    if not osm:
        return {}
    abbinate = vie_osm.abbina(vie_neta, list(osm))
    esito = {}
    for via, nome in abbinate.items():
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
        conta = collections.Counter(dove).most_common()
        esito[via] = (nome, ", ".join(f"{d} {round(100 * c / len(dove))}%" for d, c in conta if 100 * c / len(dove) >= 3))
    return esito


def coordinate_condivise(p: pd.DataFrame) -> dict[str, int]:
    """{chiave presa: numero di vie} per le prese la cui coordinata (al
    metro) e' usata anche da prese di altre vie: una coordinata segnaposto,
    non la posizione vera (vedi MIN_VIE_COORDINATA_CONDIVISA)."""
    v = p[p["COORD_VALIDE"]]
    if v.empty:
        return {}
    vie = [stradario.normalizza_indirizzo(i)[0] for i in v["INDIRIZZO"]]
    punto = list(zip(v["LAT"].round(5), v["LON"].round(5)))
    n_vie = pd.Series(vie).groupby(pd.Series(punto)).nunique()
    return {k: int(n_vie[pt]) for k, pt in zip(v["CHIAVE"], punto) if n_vie[pt] >= MIN_VIE_COORDINATA_CONDIVISA}


def genera_stradario(comune: str) -> pd.DataFrame:
    """Genera (una tantum) lo stradario del comune dalla posizione delle
    sue prese e lo salva, sostituendo quello che c'era per il comune."""
    p = prese_comune(comune)
    if p.empty:
        raise ValueError(f"Nessuna presa per il comune '{comune}'.")
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
    pos = list(pos)
    nuovo = stradario.genera_stradario(p, pos, comune)
    stradario.salva(comune, nuovo)
    return nuovo


def distretti_del_comune(comune: str) -> set[str]:
    """Codici dei distretti del comune (ufficiali o associabili)."""
    comune = comune.strip().upper()
    elenco = motore_calcolo.carica_mappa_distretti_df()
    return {
        r.codice_distretto.upper() for r in elenco.itertuples(index=False)
        if r.comune_ufficiale.strip().upper() == comune
        or comune in [c.strip().upper() for c in str(r.comuni_associabili).split(";")]
    }


def _scala(valore: float, cifre_intere: int) -> float:
    """8742620 -> 8.742620 (cifre_intere=1), 4531012 -> 45.31012 (2): la
    virgola persa nell'estrazione."""
    intere = len(str(int(abs(valore))))
    return valore / 10 ** (intere - cifre_intere)


def _coordinata_non_valida(lat, lon, comune: str) -> tuple[str, tuple[float, float] | None]:
    """Descrive una coordinata fuori dall'area valida e, se e' un errore di
    formato riconoscibile (virgola persa, latitudine e longitudine
    invertite), propone quella corretta dicendo dove cadrebbe (Daniele,
    25/09/2026: a settembre 2026 824 mancanti, 9 senza virgola, 1 invertita,
    le altre in altre regioni)."""
    if pd.isna(lat) or pd.isna(lon):
        return "Coordinate mancanti", None
    lat, lon = float(lat), float(lon)
    if lat == 0 and lon == 0:
        return "Coordinate a 0,0", None
    candidati = []
    if abs(lat) > 1000 or abs(lon) > 1000:
        candidati.append(("Virgola mancante", (_scala(lat, 2) if abs(lat) > 1000 else lat, _scala(lon, 1) if abs(lon) > 1000 else lon)))
    candidati.append(("Latitudine e longitudine invertite", (lon, lat)))
    for motivo, (la, lo) in candidati:
        if LAT_VALIDA[0] <= la <= LAT_VALIDA[1] and LON_VALIDA[0] <= lo <= LON_VALIDA[1]:
            dove = _comune_della_posizione(np.array([la]), np.array([lo]))[0]
            if _nome_comune(dove.split(" (")[0]) == _nome_comune(comune):
                return f"{motivo}: corretta cadrebbe nel comune", (round(la, 6), round(lo, 6))
            return f"{motivo}: corretta cadrebbe in {dove or 'un comune lontano'}", (round(la, 6), round(lo, 6))
    return f"Coordinate fuori provincia ({lat:.4f}, {lon:.4f})", None


def coordinate_da_verificare(comune: str) -> pd.DataFrame:
    """Prese del comune con coordinate da far verificare a Neta (Daniele,
    25/09/2026), ricalcolate a ogni estrazione indipendentemente dalle
    conferme del distretto: una presa resta qui finche' Neta non corregge
    la coordinata. PROBLEMA: mancanti o fuori provincia; dentro un distretto
    di un altro comune; a piu' di DISTANZA_FUORI_COMUNE_M dai distretti del
    comune; coordinata segnaposto (stesso punto per piu' vie); lontana dal
    resto della sua via (vedi fuori_dalla_via, con la coordinata mediana dei
    civici vicini come proposta). Se ci sono i confini
    ISTAT del comune, "fuori comune" viene da quelli e sostituisce le due
    regole sui distretti (dentro un distretto di altro comune, lontana dai
    distretti del comune). La regola "lontana dal distretto della sua
    via" e' stata tolta: sulle strade lunghe (Via Emilia, Via Piacenza,
    frazioni) segnalava le case sparse lungo la stessa via (~970 falsi
    allarmi con lo stradario di tutti i comuni)."""
    p = prese_comune(comune)
    if p.empty:
        return p
    problema = pd.Series("", index=p.index)
    distanza = pd.Series(np.nan, index=p.index)
    lat_c = pd.Series(np.nan, index=p.index)
    lon_c = pd.Series(np.nan, index=p.index)
    for i in np.nonzero(~p["COORD_VALIDE"].to_numpy())[0]:
        testo, corretta = _coordinata_non_valida(p["LAT"].iloc[i], p["LON"].iloc[i], comune)
        problema.iloc[i] = testo
        if corretta:
            lat_c.iloc[i], lon_c.iloc[i] = corretta

    # Con i confini ISTAT: fuori dal territorio comunale (oltre la
    # tolleranza dei confini generalizzati) al posto della distanza dai
    # distretti, che segnalava anche case sparse legittime in campagna.
    v_tutti = np.nonzero(p["COORD_VALIDE"].to_numpy())[0]
    istat = None
    if len(v_tutti):
        lat_v = p["LAT"].to_numpy(dtype=float)[v_tutti]
        lon_v = p["LON"].to_numpy(dtype=float)[v_tutti]
        dist_comune = _distanza_dal_comune_m(lat_v, lon_v, comune)
        if dist_comune is not None:
            istat = True
            dove = _comune_della_posizione(lat_v, lon_v)
            for k, i in enumerate(v_tutti):
                if _nome_comune(dove[k].split(" (")[0]) != _nome_comune(comune) and dist_comune[k] > TOLLERANZA_CONFINE_COMUNE_M:
                    problema.iloc[i] = f"Fuori dal comune: cade in {dove[k] or 'un comune lontano'}"
                    distanza.iloc[i] = round(dist_comune[k])

    condivise = coordinate_condivise(p)
    fuori = coordinate_sbagliate_per_via(p, comune)
    for i, chiave in enumerate(p["CHIAVE"]):
        if problema.iloc[i]:
            continue
        if chiave in condivise:
            problema.iloc[i] = f"Coordinata segnaposto: stesso punto per prese di {condivise[chiave]} vie diverse"
        elif chiave in fuori:
            motivo, d, la, lo = fuori[chiave]
            problema.iloc[i] = motivo
            distanza.iloc[i] = d
            if la is not None:
                lat_c.iloc[i], lon_c.iloc[i] = la, lo

    # Coordinata in un distretto diverso da quello della sua via, oltre la
    # tolleranza dal confine: con "prima l'indirizzo" il distretto non si
    # segnala, ma la coordinata va corretta.
    dv_col = p["DISTRETTO_VIA"].to_numpy()
    j = np.nonzero((problema == "").to_numpy() & p["COORD_VALIDE"].to_numpy() & (dv_col != ""))[0]
    if len(j):
        la = p["LAT"].to_numpy(dtype=float)[j]
        lo = p["LON"].to_numpy(dtype=float)[j]
        dove = np.array(_dentro_confini(la, lo), dtype=object)
        for dv in set(dv_col[j]):
            k = np.nonzero((dv_col[j] == dv) & (dove != "") & (dove != dv))[0]
            if not len(k):
                continue
            dentro, dist = _dentro_e_distanza(la[k], lo[k], dv)
            for kk, d_in, d in zip(k, dentro, dist):
                if not d_in and d > TOLLERANZA_BORDO_M:
                    problema.iloc[j[kk]] = f"Cade in {dove[kk]}, ma la via e' {dv}"
                    distanza.iloc[j[kk]] = round(d)

    propri = distretti_del_comune(comune) & {c for c, _, _ in _poligoni()}
    elenco = motore_calcolo.carica_mappa_distretti_df().set_index("codice_distretto")["comune_ufficiale"]
    v = np.nonzero(p["COORD_VALIDE"].to_numpy())[0]
    if len(v) and propri:
        lat = p["LAT"].to_numpy(dtype=float)[v]
        lon = p["LON"].to_numpy(dtype=float)[v]
        pos = np.array(_dentro_confini(lat, lon), dtype=object)
        minima = np.full(len(v), np.inf)
        for codice in propri:
            dentro, dist = _dentro_e_distanza(lat, lon, codice)
            minima = np.minimum(minima, np.where(dentro, 0, dist))
        for k, i in enumerate(v):
            if istat:
                break  # fuori comune gia' deciso dai confini ISTAT
            if pos[k] and pos[k] not in propri:
                problema.iloc[i] = f"Dentro un distretto di un altro comune ({pos[k]}, {elenco.get(pos[k], '') or '?'})"
                distanza.iloc[i] = round(minima[k])
            elif minima[k] > (DISTANZA_FUORI_COMUNE_NODMA_M if p["MOTIVO"].iloc[i] == "NODMA" else DISTANZA_FUORI_COMUNE_M):
                problema.iloc[i] = "Lontana dai distretti del comune"
                distanza.iloc[i] = round(minima[k])

    p = p.assign(PROBLEMA=problema, DISTANZA_M=distanza, LAT_CORRETTA=lat_c, LON_CORRETTA=lon_c)
    return p[p["PROBLEMA"] != ""].reset_index(drop=True)


def esporta_coordinate_excel(comuni: list[str]) -> bytes:
    """File per Neta con le prese da ricontrollare sul posto / in mappa."""
    parti = [c.assign(COMUNE=comune) for comune in comuni if not (c := coordinate_da_verificare(comune)).empty]
    colonne = {
        "COMUNE": "Comune", "DP": "Presa (DP)", "INDIRIZZO": "Indirizzo", "CAP": "CAP",
        "SERVIZI": "Codici servizio", "N_SERVIZI": "N. servizi", "DISTRETTO": "Distretto attuale",
        "PROBLEMA": "Problema", "DISTANZA_M": "Distanza (m)", "LAT": "Latitudine", "LON": "Longitudine",
        "LAT_CORRETTA": "Latitudine corretta (proposta)", "LON_CORRETTA": "Longitudine corretta (proposta)",
    }
    if parti:
        df = pd.concat(parti, ignore_index=True)
        df["DP"] = np.where(df["DP"] == "", "(servizio senza presa)", df["DP"])
        df = df[list(colonne)].rename(columns=colonne)
    else:
        df = pd.DataFrame(columns=list(colonne.values()))
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Coordinate")
        foglio = writer.sheets["Coordinate"]
        for i, col in enumerate(df.columns, start=1):
            larghezza = min(60, max(10, len(col) + 2, *(len(str(v)) + 2 for v in df[col].head(500))))
            foglio.column_dimensions[foglio.cell(row=1, column=i).column_letter].width = larghezza
        foglio.freeze_panes = "A2"
        foglio.auto_filter.ref = foglio.dimensions
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Registro degli invii a Neta
# ---------------------------------------------------------------------------

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
        colonne = {**comuni_col, "VALORE": "Distretto da assegnare", "INVIATA_PRIMA": "Gia' inviata",
                   "LAT": "Latitudine", "LON": "Longitudine"}
    else:
        colonne = {**comuni_col, "VALORE": "Problema", "DISTANZA_M": "Distanza (m)", "INVIATA_PRIMA": "Gia' inviata",
                   "LAT": "Latitudine", "LON": "Longitudine",
                   "LAT_CORRETTA": "Latitudine corretta (proposta)", "LON_CORRETTA": "Longitudine corretta (proposta)"}
    return id_invio, len(df), _excel(df[list(colonne)].rename(columns=colonne), "Distretti" if tipo == "distretti" else "Coordinate")


def _excel(df: pd.DataFrame, foglio_nome: str) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=foglio_nome)
        foglio = writer.sheets[foglio_nome]
        for i, col in enumerate(df.columns, start=1):
            larghezza = min(60, max(10, len(col) + 2, *(len(str(v)) + 2 for v in df[col].head(500))))
            foglio.column_dimensions[foglio.cell(row=1, column=i).column_letter].width = larghezza
        foglio.freeze_panes = "A2"
        foglio.auto_filter.ref = foglio.dimensions
    return buffer.getvalue()


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
    i dati non cambiano (le coordinate di tutti i comuni costano secondi)."""
    versione = _versione_dati()
    if _CACHE_APERTE["versione"] != versione:
        _CACHE_APERTE.update(versione=versione, dati={})
    chiave = (tipo, comune)
    if chiave not in _CACHE_APERTE["dati"]:
        p = _da_inviare(tipo, comune)
        _CACHE_APERTE["dati"][chiave] = [] if p.empty else list(p["CHIAVE"])
    return _CACHE_APERTE["dati"][chiave]


def distretti_noti() -> set[str]:
    codici = set(motore_calcolo.carica_mappa_distretti_df()["codice_distretto"].str.upper())
    return codici | {c.upper() for c, _, _ in _poligoni()}


def salva_assegnazioni(comune: str, voci: list[tuple], utente: str) -> tuple[int, int]:
    """voci = [(chiave presa, distretto)] o [(chiave, distretto, validata)];
    distretto vuoto = togli la conferma. validata = "mantieni attuale": il
    distretto e' quello che la presa ha gia' (anche NO DISTRETTO), accettato
    anche se non e' nell'elenco distretti. Restituisce (salvate, tolte).
    Codici sconosciuti rifiutati."""
    noti = distretti_noti()
    comune = comune.strip().upper()
    adesso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    salvate = tolte = 0
    with database.connessione() as conn:
        assicura_tabelle(conn)
        for voce in voci:
            chiave, distretto = voce[0], voce[1]
            validata = bool(voce[2]) if len(voce) > 2 else False
            distretto = (distretto or "").strip().upper()
            if not distretto:
                tolte += conn.execute(
                    "DELETE FROM prese_assegnazioni WHERE LOCALITA=? AND DP=?", (comune, chiave)
                ).rowcount
                continue
            if not validata and distretto not in noti:
                raise ValueError(f"Distretto '{distretto}' non presente nell'elenco distretti né nei confini.")
            conn.execute(
                "INSERT INTO prese_assegnazioni (LOCALITA, DP, DISTRETTO, UTENTE, QUANDO, VALIDATA) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(LOCALITA, DP) DO UPDATE SET DISTRETTO=excluded.DISTRETTO, "
                "UTENTE=excluded.UTENTE, QUANDO=excluded.QUANDO, VALIDATA=excluded.VALIDATA",
                (comune, chiave, distretto, utente, adesso, int(validata)),
            )
            salvate += 1
        conn.commit()
    return salvate, tolte


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
        "CONFERMATO": "Distretto da assegnare", "STATO": "Stato",
        "PROPOSTA": "Distretto proposto", "PROPOSTA_DA": "Proposto da", "PROPOSTA_COME": "Posizione",
        "DISTRETTO_VIA": "Distretto della via (stradario)",
        "CONFERMATO_DA": "Confermato da", "CONFERMATO_IL": "Confermato il",
        "LAT": "Latitudine", "LON": "Longitudine",
    }
    if parti:
        df = pd.concat(parti, ignore_index=True)
        df["DP"] = np.where(df["DP"] == "", "(servizio senza presa)", df["DP"])
        df["MOTIVO_TESTO"] = df["MOTIVO"].map(MOTIVI).fillna("")
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
        df = df[list(colonne)].rename(columns=colonne)
    else:
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
