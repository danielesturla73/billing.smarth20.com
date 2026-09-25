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
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app import database, motore_calcolo

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
    ALTRO / POSIZIONE / FUSO / '' se a posto), LAT, LON, COORD_VALIDE,
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
    da_controllare = np.nonzero(((p["MOTIVO"] == "") & p["COORD_VALIDE"]).to_numpy())[0]
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
        a[["DP", "DISTRETTO", "UTENTE", "QUANDO"]].rename(columns={
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
    p["PROPOSTA"] = [c for c, _ in proposte]
    p["DISTANZA_M"] = [d for _, d in proposte]
    # Recepito: Neta ha gia' messo sulla presa il distretto confermato.
    p["RECEPITO"] = p["CONFERMATO"].notna() & (p["DISTRETTO"] == p["CONFERMATO"])
    return p


def riepilogo_comuni(comuni: list[str]) -> list[dict]:
    """Conteggi per la pagina Prese senza comune scelto."""
    righe = []
    for comune in comuni:
        p = prese_da_assegnare(comune, con_proposta=False)
        if p.empty:
            righe.append({"comune": comune, "NODMA": 0, "ND": 0, "ALTRO": 0, "POSIZIONE": 0, "FUSO": 0,
                          "confermate": 0, "recepite": 0})
            continue
        aperte = p[p["MOTIVO"] != ""]
        righe.append({
            "comune": comune,
            "NODMA": int((aperte["MOTIVO"] == "NODMA").sum()),
            "ND": int((aperte["MOTIVO"] == "ND").sum()),
            "ALTRO": int((aperte["MOTIVO"] == "ALTRO").sum()),
            "POSIZIONE": int((aperte["MOTIVO"] == "POSIZIONE").sum()),
            "FUSO": int((aperte["MOTIVO"] == "FUSO").sum()),
            "confermate": int(p["CONFERMATO"].notna().sum()),
            "recepite": int(p["RECEPITO"].sum()),
        })
    return righe


# ---------------------------------------------------------------------------
# Conferme ed esportazione
# ---------------------------------------------------------------------------

def distretti_noti() -> set[str]:
    codici = set(motore_calcolo.carica_mappa_distretti_df()["codice_distretto"].str.upper())
    return codici | {c.upper() for c, _, _ in _poligoni()}


def salva_assegnazioni(comune: str, voci: list[tuple[str, str]], utente: str) -> tuple[int, int]:
    """voci = [(chiave presa, distretto)]; distretto vuoto = togli la
    conferma. Restituisce (salvate, tolte). Codici sconosciuti rifiutati."""
    noti = distretti_noti()
    comune = comune.strip().upper()
    adesso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    salvate = tolte = 0
    with database.connessione() as conn:
        assicura_tabelle(conn)
        for chiave, distretto in voci:
            distretto = (distretto or "").strip().upper()
            if not distretto:
                tolte += conn.execute(
                    "DELETE FROM prese_assegnazioni WHERE LOCALITA=? AND DP=?", (comune, chiave)
                ).rowcount
                continue
            if distretto not in noti:
                raise ValueError(f"Distretto '{distretto}' non presente nell'elenco distretti né nei confini.")
            conn.execute(
                "INSERT INTO prese_assegnazioni VALUES (?,?,?,?,?) "
                "ON CONFLICT(LOCALITA, DP) DO UPDATE SET DISTRETTO=excluded.DISTRETTO, "
                "UTENTE=excluded.UTENTE, QUANDO=excluded.QUANDO",
                (comune, chiave, distretto, utente, adesso),
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
            p = p[p["CONFERMATO"].notna()]
        if p.empty:
            continue
        p = p.assign(COMUNE=comune)
        parti.append(p)
    colonne = {
        "COMUNE": "Comune", "DP": "Presa (DP)", "INDIRIZZO": "Indirizzo", "CAP": "CAP",
        "SERVIZI": "Codici servizio", "N_SERVIZI": "N. servizi",
        "DISTRETTO": "Distretto attuale", "MOTIVO_TESTO": "Motivo",
        "CONFERMATO": "Distretto da assegnare", "STATO": "Stato",
        "PROPOSTA": "Distretto proposto (posizione)", "PROPOSTA_COME": "Come",
        "CONFERMATO_DA": "Confermato da", "CONFERMATO_IL": "Confermato il",
        "LAT": "Latitudine", "LON": "Longitudine",
    }
    if parti:
        df = pd.concat(parti, ignore_index=True)
        df["DP"] = np.where(df["DP"] == "", "(servizio senza presa)", df["DP"])
        df["MOTIVO_TESTO"] = df["MOTIVO"].map(MOTIVI).fillna("")
        df["STATO"] = np.where(
            df["RECEPITO"], "Già recepito da Neta",
            np.where(df["CONFERMATO"].notna(), "Da recepire", "Da verificare"),
        )
        df["PROPOSTA_COME"] = [
            "" if not prop else ("dentro il confine" if pd.isna(d) else f"fuori, a {int(d)} m")
            for prop, d in zip(df["PROPOSTA"], df["DISTANZA_M"])
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
