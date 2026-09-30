"""
Punto d'ingresso del servizio web di Analisi Consumi da Fatturazione.

Stato al 17/09/2026: oltre al health-check ci sono un endpoint di upload
delle estrazioni Neta H2O (/upload), endpoint di riepilogo/diagnostica in
JSON (/riepilogo, /diagnostica) e le PRIME PAGINE VERE (/pagine/riepilogo,
/pagine/diagnostica — stesso motore, stessa scelta di comune, ma rese in
HTML con lo stile di WMS SmartH2O invece che JSON grezzo). L'archivio
storico vive in SQLite (archivio/archivio.db, vedi app/database.py)
invece che in CSV per comune.

Cosa NON c'e' ancora, e va costruito (pensato per Claude Code):
- i grafici (proposta in specifiche 6.2, non ancora confermata da Daniele
  — non costruirli senza conferma esplicita)
- il pulsante "a un click" che invia i dati a WMS SmartH2O (vedi 4.9:
  per ora e' un invio manuale/con conferma umana, non uno scheduler
  automatico)
- il token interno verso WMS SmartH2O (INTERNAL_API_TOKEN in .env.example);
  l'autenticazione di chi usa l'interfaccia c'e' dal 19/09/2026 (accessi.py,
  auth.py: login, ruoli viewer/editor/admin, registro azioni)
- upload/gestione utenti dalle pagine web (oggi solo via /upload, API)

Il motore di calcolo vero e proprio resta in motore_calcolo.py (Metodo
B, statistiche per distretto, ecc.) — questo file lo importa ma non ne
cambia la logica. La persistenza (SQLite) e' in database.py.
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
from pathlib import Path

from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware
from fastapi.templating import Jinja2Templates

import pandas as pd

from app import accessi, anncsu, auth, cache_disco, consolidamento, database, invio_wms, motore_calcolo, prese, prese_confronto, stradario, vie_osm
from app import prese_gis as prese_gis_modulo
from app import prese_vie as prese_vie_modulo

app = FastAPI(
    title="Analisi Consumi da Fatturazione",
    description="Consumi (da fatturazione) per distretto idrico, per il bilancio idrico e la riduzione delle perdite in WMS SmartH2O, a partire dalle estrazioni Neta H2O.",
    version="0.1.0",
)
# Pagine compresse (Daniele, 26/09/2026): la pagina Prese di Voghera pesava
# 1,5 MB e la Mappa 0,9 MB; compresse circa un decimo.
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
# Cache-busting per lo stylesheet: senza questo, il browser puo' tenere in
# cache una versione vecchia di style.css dopo un deploy (stesso URL
# /static/style.css ad ogni rebuild) e le modifiche CSS non si vedono senza
# un refresh forzato. La versione e' la data di modifica del file, letta una
# volta all'avvio del container.
def _numero_it(valore, decimali: int = 0) -> str:
    """1234.5 -> '1.234,50' (separatori all'italiana, per i template)."""
    testo = f"{valore:,.{decimali}f}"
    return testo.replace(",", "\0").replace(".", ",").replace("\0", ".")


templates.env.filters["it_num"] = _numero_it


def _litri_secondo(volume_m3: float, giorni: int) -> float:
    """Portata media equivalente: m3 -> l/s su `giorni` giorni (m3*1000 / (giorni*86400)).
    Richiesta da Daniele il 21/09/2026 per chi si occupa di conduzione impianti:
    e' la portata continua che darebbe quel volume, non un picco."""
    return volume_m3 * 1000 / (giorni * 86400) if giorni else 0.0


def _giorni_periodo(periodo: str | None) -> int:
    """Giorni di un mese 'YYYY-MM' o di un trimestre 'YYYY-TN'."""
    if not periodo:
        return 0
    if "-T" in periodo:
        anno, trim = periodo.split("-T")
        primo = (int(trim) - 1) * 3 + 1
        return sum(pd.Period(f"{anno}-{m:02d}").days_in_month for m in range(primo, primo + 3))
    return pd.Period(periodo).days_in_month


templates.env.globals["css_version"] = int(
    (Path("app/static/style.css").stat().st_mtime)
)

# Percorso relativo alla working directory del processo ("input/"),
# coerente con quello gia' usato da motore_calcolo.py e con i volumi
# Docker montati in docker-compose.yml.
INPUT_DIR = Path("input")

# Login, ruoli e registro azioni (middleware default-deny): vedi accessi.py.
accessi.registra_accessi(app, templates)


@app.get("/health")
def health_check():
    """Controllo minimo di salute del servizio: risponde subito, senza
    toccare l'archivio o fare calcoli. Usato per verificare che il
    container sia su — sia da fuori (durante lo sviluppo), sia in futuro
    da WMS SmartH2O sulla rete Docker interna, sia da un reverse proxy.
    """
    return {"status": "ok", "servizio": "billing"}


@app.get("/")
def root():
    """L'app si apre sulla mappa dei distretti (richiesto da Daniele il
    19/09/2026): da li' si entra nella pagina del comune con un clic."""
    return RedirectResponse(url="/pagine/mappa")


async def _carica_estrazioni(request: Request, files: list[UploadFile]) -> dict:
    """Cuore del caricamento, condiviso da POST /upload (JSON) e dalla
    pagina /pagine/carica (HTML).

    Per ogni file: lo salva in input/, riconosce il comune dalla colonna
    LOCALITA dentro il file (non dal nome del file — vedi specifiche,
    sezione 6.0) e lo aggiunge all'archivio storico (SQLite) di quel
    comune, creandolo automaticamente se il comune non era mai stato
    visto. Un file con struttura non valida (colonne mancanti, file non
    Excel, ecc.) viene segnalato con un errore SENZA bloccare il
    caricamento degli altri file del batch.

    Non fa nessun ricalcolo immediato dei volumi (lo fa la cache in
    background, vedi _invalida_comune): cosi' non si ripete un calcolo
    pesante ad ogni singolo upload quando si caricano piu' file insieme.
    """
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    risultati_file = []
    file_per_comune: dict[str, list[Path]] = {}

    for upload in files:
        # Solo il nome, senza cartelle: un filename tipo "../../x" non deve
        # poter scrivere fuori da input/.
        nome_originale = Path((upload.filename or "").replace("\\", "/")).name or "estrazione.xlsx"
        contenuto = await upload.read()
        # Non sovrascrivere mai un file gia' presente (es. stesso nome
        # ricaricato per errore, o due file omonimi nello stesso invio): si
        # tiene comunque traccia di ogni caricamento con un suffisso
        # progressivo (_2, _3...), scelto in modo esclusivo ("xb") cosi'
        # neanche due richieste contemporanee possono scrivere sullo stesso
        # file. Il suffisso e' un contatore e non l'orario in secondi: con
        # l'orario, tre file omonimi nello stesso secondo si sovrascrivevano
        # (corretto il 19/09/2026). La deduplica vera e propria delle LETTURE
        # avviene poi in database.aggiorna_letture, su chiave
        # utenza+data+tipo, non sul nome del file.
        radice = Path(nome_originale)
        percorso_salvato = INPUT_DIR / radice.name
        progressivo = 1
        while True:
            try:
                with open(percorso_salvato, "xb") as f:
                    f.write(contenuto)
                break
            except FileExistsError:
                progressivo += 1
                percorso_salvato = INPUT_DIR / f"{radice.stem}_{progressivo}{radice.suffix}"

        try:
            # Il lavoro pesante in un thread: l'app (un solo processo) continua
            # a rispondere agli altri utenti durante il caricamento.
            df = await run_in_threadpool(motore_calcolo.carica_estrazione, percorso_salvato)
        except Exception as exc:
            messaggio = str(exc)
            if "Excel file format cannot be determined" in messaggio or "zip file" in messaggio.lower():
                messaggio = "Non è un file Excel (.xlsx) valido."
            risultati_file.append({
                "file": nome_originale,
                "salvato_come": percorso_salvato.name,
                "comune": None,
                "righe_lette": None,
                "errore": messaggio,
            })
            continue

        comune = motore_calcolo.comune_dominante(df) or "SCONOSCIUTO"
        # Controllo prese (Daniele, 29/09/2026): distretto di Neta contro il
        # nostro, prima che l'anagrafica venga aggiornata con questo file.
        try:
            controllo = await run_in_threadpool(
                prese_confronto.controlla_e_salva, df, comune, percorso_salvato.name, request.state.utente["username"])
        except Exception as exc:  # il controllo non deve mai bloccare il caricamento
            controllo = {"saltato": f"errore nel controllo: {exc}"}
        # Anagrafica delle prese (coordinate/distretto dell'ultima estrazione,
        # vedi app/prese.py): dal file intero, prima della deduplica letture.
        await run_in_threadpool(prese.aggiorna_anagrafica, df, percorso_salvato.name)
        risultati_file.append({
            "file": nome_originale,
            "salvato_come": percorso_salvato.name,
            "comune": comune,
            "righe_lette": len(df),
            "errore": None,
            "controllo_prese": controllo,
        })
        file_per_comune.setdefault(comune, []).append(percorso_salvato)

    archivi_aggiornati = []
    for comune, percorsi in file_per_comune.items():
        _, stats = await run_in_threadpool(database.aggiorna_letture, percorsi, comune)
        _invalida_comune(comune)
        avvisi = []
        if comune == "SCONOSCIUTO":
            avvisi.append("Comune non riconosciuto dalla colonna LOCALITA: controlla il file.")
        if stats["righe_archivio_prima"] == 0:
            avvisi.append(
                "Comune nuovo in archivio: se i suoi distretti non sono nell'elenco ufficiale "
                "(pagina Distretti) la classificazione si basa sull'euristica del prefisso."
            )
        elif stats["righe_nuove_aggiunte_davvero"] == 0:
            avvisi.append("Nessuna riga nuova: le letture erano già tutte in archivio (file già caricato?).")
        archivi_aggiornati.append({"comune": comune, **stats, "avvisi": avvisi})
        auth.registra(
            request.state.utente["username"], "upload_estrazione",
            f"{comune}: {stats['righe_nuove_aggiunte_davvero']} righe nuove, "
            f"{stats['righe_duplicate_scartate']} duplicate scartate — file: {', '.join(p.name for p in percorsi)}",
            accessi.ip_client(request),
        )

    if archivi_aggiornati:
        # Conteggi del tab Prese pronti per la prossima apertura.
        prese.aggiorna_riepilogo_in_background(_comuni_disponibili())

    for r in risultati_file:
        if r["errore"]:
            auth.registra(
                request.state.utente["username"], "upload_errore",
                f"{r['file']}: {r['errore']}", accessi.ip_client(request),
            )

    return {"file": risultati_file, "archivi_aggiornati": archivi_aggiornati}


@app.post("/upload")
async def upload_estrazioni(request: Request, files: list[UploadFile] = File(...)):
    """Versione API (JSON) del caricamento, vedi _carica_estrazioni. Serve
    il cookie di sessione di un editor."""
    return await _carica_estrazioni(request, files)


def _stato_archivio() -> list[dict]:
    """Per ogni comune in archivio: quante letture, quante utenze e da/a
    quale data di lettura, cosi' si vede quale estrazione manca."""
    with database.connessione() as conn:
        if not database.tabella_esiste(conn):
            return []
        righe = conn.execute(
            "SELECT LOCALITA, COUNT(*), COUNT(DISTINCT CODICE_SERVIZIO), MIN(DATA_LETTURA), MAX(DATA_LETTURA) "
            "FROM letture WHERE LOCALITA IS NOT NULL GROUP BY LOCALITA ORDER BY LOCALITA"
        ).fetchall()
    return [
        {"comune": r[0], "letture": r[1], "utenze": r[2], "dal": str(r[3])[:10], "al": str(r[4])[:10]}
        for r in righe
    ]


def _contesto_carica(request: Request, esito: dict | None = None) -> dict:
    return {
        "pagina_attiva": "carica",
        "comuni_disponibili": _comuni_disponibili(),
        "comune_selezionato": None,
        "stato_archivio": _stato_archivio(),
        "ultimi_caricamenti": auth.leggi_registro(azione="upload_estrazione", limite=10),
        "controlli_prese": prese_confronto.elenco_controlli(),
        "stati_prese": prese_confronto.STATI,
        "esito": esito,
    }


@app.get("/pagine/carica")
def pagina_carica(request: Request):
    """Schermata di caricamento delle estrazioni Neta H2O (solo editor/
    admin, vedi il middleware): stessa logica di /upload, ma con l'esito
    per file e per comune in pagina, invece del JSON."""
    return templates.TemplateResponse(request, "carica.html", _contesto_carica(request))


@app.post("/pagine/carica")
async def pagina_carica_invio(request: Request, files: list[UploadFile] = File(...)):
    esito = await _carica_estrazioni(request, files)
    return templates.TemplateResponse(request, "carica.html", _contesto_carica(request, esito))


@app.get("/prese/controllo/{id_controllo}")
def prese_controllo_esporta(request: Request, id_controllo: int):
    """Excel delle prese di un controllo fatto al caricamento (vedi
    app/prese_confronto.py)."""
    try:
        nome, contenuto = prese_confronto.esporta_excel(id_controllo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(
        content=contenuto,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


def _tabella_json(df: pd.DataFrame) -> list[dict]:
    """Converte un DataFrame in una lista di dict pronta per la risposta
    JSON: le colonne 'Mese' (Period) e le date diventano stringhe, i NaN
    diventano null — altrimenti Starlette rifiuta la risposta (il JSON
    standard non ammette NaN, e non sa serializzare un pandas.Period).
    """
    df = df.copy()
    for col in df.columns:
        if isinstance(df[col].dtype, pd.PeriodDtype):
            df[col] = df[col].astype(str)
        elif pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%d")
    df = df.astype(object).where(pd.notnull(df), None)
    return df.to_dict(orient="records")


def _pivot_mese_distretto(volumi_distretto_mese: pd.DataFrame) -> dict:
    """Trasforma Import_WMS (una riga per mese+distretto) in una tabella
    incrociata per la pagina web: una riga per mese, una colonna per
    distretto, totale di riga a destra — più compatta da leggere della
    sequenza mese/distretto/mese/distretto originale. Il JSON di /riepilogo
    resta invariato (vedi _tabella_json), questa è solo una vista per
    riepilogo.html.
    """
    if volumi_distretto_mese.empty:
        return {"distretti": [], "righe": []}

    pivot = volumi_distretto_mese.pivot_table(
        index="Mese", columns="Codice Distretto", values="Volume Fatturato (m3)",
        aggfunc="sum", fill_value=0,
    ).sort_index()

    distretti = [str(d) for d in pivot.columns]
    righe = []
    for mese, valori in pivot.iterrows():
        riga = {"Mese": str(mese)}
        for distretto, valore in zip(distretti, valori):
            riga[distretto] = round(float(valore), 2)
        riga["Totale"] = round(float(valori.sum()), 2)
        righe.append(riga)

    return {"distretti": distretti, "righe": righe}


def _data(valore) -> str:
    """Formatta una data come YYYY-MM-DD, coerente con _tabella_json, per
    le frasi costruite a mano in _anomalie_per_utenza (li' non si passa
    dal DataFrame->JSON che fa gia' questa conversione)."""
    return pd.Timestamp(valore).strftime("%Y-%m-%d") if pd.notnull(valore) else "?"


def _anomalie_per_utenza(risultato, codice_servizio: int) -> list[str]:
    """Raccoglie, per UNA utenza, le segnalazioni che la riguardano in
    tutte e 5 le categorie mostrate da /pagine/diagnostica (non solo
    Anomalie Metodo B), come frasi pronte da mostrare in cima a
    /pagine/utenza. Richiesto da Daniele il 18/09/2026: quando si apre
    un'utenza cliccando da una qualsiasi delle tabelle, si vuole vedere
    subito perche' era segnalata, senza dover tornare indietro a
    ricontrollare.
    """
    frasi = []

    segnalazioni = risultato.segnalazioni[risultato.segnalazioni["Codice Servizio"] == codice_servizio]
    for _, r in segnalazioni.iterrows():
        frasi.append(f"Segnalazione distretto: {r['Motivo']} (distretto riportato: {r['Distretto Riportato']})")

    anomalie_b = risultato.anomalie_metodo_b[risultato.anomalie_metodo_b["CODICE_SERVIZIO"] == codice_servizio]
    for _, r in anomalie_b.iterrows():
        frasi.append(f"Anomalia Metodo B del {_data(r['DATA_LETTURA'])}: {r['MOTIVO']}")

    scomparse = risultato.utenze_scomparse[
        (risultato.utenze_scomparse["Codice Servizio"] == codice_servizio)
        & (risultato.utenze_scomparse["Da Verificare"] == "Sì (era ancora attiva)")
    ]
    for _, r in scomparse.iterrows():
        frasi.append(
            f"Utenza scomparsa: ancora attiva nell'ultima lettura nota ({_data(r['Ultima Data Lettura'])}, "
            f"file {r['Ultimo File in cui Compare']}), ma non compare nel file più recente"
        )

    cessate = risultato.cessate_con_stima_finale[
        risultato.cessate_con_stima_finale["Codice Servizio"] == codice_servizio
    ]
    for _, r in cessate.iterrows():
        frasi.append(
            f"Cessata con stima finale: ultima lettura ({r['Ultima Lettura (tipo)']}) del "
            f"{_data(r['Ultima Data Lettura'])} non è reale, mancava la lettura di chiusura"
        )

    corrette_nodma = risultato.utenze_corrette_da_nodma[
        risultato.utenze_corrette_da_nodma["Codice Servizio"] == codice_servizio
    ]
    for _, r in corrette_nodma.iterrows():
        frasi.append(
            f"Corretta da NODMA/ND a {r['Distretto Attuale']}: {r['Volume Escluso Permanentemente (m3)']:.2f} m³ "
            f"dei mesi {r['Mesi Interessati']} restano esclusi da qualunque distretto per sempre"
        )

    return frasi


# Cache dei risultati del Metodo B per comune (aggiunta il 19/09/2026 dopo
# che le pagine impiegavano 2-6 secondi: ogni richiesta ricalcolava
# elabora_dataframe su tutto l'archivio, ~1,7s Belgioioso, ~3,3s Mortara).
# Invalidazione (concordata con Daniele il 19/09/2026):
# - upload di un'estrazione: solo il comune caricato (_invalida_comune),
#   ricalcolato subito in background; gli altri comuni restano in cache;
# - elenco distretti (importa_mappa_distretti cambia la classificazione di
#   TUTTI i comuni): si controlla la data del file e si svuota tutto.
# Conseguenza accettata: modifiche al database fatte fuori dall'upload
# (scripts/migra_csv_a_sqlite.py, a mano) NON si vedono finche' non si
# riavvia il container. I risultati in cache sono condivisi tra richieste:
# vanno solo LETTI, mai modificati sul posto (le pagine ne ricavano copie,
# es. _tabella_json).
_CACHE_RISULTATI: dict[str, object] = {}
_CACHE_VERSIONE: list = [None]
_LOCK_CACHE = threading.Lock()
_LOCK_PER_COMUNE: dict[str, threading.Lock] = {}


def _chiave_comune(comune: str) -> str:
    return comune.strip().upper()


def _versione_elenco_distretti():
    try:
        st = motore_calcolo.PERCORSO_MAPPA_DISTRETTI.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _risultato_in_cache(conn, comune: str):
    """Lock globale solo per leggere/scrivere il dizionario (istantaneo);
    il calcolo (secondi) e' sotto un lock PER COMUNE, cosi' un ricalcolo in
    corso non blocca chi legge un altro comune gia' in cache."""
    versione = _versione_elenco_distretti()
    chiave = _chiave_comune(comune)
    with _LOCK_CACHE:
        if _CACHE_VERSIONE[0] != versione:
            _CACHE_RISULTATI.clear()
            _CACHE_VERSIONE[0] = versione
        if chiave in _CACHE_RISULTATI:
            return _CACHE_RISULTATI[chiave]
        lock_comune = _LOCK_PER_COMUNE.setdefault(chiave, threading.Lock())
    with lock_comune:
        with _LOCK_CACHE:
            if chiave in _CACHE_RISULTATI:  # calcolato da un'altra richiesta nel frattempo
                return _CACHE_RISULTATI[chiave]
        # Risultato gia' salvato su disco per gli stessi dati e lo stesso
        # codice (vedi cache_disco): dopo un riavvio si ricarica in un attimo.
        chiave_disco = (
            comune, versione,
            tuple(conn.execute("SELECT COUNT(*), MAX(rowid) FROM letture WHERE LOCALITA = ?", (comune,)).fetchone()),
            tuple(conn.execute("SELECT COUNT(*), MAX(AGGIORNATO_IL) FROM anagrafica_servizi WHERE LOCALITA = ?",
                               (comune.strip().upper(),)).fetchone()),
            prese._versione_conferme(comune), consolidamento.versione(comune),
        )
        risultato = cache_disco.carica(f"metodo_b_{comune}", chiave_disco)
        if risultato is None:
            df = database.carica_letture(conn, comune)
            # Conferme del tab Prese nel calcolo, su tutta la storia; poi i mesi
            # degli anni consolidati tornano quelli della copia fissa (Daniele,
            # 26/09/2026).
            risultato = None if df.empty else motore_calcolo.elabora_dataframe(df, prese.riassegnazioni_calcolo(comune))
            if risultato is not None:
                risultato.volumi_distretto_mese_calcolati = risultato.volumi_distretto_mese
                risultato.volumi_distretto_mese = consolidamento.applica(risultato.volumi_distretto_mese, comune)
                cache_disco.salva(f"metodo_b_{comune}", chiave_disco, risultato)
        with _LOCK_CACHE:
            _CACHE_RISULTATI[chiave] = risultato
        return risultato


def _invalida_comune(comune: str) -> None:
    """Toglie dalla cache il comune appena aggiornato da un upload e lo
    ricalcola in background, cosi' chi apre la pagina lo trova pronto."""
    with _LOCK_CACHE:
        _CACHE_RISULTATI.pop(_chiave_comune(comune), None)
    threading.Thread(target=lambda: list(_risultati_per_comune(comune)), daemon=True).start()


# Ricalcolo dopo le conferme del tab Prese, rimandato (Daniele, 26/09/2026:
# chi conferma una presa alla volta non deve far ricalcolare a ogni clic).
# Un timer per comune: ogni conferma lo fa ripartire; quando scade si
# ricalcolano volumi e riepilogo Prese in background. Intanto la cache del
# comune e' gia' vuota, quindi chi apre una pagina vede comunque i numeri
# giusti (ricalcolati in quel momento).
RITARDO_RICALCOLO_S = 120          # conferma di una presa alla volta
RITARDO_RICALCOLO_GRUPPO_S = 5     # conferma di piu' prese insieme
_TIMER_RICALCOLO: dict[str, threading.Timer] = {}
_LOCK_TIMER = threading.Lock()


def _programma_ricalcolo(comune: str, ritardo: float) -> None:
    with _LOCK_CACHE:
        _CACHE_RISULTATI.pop(_chiave_comune(comune), None)

    def _lavoro():
        with _LOCK_TIMER:
            _TIMER_RICALCOLO.pop(comune, None)
        list(_risultati_per_comune(comune))
        prese.riepilogo_comuni(_comuni_disponibili())

    with _LOCK_TIMER:
        vecchio = _TIMER_RICALCOLO.pop(comune, None)
        if vecchio:
            vecchio.cancel()
        timer = threading.Timer(ritardo, _lavoro)
        timer.daemon = True
        _TIMER_RICALCOLO[comune] = timer
        timer.start()


def _risultati_per_comune(comune_filtro: str | None):
    """Metodo B sull'archivio storico (SQLite) di ogni comune, uno alla
    volta (mai comuni diversi insieme: vedi /riepilogo per il perche'),
    restituisce (comune, risultato) per ognuno. Il calcolo e' in cache
    (vedi _risultato_in_cache): si rifa' solo quando l'archivio cambia.
    Usato da tutte le pagine/endpoint, per non duplicare la stessa
    selezione dei comuni noti al database.
    """
    with database.connessione() as conn:
        comuni = database.elenco_comuni(conn)
        if comune_filtro:
            comuni = [c for c in comuni if c.strip().upper() == comune_filtro.strip().upper()]
        for comune in comuni:
            risultato = _risultato_in_cache(conn, comune)
            if risultato is not None:
                yield comune, risultato


@app.on_event("startup")
def _precalcola_risultati():
    """Scalda la cache in background all'avvio, cosi' anche la prima
    visita dopo un riavvio/deploy e' veloce (non blocca l'avvio)."""
    threading.Thread(target=lambda: list(_risultati_per_comune(None)), daemon=True).start()
    # Primo avvio con il tab Prese: anagrafica ricostruita dai file gia' caricati.
    prese.avvia_ricostruzione_se_serve()


@app.get("/riepilogo")
def riepilogo(comune: str | None = None):
    """Ricalcola il Metodo B sull'intero archivio storico e restituisce un
    riepilogo compatto per comune: Riepilogo_File, Import_WMS,
    Riepilogo_Trimestrale (con utenze attive e flag stime provvisorie),
    Riepilogo_Comune e Riepilogo_Comune_Trimestre — l'equivalente dei
    fogli "ufficiali" dell'Excel. Il dettaglio riga per riga delle
    anomalie (Segnalazioni, Anomalie_MetodoB, Utenze_Scomparse,
    Cessate_Con_Stima_Finale) qui è solo contato, non elencato: vedi
    /diagnostica per l'elenco completo.

    Ricalcola sempre da capo sull'intero archivio di ogni comune (mai solo
    sull'ultimo file caricato), ed elabora UN COMUNE ALLA VOLTA (non tutti
    insieme): combinare comuni diversi in una sola chiamata confonderebbe
    il controllo "utenze scomparse", che confronta l'ultimo file caricato
    in ordine cronologico con i precedenti — se quel file fosse di un
    altro comune, ogni utenza del primo comune sembrerebbe sparita.

    Query string opzionale: ?comune=BELGIOIOSO per limitare il ricalcolo
    a un solo comune (case-insensitive); senza parametro, ricalcola tutti
    i comuni che hanno un archivio su disco.
    """
    comuni_risultato = []
    for comune_trovato, risultato in _risultati_per_comune(comune):
        comuni_risultato.append({
            "comune": comune_trovato,
            "warning": risultato.warning,
            "riepilogo_file": _tabella_json(risultato.riepilogo_file),
            "volumi_distretto_mese": _tabella_json(risultato.volumi_distretto_mese),
            "volumi_distretto_trimestre": _tabella_json(risultato.volumi_distretto_trimestre),
            "volumi_comune_mese": _tabella_json(risultato.volumi_comune_mese),
            "volumi_comune_trimestre": _tabella_json(risultato.volumi_comune_trimestre),
            "diagnostica_da_verificare": {
                "segnalazioni": len(risultato.segnalazioni),
                "anomalie_metodo_b": len(risultato.anomalie_metodo_b),
                "utenze_scomparse": len(risultato.utenze_scomparse),
                "cessate_con_stima_finale": len(risultato.cessate_con_stima_finale),
            },
        })

    if comune and not comuni_risultato:
        raise HTTPException(status_code=404, detail=f"Nessun archivio trovato per il comune '{comune}'")

    return {"comuni": comuni_risultato}


@app.get("/diagnostica")
def diagnostica(comune: str | None = None):
    """Dettaglio riga per riga delle anomalie da verificare per comune —
    la pagina di diagnostica prevista dalle specifiche (sezione 6.0):
    Segnalazioni (distretto anomalo/mancante), Anomalie_MetodoB (letture
    sentinella, reset di contatore non marcati, ritmo di consumo
    assurdo), Utenze_Scomparse (comprese quelle il cui contratto NON
    risulta chiuso — le uniche davvero "da verificare"), Cessate con
    l'ultima lettura una stima invece che reale, e quali (trimestre,
    distretto) contengono ancora stime provvisorie non confermate da una
    lettura reale.

    Stessa semantica di /riepilogo per il parametro ?comune= e per il
    ricalcolo (sempre sull'intero archivio, un comune alla volta).
    """
    comuni_risultato = []
    for comune_trovato, risultato in _risultati_per_comune(comune):
        trimestre_provvisorio = risultato.volumi_distretto_trimestre[
            risultato.volumi_distretto_trimestre["Contiene Stime Provvisorie"] == "Sì"
        ][["Trimestre", "Codice Distretto"]]
        comuni_risultato.append({
            "comune": comune_trovato,
            "segnalazioni": _tabella_json(risultato.segnalazioni),
            "anomalie_metodo_b": _tabella_json(risultato.anomalie_metodo_b),
            "utenze_scomparse": _tabella_json(risultato.utenze_scomparse),
            "cessate_con_stima_finale": _tabella_json(risultato.cessate_con_stima_finale),
            "trimestri_con_stime_provvisorie": _tabella_json(trimestre_provvisorio),
            "utenze_corrette_da_nodma": _tabella_json(risultato.utenze_corrette_da_nodma),
        })

    if comune and not comuni_risultato:
        raise HTTPException(status_code=404, detail=f"Nessun archivio trovato per il comune '{comune}'")

    return {"comuni": comuni_risultato}


def _comuni_disponibili() -> list[str]:
    with database.connessione() as conn:
        return database.elenco_comuni(conn)


def _riassegnati_per_coppia(v: pd.DataFrame) -> list[dict]:
    """Volumi spostati dalle conferme del tab Prese, per coppia da -> a
    (totale su tutti i mesi), dal piu' grande."""
    if v is None or v.empty:
        return []
    g = v.groupby(["Da", "A"], as_index=False).agg(servizi=("Servizi", "max"), volume=("Volume (m3)", "sum"),
                                                   mesi=("Mese", "nunique"))
    return g.sort_values("volume", ascending=False).to_dict("records")


def _contesto_consolidamento(request: Request, errore: str = "", esito: str = "") -> dict:
    calcolati = {c: r.volumi_distretto_mese_calcolati for c, r in _risultati_per_comune(None)}
    fissi = consolidamento.volumi_consolidati()
    anni = []
    for a in consolidamento.anni_consolidati():
        f = fissi[fissi["ANNO"] == a["anno"]]
        comuni = []
        for comune in sorted(set(f["COMUNE"]) | set(calcolati)):
            v = calcolati.get(comune)
            oggi = float(v[v["Mese"].astype(str).str.startswith(f"{a['anno']}-")]["Volume Fatturato (m3)"].sum()) if v is not None and not v.empty else 0.0
            comuni.append({"comune": comune, "consolidato": float(f[f["COMUNE"] == comune]["VOLUME"].sum()), "oggi": oggi})
        anni.append({**a, "comuni": comuni, "consolidato": sum(c["consolidato"] for c in comuni), "oggi": sum(c["oggi"] for c in comuni)})
    fatti = {a["anno"] for a in anni}
    presenti = {int(str(m)[:4]) for v in calcolati.values() if v is not None and not v.empty for m in v["Mese"]}
    return {
        "request": request, "pagina_attiva": "consolidamento", "comuni_disponibili": _comuni_disponibili(),
        "comune_selezionato": None, "anni": anni, "anni_consolidabili": sorted(presenti - fatti),
        "errore": errore, "esito": esito,
    }


@app.get("/pagine/consolidamento")
def pagina_consolidamento(request: Request):
    """Anni consolidati (copia fissa di Import_WMS) e, per gli admin, il
    pulsante per consolidare un anno (Daniele, 26/09/2026)."""
    return templates.TemplateResponse(request, "consolidamento.html", _contesto_consolidamento(request))


def _contesto_invio_wms(request: Request, comuni_scelti: list[str] | None = None, errore: str = "",
                        anteprima: dict | None = None, esito: dict | None = None) -> dict:
    tutti = _comuni_disponibili()
    return {
        "request": request, "pagina_attiva": "invio_wms", "comuni_disponibili": tutti, "comune_selezionato": None,
        "comuni_scelti": comuni_scelti if comuni_scelti is not None else tutti,
        "configurato": invio_wms.configurato(), "invii": invio_wms.elenco_invii(),
        "errore": errore, "anteprima": anteprima, "esito": esito,
    }


def _dati_invio(comuni: list[str]) -> dict:
    return invio_wms.prepara(list(_risultati_per_comune(None)), comuni, _mesi_incompleti)


@app.get("/pagine/invio-wms")
def pagina_invio_wms(request: Request):
    """Invio dei volumi a WMS SmartH2O: anteprima, poi Invia (editor).
    Vedi app/invio_wms.py."""
    return templates.TemplateResponse(request, "invio_wms.html", _contesto_invio_wms(request))


@app.post("/pagine/invio-wms/anteprima")
async def invio_wms_anteprima(request: Request):
    form = await request.form()
    comuni = form.getlist("comuni")
    if not comuni:
        return templates.TemplateResponse(request, "invio_wms.html", _contesto_invio_wms(request, [], "Scegli almeno un comune."))
    try:
        dati = await run_in_threadpool(_dati_invio, comuni)
        esito = await run_in_threadpool(invio_wms.chiama_wms, dati, True, request.state.utente["username"])
    except ValueError as exc:
        return templates.TemplateResponse(request, "invio_wms.html", _contesto_invio_wms(request, comuni, str(exc)))
    anteprima = {**invio_wms.riassunto_anteprima(esito), "conteggi": esito["conteggi"], "firma": dati["firma"],
                 "mesi_esclusi": dati["mesi_esclusi"],
                 "indicatori": esito.get("conteggi_indicatori"),
                 "n_righe": len(dati["righe"]), "n_provvisorie": sum(r["provvisorio"] for r in dati["righe"])}
    return templates.TemplateResponse(request, "invio_wms.html", _contesto_invio_wms(request, comuni, anteprima=anteprima))


@app.post("/pagine/invio-wms/invia")
async def invio_wms_invia(request: Request):
    form = await request.form()
    comuni = form.getlist("comuni")
    utente = request.state.utente["username"]
    try:
        dati = await run_in_threadpool(_dati_invio, comuni)
        if not comuni or dati["firma"] != form.get("firma"):
            raise ValueError("I numeri sono cambiati dall'anteprima (nuova estrazione, conferme o consolidamento): "
                             "rifai l'anteprima prima di inviare.")
        esito = await run_in_threadpool(invio_wms.chiama_wms, dati, False, utente)
    except ValueError as exc:
        auth.registra(utente, "invio_wms_errore", f"{', '.join(comuni)}: {exc}", accessi.ip_client(request))
        return templates.TemplateResponse(request, "invio_wms.html", _contesto_invio_wms(request, comuni, str(exc)))
    id_invio = await run_in_threadpool(invio_wms.registra_invio, dati, esito, utente)
    c = esito["conteggi"]
    auth.registra(utente, "invio_wms",
                  f"invio {id_invio} — {', '.join(dati['comuni'])}: {len(dati['righe'])} righe, {c['creato']} create, "
                  f"{c['aggiornato']} aggiornate, {c['invariato']} invariate, {c['scartato']} scartate, "
                  f"{c.get('cancellato', 0)} cancellate",
                  accessi.ip_client(request))
    return templates.TemplateResponse(request, "invio_wms.html",
                                      _contesto_invio_wms(request, comuni, esito={"id": id_invio, "conteggi": c,
                                                                                  "indicatori": esito.get("conteggi_indicatori")}))


# Aggiornamento dei dati di riferimento esterni dalla pagina Amministrazione
# (Daniele, 27/09/2026: ANNCSU cambia ogni mese). Un aggiornamento alla
# volta per tipo, in un thread; i risultati dei controlli si ricalcolano da
# soli perche' la data dei file entra nella versione dei dati (prese).
STATO_RIFERIMENTI: dict[str, dict] = {"anncsu": {}, "osm": {}}


def _aggiorna_riferimento(tipo: str, utente: str, ip: str) -> None:
    import contextlib
    stato = STATO_RIFERIMENTI[tipo]
    stato.update(in_corso=True, inizio=time.strftime("%d/%m %H:%M"), esito="")
    uscita = io.StringIO()
    try:
        with contextlib.redirect_stdout(uscita):
            if tipo == "anncsu":
                from scripts import aggiorna_anncsu
                aggiorna_anncsu.main()
            else:
                from scripts import scarica_vie_osm
                scarica_vie_osm.main([])
        ultima = [r for r in uscita.getvalue().splitlines() if r.strip()][-1:] or ["fatto"]
        stato["esito"] = f"ultimo aggiornamento riuscito ({time.strftime('%d/%m %H:%M')}): {ultima[0]}"
        auth.registra(utente, f"aggiorna_{tipo}", f"completato: {ultima[0]}", ip)
    except Exception as exc:  # l'errore si mostra in pagina, i file vecchi restano
        stato["esito"] = f"aggiornamento NON riuscito ({time.strftime('%d/%m %H:%M')}): {exc}"
        auth.registra(utente, f"aggiorna_{tipo}", f"errore: {exc}", ip)
    finally:
        stato["in_corso"] = False


def _info_riferimenti() -> list[dict]:
    from datetime import date as _date
    righe = []
    oggi = _date.today()

    def vecchio(d):
        try:
            return (oggi - _date.fromisoformat(d)).days > 35
        except (TypeError, ValueError):
            return False

    p = anncsu.PERCORSO_ANNCSU
    data, contenuto = "", "file assente"
    if p.exists():
        testa = p.open(encoding="utf-8").readline()
        m = re.search(r"(\d{4})(\d{2})(\d{2})\.zip", testa)
        data = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
        d = anncsu._dati()
        con = d.groupby("COMUNE")["LAT"].apply(lambda s: s.notna().mean() >= 0.5)
        contenuto = f"{len(d):,} civici di {d['COMUNE'].nunique()} comuni; posizionati in {int(con.sum())}".replace(",", ".")
    righe.append({"nome": "Civici ANNCSU", "fonte": "Agenzia delle Entrate e ISTAT, CC-BY 4.0", "data": data,
                  "vecchio": vecchio(data), "contenuto": contenuto, "azione": "anncsu", "stato": STATO_RIFERIMENTI["anncsu"]})
    p = vie_osm.PERCORSO_VIE_OSM
    data, contenuto = "", "file assente"
    if p.exists():
        g = json.loads(p.read_text(encoding="utf-8"))
        data = g.get("scaricato_il", "")
        contenuto = f"{len(g['features']):,} tratti di strada di {len({f['properties']['comune'] for f in g['features']})} comuni".replace(",", ".")
    righe.append({"nome": "Vie OpenStreetMap", "fonte": "(c) OpenStreetMap contributors, ODbL", "data": data,
                  "vecchio": False, "contenuto": contenuto, "azione": "osm", "stato": STATO_RIFERIMENTI["osm"]})
    righe.append({"nome": "Confini dei comuni", "fonte": "ISTAT, unità amministrative a fini statistici", "data": "2025-01-01",
                  "vecchio": False, "contenuto": "aggiornamento annuale, a mano (vedi Storia)", "azione": "", "stato": {}})
    return righe


@app.get("/admin/riferimenti")
def admin_riferimenti(request: Request, msg: str = ""):
    """Date e aggiornamento dei dati di riferimento esterni (solo admin)."""
    return templates.TemplateResponse(request, "riferimenti.html", {
        "request": request, "pagina_attiva": "admin", "comuni_disponibili": _comuni_disponibili(),
        "comune_selezionato": None, "riferimenti": _info_riferimenti(), "msg": msg,
    })


@app.post("/admin/riferimenti/{tipo}")
def admin_aggiorna_riferimento(request: Request, tipo: str):
    if tipo not in STATO_RIFERIMENTI:
        raise HTTPException(status_code=404, detail="Dato di riferimento sconosciuto.")
    if not STATO_RIFERIMENTI[tipo].get("in_corso"):
        auth.registra(request.state.utente["username"], f"aggiorna_{tipo}", "avviato", accessi.ip_client(request))
        threading.Thread(target=_aggiorna_riferimento, args=(tipo, request.state.utente["username"], accessi.ip_client(request)),
                         daemon=True).start()
    return RedirectResponse("/admin/riferimenti?msg=Aggiornamento+avviato+in+background", status_code=303)


@app.post("/admin/consolida")
async def admin_consolida(request: Request):
    """Consolida un anno: copia fissa di Import_WMS di tutti i comuni.
    Solo admin (middleware su /admin/*). Irreversibile."""
    form = await request.form()
    try:
        anno = int(str(form.get("anno", "")))
    except ValueError:
        return templates.TemplateResponse(request, "consolidamento.html",
                                          await run_in_threadpool(_contesto_consolidamento, request, "Anno non valido."))
    nota = str(form.get("nota", ""))
    calcolati = await run_in_threadpool(lambda: {c: r.volumi_distretto_mese_calcolati for c, r in _risultati_per_comune(None)})
    try:
        n = await run_in_threadpool(consolidamento.consolida, anno, calcolati, request.state.utente["username"], nota)
    except ValueError as exc:
        return templates.TemplateResponse(request, "consolidamento.html",
                                          await run_in_threadpool(_contesto_consolidamento, request, str(exc)))
    auth.registra(request.state.utente["username"], "consolida_anno", f"anno {anno}: {n} righe; nota: {nota}", accessi.ip_client(request))
    with _LOCK_CACHE:
        _CACHE_RISULTATI.clear()
    threading.Thread(target=lambda: list(_risultati_per_comune(None)), daemon=True).start()
    return templates.TemplateResponse(request, "consolidamento.html", await run_in_threadpool(
        _contesto_consolidamento, request, "", f"Anno {anno} consolidato: {n} righe mese/distretto salvate."))


@app.get("/pagine/riepilogo")
def pagina_riepilogo(request: Request, comune: str | None = None, dal: str = "", al: str = ""):
    """Stessi dati di /riepilogo, mostrati come pagina HTML invece che
    JSON grezzo — la vista principale prevista dalle specifiche (sezione
    6.0): filtro Comune nell'header (persistente tra le pagine), KPI in
    alto, tabella Import_WMS sotto. Senza ?comune= mostra un colpo
    d'occhio su tutti i comuni invece del dettaglio.
    """
    comuni_disponibili = _comuni_disponibili()

    def filtra_periodo(volumi: pd.DataFrame) -> pd.DataFrame:
        # Filtro periodo (richiesto da Daniele il 21/09/2026): dal/al mese
        # inclusi, formato YYYY-MM, confronto sul testo perche' Mese e' un Period.
        mesi = volumi["Mese"].astype(str)
        maschera = pd.Series(True, index=volumi.index)
        if dal:
            maschera &= mesi >= dal
        if al:
            maschera &= mesi <= al
        return volumi[maschera]

    if comune:
        trovati = list(_risultati_per_comune(comune))
        if not trovati:
            raise HTTPException(status_code=404, detail=f"Nessun archivio trovato per il comune '{comune}'")
        comune_trovato, risultato = trovati[0]
        mesi_disponibili = sorted(risultato.volumi_distretto_mese["Mese"].astype(str).unique())
        mesi_incompleti = _mesi_incompleti(risultato.volumi_distretto_mese)
        volumi_periodo = filtra_periodo(risultato.volumi_distretto_mese)
        trimestri_provvisori = risultato.volumi_distretto_trimestre[
            risultato.volumi_distretto_trimestre["Contiene Stime Provvisorie"] == "Sì"
        ]["Trimestre"].nunique()
        pivot = _pivot_mese_distretto(volumi_periodo)
        return templates.TemplateResponse(request, "riepilogo.html", {
            "mesi_disponibili": mesi_disponibili,
            "mesi_incompleti": mesi_incompleti,
            "dal": dal,
            "al": al,
            "request": request,
            "pagina_attiva": "riepilogo",
            "comuni_disponibili": comuni_disponibili,
            "comune_selezionato": comune_trovato,
            "comune": comune_trovato,
            "warning": risultato.warning,
            "volume_totale": float(volumi_periodo["Volume Fatturato (m3)"].sum()),
            "n_mesi": int(volumi_periodo["Mese"].nunique()),
            "trimestri_provvisori": int(trimestri_provvisori),
            "distretti": pivot["distretti"],
            "righe_pivot": pivot["righe"],
            "riassegnati": _riassegnati_per_coppia(risultato.volumi_riassegnati),
            "anni_consolidati": consolidamento.anni_consolidati(),
        })

    tutti = list(_risultati_per_comune(None))
    riepilogo_comuni = [
        {"comune": c, "volume_totale": float(filtra_periodo(r.volumi_distretto_mese)["Volume Fatturato (m3)"].sum())}
        for c, r in tutti
    ]
    mesi_disponibili = sorted({str(m) for _, r in tutti for m in r.volumi_distretto_mese["Mese"].unique()})
    return templates.TemplateResponse(request, "riepilogo.html", {
        "mesi_disponibili": mesi_disponibili,
        "dal": dal,
        "al": al,
        "request": request,
        "pagina_attiva": "riepilogo",
        "comuni_disponibili": comuni_disponibili,
        "comune_selezionato": None,
        "comune": None,
        "riepilogo_comuni": riepilogo_comuni,
    })


@app.get("/pagine/diagnostica")
def pagina_diagnostica(request: Request, comune: str | None = None):
    """Stessi dati di /diagnostica, mostrati come pagina HTML — vedi
    pagina_riepilogo per la logica del filtro Comune nell'header.
    """
    comuni_disponibili = _comuni_disponibili()

    if not comune:
        return templates.TemplateResponse(request, "diagnostica.html", {
            "request": request,
            "pagina_attiva": "diagnostica",
            "comuni_disponibili": comuni_disponibili,
            "comune_selezionato": None,
            "comune": None,
        })

    trovati = list(_risultati_per_comune(comune))
    if not trovati:
        raise HTTPException(status_code=404, detail=f"Nessun archivio trovato per il comune '{comune}'")
    comune_trovato, risultato = trovati[0]

    utenze_scomparse_json = _tabella_json(risultato.utenze_scomparse)
    utenze_scomparse_da_verificare = [
        r for r in utenze_scomparse_json if r["Da Verificare"] == "Sì (era ancora attiva)"
    ]

    # I distretti anomali o mancanti stanno in Punti di erogazione (Daniele, 30/09/2026): qui solo il numero e il link.
    try:
        dp_da_confermare = prese.riepilogo_comuni([comune_trovato])[0]["aperti"]
    except Exception:
        dp_da_confermare = None

    return templates.TemplateResponse(request, "diagnostica.html", {
        "request": request,
        "pagina_attiva": "diagnostica",
        "comuni_disponibili": comuni_disponibili,
        "comune_selezionato": comune_trovato,
        "comune": comune_trovato,
        "dp_da_confermare": dp_da_confermare,
        "anomalie_metodo_b": _tabella_json(risultato.anomalie_metodo_b),
        "utenze_scomparse_da_verificare": utenze_scomparse_da_verificare,
        "cessate_con_stima_finale": _tabella_json(risultato.cessate_con_stima_finale),
        "utenze_corrette_da_nodma": _tabella_json(risultato.utenze_corrette_da_nodma),
        "stato_chiusura_mesi": _tabella_json(risultato.stato_chiusura_mesi),
    })


@app.get("/pagine/utenza")
def pagina_utenza(request: Request, comune: str, codice_servizio: int | None = None):
    """Cronologia completa delle letture di UNA utenza (data, tipo,
    lettura, indirizzo, distretto...), con le eventuali righe del Metodo B
    (anomalie_metodo_b) che la riguardano evidenziate in cima. Pensata per
    essere raggiunta cliccando un Codice Servizio dalla pagina di
    diagnostica, per verificare un'anomalia senza dover interrogare il
    database a mano (vedi conversazione con Daniele del 18/09/2026, caso
    75768720).
    """
    if codice_servizio is None:
        # Il filtro Comune nell'header (vedi base.html) invia un GET sulla
        # stessa pagina con solo ?comune=: qui non basta a mostrare nulla
        # di sensato, quindi si torna alla diagnostica di quel comune
        # invece di rispondere con un errore di validazione.
        return RedirectResponse(url=f"/pagine/diagnostica?comune={comune}")

    comuni_disponibili = _comuni_disponibili()

    with database.connessione() as conn:
        letture_comune = database.carica_letture(conn, comune)
    letture_utenza = letture_comune[letture_comune["CODICE_SERVIZIO"] == codice_servizio]
    if letture_utenza.empty:
        raise HTTPException(
            status_code=404,
            detail=f"Nessuna lettura trovata per l'utenza {codice_servizio} nel comune '{comune}'",
        )
    letture_utenza = motore_calcolo._ordina_priorita_stessa_data(letture_utenza)
    letture_utenza = letture_utenza.copy()
    letture_utenza["_reale"] = letture_utenza["TIPO_LETTURA"].isin(motore_calcolo.TIPI_LETTURA_REALE)
    ultima = letture_utenza.iloc[-1]

    trovati = list(_risultati_per_comune(comune))
    anomalie_utenza = []
    if trovati:
        _, risultato = trovati[0]
        anomalie_utenza = _anomalie_per_utenza(risultato, codice_servizio)

    return templates.TemplateResponse(request, "utenza.html", {
        "request": request,
        "senza_filtro_comune": True,  # dettaglio di un'utenza: il filtro Comune qui non ha senso
        "pagina_attiva": "diagnostica",
        "comuni_disponibili": comuni_disponibili,
        "comune_selezionato": comune,
        "comune": comune,
        "codice_servizio": codice_servizio,
        "indirizzo": ultima["INDIRIZZO_UBICAZIONE"],
        "cap": int(ultima["CAP_UBICAZIONE"]) if pd.notnull(ultima["CAP_UBICAZIONE"]) else None,
        "distretto": ultima["DISTRETTO"],
        "stato_servizio": ultima["STATO_SERVIZIO"],
        "prodotto": ultima["PRODOTTO_CODICE"],
        "anomalie_utenza": anomalie_utenza,
        "letture": _tabella_json(letture_utenza[[
            "DATA_LETTURA", "TIPO_LETTURA", "_reale", "LETTURA", "CONSUMO", "GG_LETT_PREC",
            "STATO_LETTURA", "DISTRETTO", "FILE_ORIGINE",
        ]]),
    })


@app.get("/api/top_consumatori")
def api_top_consumatori(comune: str, dal: str = "", al: str = "", n: int = 10):
    """Classifica dei maggiori consumatori nel periodo dal/al (mesi inclusi,
    'YYYY-MM', stesso filtro della pagina Volumi; vuoto = senza limite) —
    calcolata a richiesta su risultato.volumi_utenza_mese invece che
    incorporata nella pagina (vedi pagina_riepilogo), perche' quella
    tabella e' granulare per singola utenza e puo' avere decine di migliaia
    di righe per comune.
    """
    trovati = list(_risultati_per_comune(comune))
    if not trovati:
        raise HTTPException(status_code=404, detail=f"Nessun archivio trovato per il comune '{comune}'")
    _, risultato = trovati[0]

    df = risultato.volumi_utenza_mese
    if df.empty:
        return {"utenze": []}

    mesi = df["Mese"].astype(str)
    maschera = pd.Series(True, index=df.index)
    if dal:
        maschera &= mesi >= dal
    if al:
        maschera &= mesi <= al
    selezione = df[maschera]

    if selezione.empty:
        return {"utenze": []}

    top = (
        selezione.groupby(["Codice Servizio", "Codice Distretto", "Classe d'uso"], as_index=False)["Volume (m3)"]
        .sum()
        .sort_values("Volume (m3)", ascending=False)
        .head(n)
    )

    # L'indirizzo non e' in volumi_utenza_mese (vedi aggrega_utenza_mese):
    # si recupera qui, dall'archivio grezzo, solo per le poche utenze in
    # classifica (mai per l'intera tabella granulare) — stessa logica di
    # "ultima lettura nota" gia' usata da pagina_utenza.
    with database.connessione() as conn:
        letture_comune = database.carica_letture(conn, comune)
    indirizzi = (
        letture_comune[letture_comune["CODICE_SERVIZIO"].isin(top["Codice Servizio"])]
        .sort_values("DATA_LETTURA")
        .groupby("CODICE_SERVIZIO")["INDIRIZZO_UBICAZIONE"]
        .last()
    )
    top["Indirizzo"] = top["Codice Servizio"].map(indirizzi)

    # Consumo medio giornaliero STIMATO: il volume del periodo selezionato
    # diviso i giorni di calendario del periodo — e' una media, non una
    # misura (lo stesso principio della proratazione mensile del Metodo B).
    giorni_periodo = sum(m.days_in_month for m in selezione["Mese"].unique())
    top["Consumo Medio Stimato (m3/giorno)"] = (top["Volume (m3)"] / giorni_periodo).round(2)

    # Ritmo REALE: il m3/giorno dell'ULTIMA differenza tra due letture reali
    # che ricade nel periodo selezionato, preso da riferimento_prodie (lo
    # stesso foglio di controllo usato da calcola_periodi_metodo_b per ogni
    # confronto del Metodo B — non una stima, la misura fisica vera).
    # Se l'utenza non ha nessun confronto reale in quel periodo (solo stime
    # provvisorie/interpolate), resta vuoto: non si inventa un valore.
    rif = risultato.riferimento_prodie
    mesi_fine = rif["DATA_FINE"].dt.to_period("M").astype(str)
    maschera_rif = pd.Series(True, index=rif.index)
    if dal:
        maschera_rif &= mesi_fine >= dal
    if al:
        maschera_rif &= mesi_fine <= al
    rif_periodo = rif[maschera_rif]
    ritmo_reale = (
        rif_periodo[rif_periodo["CODICE_SERVIZIO"].isin(top["Codice Servizio"])]
        .sort_values("DATA_FINE")
        .groupby("CODICE_SERVIZIO")["M3_GIORNO"]
        .last()
    )
    top["Ritmo Reale Ultima Lettura (m3/giorno)"] = top["Codice Servizio"].map(ritmo_reale)

    return {"utenze": _tabella_json(top)}


@app.get("/pagine/info")
def pagina_info(request: Request, comune: str | None = None):
    """Pagina statica (nessun ricalcolo, nessun dato per comune): spiega in
    linguaggio semplice come funziona il Metodo B e come si leggono i
    grafici, per chi usa l'app senza aver letto il codice o le specifiche.
    Il filtro Comune nell'header resta visibile per coerenza di navigazione
    ma non cambia nulla in questa pagina.
    """
    return templates.TemplateResponse(request, "info.html", {
        "request": request,
        "pagina_attiva": "info",
        "comuni_disponibili": _comuni_disponibili(),
        "comune_selezionato": comune,
    })


@app.get("/pagine/storia")
def pagina_storia(request: Request, comune: str | None = None):
    """Pagina statica con la cronologia di sviluppo del progetto — utile a
    Daniele per ritrovare velocemente perche' un numero e' cambiato da una
    sessione all'altra, senza dover leggere i commit git uno per uno.
    """
    return templates.TemplateResponse(request, "storia.html", {
        "request": request,
        "pagina_attiva": "storia",
        "comuni_disponibili": _comuni_disponibili(),
        "comune_selezionato": comune,
    })


def _pagina_distretti_contesto(
    request: Request, modifica: str | None, comune: str | None = None,
    errore_import=None, esito_import=None, errore_import_confini=None, esito_import_confini=None,
) -> dict:
    """Contesto comune a GET /pagine/distretti e a ogni azione (importa/
    salva/elimina) che ri-renderizza la stessa pagina invece di fare un
    redirect — cosi' un errore di import resta visibile senza perdersi in
    un redirect.
    """
    df_mappa = motore_calcolo.carica_mappa_distretti_df()
    riga_modifica = None
    if modifica:
        trovata = df_mappa[df_mappa["codice_distretto"] == modifica.strip().upper()]
        if not trovata.empty:
            riga_modifica = trovata.iloc[0].to_dict()

    df_righe = df_mappa
    if comune:
        comune_norm = comune.strip().upper()
        df_righe = df_mappa[
            (df_mappa["comune_ufficiale"] == comune_norm)
            | df_mappa["comuni_associabili"].str.split(";").apply(
                lambda lista: comune_norm in [c.strip() for c in lista]
            )
        ]

    return {
        "request": request,
        "pagina_attiva": "distretti",
        "comuni_disponibili": _comuni_disponibili(),
        "comune_selezionato": comune,
        "righe": df_righe.to_dict(orient="records"),
        "riga_modifica": riga_modifica,
        "errore_import": errore_import,
        "esito_import": esito_import,
        "errore_import_confini": errore_import_confini,
        "esito_import_confini": esito_import_confini,
    }


@app.get("/pagine/distretti")
def pagina_distretti(request: Request, modifica: str | None = None, comune: str | None = None):
    """Gestione dell'elenco ufficiale distretto -> comune usato da
    classifica_distretto (vedi project_docs/distretti_comuni.csv): tabella
    con un modulo per aggiungere/modificare/eliminare una riga alla volta,
    più due moduli per importare file che Daniele già ha (l'elenco
    distretto/comune, e i confini GeoJSON per la pagina Mappa) — richiesto
    da Daniele il 18/09/2026.

    ?modifica=<codice> pre-compila il modulo con la riga esistente.
    ?comune=<nome> (il filtro Comune nell'header, come nelle altre pagine)
    limita la tabella ai distretti di quel comune o a lui associabili —
    prima veniva ignorato, mostrava sempre tutto l'elenco.
    """
    return templates.TemplateResponse(
        request, "distretti.html", _pagina_distretti_contesto(request, modifica, comune)
    )


@app.post("/distretti/importa")
async def importa_distretti(request: Request, file: UploadFile = File(...)):
    """Importa (upsert per codice_distretto, non sovrascrive l'intero
    elenco) un file che Daniele carica dalla pagina — vedi
    motore_calcolo.importa_mappa_distretti per il riconoscimento delle
    colonne. Un file con intestazioni non riconosciute non blocca nulla:
    l'errore torna visibile in pagina con le colonne che sono state lette,
    invece di un errore HTTP generico.
    """
    nome_originale = file.filename or "distretti.csv"
    percorso_temp = Path("/tmp") / f"import_distretti_{int(time.time())}_{nome_originale}"
    percorso_temp.write_bytes(await file.read())

    errore = None
    esito = None
    try:
        esito = await run_in_threadpool(motore_calcolo.importa_mappa_distretti, percorso_temp)
    except ValueError as exc:
        errore = str(exc)
    except Exception as exc:
        errore = f"File '{nome_originale}' non leggibile: {exc}"
    finally:
        percorso_temp.unlink(missing_ok=True)

    auth.registra(
        request.state.utente["username"], "distretti_importa",
        f"{nome_originale}: " + (f"errore ({errore})" if errore else str(esito)), accessi.ip_client(request),
    )
    return templates.TemplateResponse(
        request, "distretti.html",
        _pagina_distretti_contesto(request, modifica=None, errore_import=errore, esito_import=esito),
    )


@app.post("/distretti/importa-confini")
async def importa_confini_endpoint(request: Request, file: UploadFile = File(...)):
    """Importa il GeoJSON dei confini reali dei distretti (usato dalla
    pagina Mappa al posto dei quadrati segnaposto) — vedi
    motore_calcolo.importa_confini_distretti per il riconoscimento della
    proprietà che contiene il codice distretto.
    """
    nome_originale = file.filename or "confini.geojson"
    percorso_temp = Path("/tmp") / f"import_confini_{int(time.time())}_{nome_originale}"
    percorso_temp.write_bytes(await file.read())

    errore = None
    esito = None
    try:
        esito = await run_in_threadpool(motore_calcolo.importa_confini_distretti, percorso_temp)
    except ValueError as exc:
        errore = str(exc)
    except Exception as exc:
        errore = f"File '{nome_originale}' non leggibile: {exc}"
    finally:
        percorso_temp.unlink(missing_ok=True)

    auth.registra(
        request.state.utente["username"], "confini_importa",
        f"{nome_originale}: " + (f"errore ({errore})" if errore else str(esito)), accessi.ip_client(request),
    )
    return templates.TemplateResponse(
        request, "distretti.html",
        _pagina_distretti_contesto(
            request, modifica=None, errore_import_confini=errore, esito_import_confini=esito
        ),
    )


@app.post("/distretti/salva")
def salva_distretto(
    request: Request,
    codice_distretto: str = Form(...),
    comune_ufficiale: str = Form(...),
    comuni_associabili: str = Form(""),
    nome_distretto: str = Form(""),
    comune: str = Form(""),
):
    """Aggiunge o aggiorna una riga dell'elenco (upsert per codice
    distretto) dal modulo della pagina. comune e' solo il filtro attivo
    nell'header (se c'era), per tornare alla stessa vista filtrata invece
    di perderla ad ogni salvataggio."""
    motore_calcolo.upsert_distretto(codice_distretto, comune_ufficiale, comuni_associabili, nome_distretto)
    auth.registra(
        request.state.utente["username"], "distretto_salvato",
        f"{codice_distretto} -> {comune_ufficiale}" + (f" (associabili: {comuni_associabili})" if comuni_associabili else ""),
        accessi.ip_client(request),
    )
    url = f"/pagine/distretti?comune={comune}" if comune else "/pagine/distretti"
    return RedirectResponse(url=url, status_code=303)


@app.post("/distretti/elimina")
def elimina_distretto_endpoint(request: Request, codice_distretto: str = Form(...), comune: str = Form("")):
    """Rimuove una riga dall'elenco (il distretto torna a ricadere
    sull'euristica del prefisso al prossimo ricalcolo)."""
    motore_calcolo.elimina_distretto(codice_distretto)
    auth.registra(request.state.utente["username"], "distretto_eliminato", codice_distretto, accessi.ip_client(request))
    url = f"/pagine/distretti?comune={comune}" if comune else "/pagine/distretti"
    return RedirectResponse(url=url, status_code=303)


# Coordinate approssimate (centro comune) SOLO per generare poligoni
# segnaposto nella pagina Mappa, finche' non arriva il GeoJSON vero dei
# confini distretto (vedi pagina_mappa) — da buttare via a quel punto.
# Fonte: posizione approssimativa nota dei comuni in provincia di Pavia,
# non un dato dell'archivio.
_COORDINATE_DEMO_COMUNI = {
    "BELGIOIOSO": (45.1517, 9.3739),
    "MORTARA": (45.2508, 8.7379),
    "BRONI": (45.0606, 9.2606),
    "STRADELLA": (45.0763, 9.3011),
}


def _geojson_dimostrativo() -> dict:
    """Poligoni SEGNAPOSTO (piccoli quadrati, non confini reali) per i
    distretti dei comuni in _COORDINATE_DEMO_COMUNI, con codice e nome
    presi dall'elenco vero (project_docs/distretti_comuni.csv) — in attesa
    del GeoJSON vero dei confini, da collegare al posto di questi non
    appena disponibile (vedi pagina_mappa).
    """
    df = motore_calcolo.carica_mappa_distretti_df()
    features = []
    lato = 0.006  # ~650m, solo per dare un'idea di scala, non e' un confine vero
    for comune, (lat_centro, lon_centro) in _COORDINATE_DEMO_COMUNI.items():
        distretti_comune = df[df["comune_ufficiale"] == comune].reset_index(drop=True)
        n = len(distretti_comune)
        if n == 0:
            continue
        colonne_griglia = max(1, int(n**0.5 + 0.5))
        for i, riga in distretti_comune.iterrows():
            riga_griglia, colonna_griglia = divmod(i, colonne_griglia)
            lat0 = lat_centro + (riga_griglia - n / (2 * colonne_griglia)) * lato * 1.3
            lon0 = lon_centro + (colonna_griglia - colonne_griglia / 2) * lato * 1.3
            quadrato = [
                [lon0, lat0], [lon0 + lato, lat0], [lon0 + lato, lat0 + lato],
                [lon0, lat0 + lato], [lon0, lat0],
            ]
            features.append({
                "type": "Feature",
                "properties": {
                    "codice_distretto": riga["codice_distretto"],
                    "nome_distretto": riga["nome_distretto"] or riga["codice_distretto"],
                    "comune": comune,
                },
                "geometry": {"type": "Polygon", "coordinates": [quadrato]},
            })
    return {"type": "FeatureCollection", "features": features}


def _dati_tematici_mappa() -> dict:
    """Per ogni distretto con archivio caricato: volume e composizione
    reale/provvisorio/interpolato dell'ultimo mese disponibile, più il
    numero di segnalazioni aperte (Anomalie Metodo B + Cessate con stima
    finale) — usati da /pagine/mappa per colorare i distretti (tema scelto
    in pagina: affidabilità/volume/segnalazioni). I distretti di comuni non
    ancora caricati restano fuori da questo dizionario: la mappa li mostra
    in grigio neutro, "nessun dato", invece di fingere un valore.
    """
    dati: dict = {}
    for comune, risultato in _risultati_per_comune(None):
        incompleti = set(_mesi_incompleti(risultato.volumi_distretto_mese))
        origine = risultato.volumi_distretto_mese_origine
        if not origine.empty:
            ultimo_mese = origine.groupby("Codice Distretto")["Mese"].transform("max")
            for _, r in origine[origine["Mese"] == ultimo_mese].iterrows():
                totale = r["Reale (m3)"] + r["Provvisorio (m3)"] + r["Interpolato (m3)"]
                dati[r["Codice Distretto"]] = {
                    "comune": comune,
                    "mese": str(r["Mese"]),
                    "volume": round(totale, 2),
                    "pct_reale": round(r["Reale (m3)"] / totale * 100, 1) if totale else 0,
                    "pct_provvisorio": round(r["Provvisorio (m3)"] / totale * 100, 1) if totale else 0,
                    "pct_interpolato": round(r["Interpolato (m3)"] / totale * 100, 1) if totale else 0,
                    "incompleto": str(r["Mese"]) in incompleti,
                    "n_segnalazioni": 0,
                    "per_mese": {},
                }
            # Tutti i mesi, per il selettore del mese o del periodo della Home (Daniele, 30/09/2026):
            # [reale, provvisorio, interpolato (m3), mese incompleto 0/1].
            for _, r in origine.iterrows():
                d = dati.get(r["Codice Distretto"])
                if d is not None:
                    d["per_mese"][str(r["Mese"])] = [
                        round(float(r["Reale (m3)"]), 2), round(float(r["Provvisorio (m3)"]), 2),
                        round(float(r["Interpolato (m3)"]), 2), int(str(r["Mese"]) in incompleti)]

        conteggio = pd.concat([
            risultato.anomalie_metodo_b[["DISTRETTO"]].rename(columns={"DISTRETTO": "Codice Distretto"}),
            risultato.cessate_con_stima_finale[["Distretto"]].rename(columns={"Distretto": "Codice Distretto"}),
        ], ignore_index=True)["Codice Distretto"].value_counts()
        for codice, n in conteggio.items():
            if codice in dati:
                dati[codice]["n_segnalazioni"] = int(n)
            else:
                dati[codice] = {
                    "comune": comune, "mese": None, "volume": None,
                    "pct_reale": None, "pct_provvisorio": None, "pct_interpolato": None,
                    "incompleto": False,
                    "n_segnalazioni": int(n),
                    "per_mese": {},
                }
    return dati


def _geojson_per_mappa() -> tuple[dict, bool]:
    """Il GeoJSON da mostrare in /pagine/mappa: quello vero (importato da
    /pagine/distretti, vedi motore_calcolo.importa_confini_distretti) se
    esiste su disco, altrimenti i quadrati segnaposto. Restituisce anche
    se e' quello vero, per il messaggio in pagina. Il nome_distretto viene
    sempre preso dall'elenco distretti_comuni.csv (piu' facile da tenere
    aggiornato che il GeoJSON), non da quello eventualmente nel GeoJSON.
    """
    if motore_calcolo.PERCORSO_CONFINI_DISTRETTI.exists():
        dati = json.loads(motore_calcolo.PERCORSO_CONFINI_DISTRETTI.read_text(encoding="utf-8"))
        df_elenco = motore_calcolo.carica_mappa_distretti_df().set_index("codice_distretto")
        for feature in dati.get("features", []):
            codice = feature.get("properties", {}).get("codice_distretto", "")
            feature["properties"]["nome_distretto"] = df_elenco["nome_distretto"].get(codice) or codice
            # Il comune serve alla mappa per aprire la pagina giusta al clic.
            feature["properties"]["comune"] = df_elenco["comune_ufficiale"].get(codice, "")
        return dati, True
    return _geojson_dimostrativo(), False


# NODMA (case sparse) e ND (distretto anomalo/mancante) non sono distretti
# veri: restano fuori dai totali per distretto, come in Import_WMS.
NON_DISTRETTI = ("NODMA", "ND")


# Mese INCOMPLETO (concordato con Daniele il 21/09/2026): dopo l'ultimo giro
# di letture di un comune mancano ancora le letture di chiusura del periodo
# (es. Rivanazzano a maggio-giugno con giro ad aprile, Belgioioso a giugno
# con giro a maggio), quindi il volume del mese e' una frazione del normale
# e non e' una stima provvisoria: il badge "Provvisorio" (quota di stime)
# non lo vedeva e Rivanazzano risultava "Consolidato". Un mese e' incompleto
# se il volume del comune e' sotto il 70% della mediana degli ultimi 3 mesi
# NON incompleti (cosi' due o tre mesi incompleti di fila non falsano il
# riferimento). Non cambia il Metodo B ne' i totali: e' solo un'indicazione;
# quando arriva la lettura di chiusura il mese si corregge da solo. Servono
# almeno 2 mesi precedenti per confrontare (i primi mesi dell'archivio, con
# la catena di letture appena iniziata, restano a Metodo B/fuori periodo).
SOGLIA_MESE_INCOMPLETO = 0.70


def _mesi_incompleti(volumi_distretto_mese: pd.DataFrame) -> list[str]:
    if volumi_distretto_mese.empty:
        return []
    totali = (
        volumi_distretto_mese.assign(Mese=volumi_distretto_mese["Mese"].astype(str))
        .groupby("Mese")["Volume Fatturato (m3)"].sum().sort_index()
    )
    buoni: list[float] = []
    incompleti: list[str] = []
    for mese, volume in totali.items():
        if len(buoni) >= 2:
            riferimento = sorted(buoni[-3:])[len(buoni[-3:]) // 2]
            if volume < SOGLIA_MESE_INCOMPLETO * riferimento:
                incompleti.append(mese)
                continue
        buoni.append(float(volume))
    return incompleti


def _stato_dato(pct_provvisorio, pct_interpolato, incompleto: bool = False) -> str:
    """Stessa regola di colore del tema "Affidabilita'" della mappa
    (mappa.html): incompleto > interpolato > provvisorio (oltre il 10%) >
    consolidato."""
    if incompleto:
        return "incompleto"
    if pct_interpolato and pct_interpolato > 0:
        return "interpolato"
    if pct_provvisorio and pct_provvisorio > 10:
        return "provvisorio"
    return "consolidato"


def _righe_comuni_mappa(dati: dict, comuni: list[str]) -> list[dict]:
    """Tabella dei comuni in archivio per la home (una riga per comune),
    ricavata da _dati_tematici_mappa senza altri ricalcoli. Il volume e'
    la somma dei distretti sull'ultimo mese del comune."""
    righe = []
    for comune in comuni:
        distretti = {
            c: v for c, v in dati.items()
            if v["comune"] == comune and c not in NON_DISTRETTI
        }
        con_dati = {c: v for c, v in distretti.items() if v["mese"]}
        mese = max((v["mese"] for v in con_dati.values()), default=None)
        ultimi = [v for v in con_dati.values() if v["mese"] == mese]
        volume = sum(v["volume"] for v in ultimi)
        prov = sum(v["volume"] * v["pct_provvisorio"] / 100 for v in ultimi)
        interp = sum(v["volume"] * v["pct_interpolato"] / 100 for v in ultimi)
        righe.append({
            "comune": comune,
            "n_distretti": len(distretti),
            "mese": mese,
            "volume": round(volume, 2) if mese else None,
            "stato": _stato_dato(
                prov / volume * 100 if volume else 0, interp / volume * 100 if volume else 0,
                any(v["incompleto"] for v in ultimi),
            ) if mese else None,
            "n_segnalazioni": sum(v["n_segnalazioni"] for v in distretti.values()),
        })
    return righe


def _dati_panoramica() -> dict:
    """Volume (Import_WMS, Metodo B) mese per mese di tutti i comuni, per
    la pagina Panoramica (richiesta da Daniele il 21/09/2026: colpo
    d'occhio su tutti i comuni, bilancio poi fatto semestre per semestre).
    Per ogni comune parte dal primo mese "non cold start" (lo stesso di
    grafici/statistiche: origine.Mese.min) e arriva fino all'ultimo mese
    calcolato, segnando quelli incompleti (vedi _mesi_incompleti) invece di
    tagliarli: cosi' si vede fin dove arriva ogni comune.
    """
    comuni = []
    for comune, risultato in _risultati_per_comune(None):
        v = risultato.volumi_distretto_mese
        if v.empty:
            continue
        per_mese = (
            v.assign(Mese=v["Mese"].astype(str)).groupby("Mese")["Volume Fatturato (m3)"].sum().sort_index()
        )
        origine = risultato.volumi_distretto_mese_origine
        if not origine.empty:
            per_mese = per_mese[per_mese.index >= str(origine["Mese"].min())]
        comuni.append({
            "nome": comune,
            "volumi": {m: round(float(x)) for m, x in per_mese.items()},
            "incompleti": [m for m in _mesi_incompleti(v) if m in per_mese.index],
        })
    mesi = sorted({m for c in comuni for m in c["volumi"]})
    return {"comuni": sorted(comuni, key=lambda c: c["nome"]), "mesi": mesi}


@app.get("/pagine/panoramica")
def pagina_panoramica(request: Request):
    return templates.TemplateResponse(request, "panoramica.html", {
        "request": request,
        "pagina_attiva": "panoramica",
        "comuni_disponibili": _comuni_disponibili(),
        "comune_selezionato": None,
        "dati": _dati_panoramica(),
    })


@app.get("/pagine/mappa")
def pagina_mappa(request: Request):
    """Mappa distretti — Leaflet 1.9.4, stile/palette WMS SmartH2O.
    Confermata da Daniele il 18/09/2026 dopo revisione del prototipo, ora
    in menu. Usa il GeoJSON vero dei confini se e' stato importato da
    /pagine/distretti, altrimenti i quadrati segnaposto (vedi
    _geojson_per_mappa) — nessun altro cambio alla pagina in nessuno dei
    due casi, stessa struttura Feature/properties attesa dal template.
    """
    geojson, confini_veri = _geojson_per_mappa()
    comuni_disponibili = _comuni_disponibili()
    dati_tematici = _dati_tematici_mappa()
    mesi_disponibili = sorted({m for v in dati_tematici.values() for m in v.get("per_mese", {})}, reverse=True)
    return templates.TemplateResponse(request, "mappa.html", {
        "mesi_disponibili": mesi_disponibili,
        "request": request,
        "pagina_attiva": "mappa",
        "comuni_disponibili": comuni_disponibili,
        "comune_selezionato": None,
        "geojson_demo": geojson,
        "confini_veri": confini_veri,
        "dati_tematici": dati_tematici,
        "comuni_tabella": _righe_comuni_mappa(dati_tematici, comuni_disponibili),
    })


@app.get("/pagine/comune/{comune}")
def pagina_comune(request: Request, comune: str, distretto: str | None = None, cambia_comune: str | None = Query(None, alias="comune")):
    """Pagina di un comune, aperta con un clic su un suo distretto dalla
    mappa (richiesta da Daniele il 19/09/2026): KPI, mini-mappa dei
    distretti del comune, grafici e tabella per distretto. Il distretto
    cliccato (?distretto=) risulta evidenziato e preselezionato nei
    grafici. Un distretto il cui comune ufficiale (distretti_comuni.csv) e'
    un altro ma che compare nei dati di questo comune (es. DBRN01, Broni e
    Stradella) e' segnato "condiviso".

    Il filtro Comune nell'header invia il form a questa stessa pagina con
    ?comune=...: se e' diverso dal comune nell'URL si va a quello scelto
    ("Tutti" torna alla mappa), altrimenti il filtro non avrebbe effetto.
    """
    if cambia_comune is not None and cambia_comune.strip().upper() != comune.strip().upper():
        if not cambia_comune.strip():
            return RedirectResponse(url="/pagine/mappa", status_code=303)
        return RedirectResponse(url=f"/pagine/comune/{quote(cambia_comune.strip())}", status_code=303)

    trovati = list(_risultati_per_comune(comune))
    if not trovati:
        raise HTTPException(status_code=404, detail=f"Nessun archivio trovato per il comune '{comune}'")
    comune_trovato, risultato = trovati[0]

    elenco = motore_calcolo.carica_mappa_distretti_df().set_index("codice_distretto")
    origine = risultato.volumi_distretto_mese_origine
    origine_valida = origine[~origine["Codice Distretto"].isin(NON_DISTRETTI)]
    ultimo_mese = str(origine_valida["Mese"].max()) if not origine_valida.empty else None
    mesi_incompleti = _mesi_incompleti(risultato.volumi_distretto_mese)
    ultimo_incompleto = ultimo_mese in mesi_incompleti
    giorni_mese = _giorni_periodo(ultimo_mese)

    segnalazioni = pd.concat([
        risultato.anomalie_metodo_b[["DISTRETTO"]].rename(columns={"DISTRETTO": "Codice Distretto"}),
        risultato.cessate_con_stima_finale[["Distretto"]].rename(columns={"Distretto": "Codice Distretto"}),
    ], ignore_index=True)["Codice Distretto"].value_counts()
    utenze = risultato.utenze_per_distretto.set_index("Codice Distretto")["Totale Utenze"]

    righe_distretti = []
    codici = sorted(set(origine_valida["Codice Distretto"]) | set(utenze.index) - set(NON_DISTRETTI))
    for codice in codici:
        r = origine_valida[(origine_valida["Codice Distretto"] == codice) & (origine_valida["Mese"].astype(str) == ultimo_mese)]
        reale, prov, interp = (float(r[c].sum()) for c in ("Reale (m3)", "Provvisorio (m3)", "Interpolato (m3)"))
        totale = reale + prov + interp
        pct_prov = prov / totale * 100 if totale else 0
        pct_interp = interp / totale * 100 if totale else 0
        comune_ufficiale = elenco["comune_ufficiale"].get(codice, "")
        righe_distretti.append({
            "codice": codice,
            "nome": elenco["nome_distretto"].get(codice) or codice,
            "volume": round(totale, 2),
            "ls": round(_litri_secondo(totale, giorni_mese), 2),
            "pct_reale": round(reale / totale * 100, 1) if totale else 0,
            "pct_provvisorio": round(pct_prov, 1),
            "pct_interpolato": round(pct_interp, 1),
            "stato": _stato_dato(pct_prov, pct_interp, ultimo_incompleto) if totale else None,
            "utenze": int(utenze.get(codice, 0)),
            "n_segnalazioni": int(segnalazioni.get(codice, 0)),
            "condiviso": bool(comune_ufficiale) and comune_ufficiale != comune_trovato,
        })

    # Ultime righe della tabella (richiesta da Daniele il 21/09/2026): NODMA =
    # utenza fuori distretto, ND = distretto non indicato ("*" o vuoto, oppure
    # codice anomalo). Volume dell'ultimo mese come le altre righe; utenze =
    # attive oggi (da statistiche_classe_uso, l'unica vista che le conta).
    # Restano FUORI da righe_distretti: non entrano in KPI, mappa e heatmap,
    # che sono solo sui distretti veri (come Import_WMS).
    stat = risultato.statistiche_classe_uso
    righe_fuori_distretto = []
    for codice, nome in (("NODMA", "Fuori distretto"), ("ND", "Distretto non indicato")):
        r = origine[(origine["Codice Distretto"] == codice) & (origine["Mese"].astype(str) == ultimo_mese)]
        reale, prov, interp = (float(r[c].sum()) for c in ("Reale (m3)", "Provvisorio (m3)", "Interpolato (m3)"))
        totale = reale + prov + interp
        utenze_att = stat.loc[stat["Codice Distretto"] == codice, "Utenze Attive Oggi"].sum()
        if not totale and not utenze_att:
            continue
        righe_fuori_distretto.append({
            "codice": codice,
            "nome": nome,
            "volume": round(totale, 2),
            "ls": round(_litri_secondo(totale, giorni_mese), 2),
            "pct_reale": round(reale / totale * 100, 1) if totale else 0,
            "pct_provvisorio": round(prov / totale * 100, 1) if totale else 0,
            "pct_interpolato": round(interp / totale * 100, 1) if totale else 0,
            "utenze": int(utenze_att),
        })

    # KPI: ultimo mese, ultimo trimestre, variazione sullo stesso mese
    # dell'anno prima (solo se presente in archivio), utenze e segnalazioni.
    volume_mese = sum(d["volume"] for d in righe_distretti)
    variazione = None
    if ultimo_mese:
        mese_prec = f"{int(ultimo_mese[:4]) - 1}{ultimo_mese[4:]}"
        prec = origine_valida[origine_valida["Mese"].astype(str) == mese_prec]
        vol_prec = float(prec[["Reale (m3)", "Provvisorio (m3)", "Interpolato (m3)"]].sum().sum())
        if vol_prec:
            variazione = round((volume_mese - vol_prec) / vol_prec * 100, 1)
    # Solo trimestri completi: l'ultimo puo' contenere un mese solo (parziale).
    trimestri = risultato.volumi_distretto_trimestre
    trimestri = trimestri[trimestri["Completo"] == "Sì"]
    trimestre_kpi = None
    if not trimestri.empty:
        ultimo_trim = trimestri["Trimestre"].max()
        t = trimestri[trimestri["Trimestre"] == ultimo_trim]
        trimestre_kpi = {
            "trimestre": ultimo_trim,
            "volume": round(float(t["Volume Fatturato (m3)"].sum()), 2),
            "provvisorio": bool((t["Contiene Stime Provvisorie"] == "Sì").any()),
        }

    # Mini-mappa: solo i distretti di questo comune (elenco ufficiale + quelli
    # presenti nei suoi dati, anche se condivisi).
    geojson, _ = _geojson_per_mappa()
    codici_comune = set(codici) | set(elenco.index[elenco["comune_ufficiale"] == comune_trovato])
    geojson["features"] = [
        f for f in geojson.get("features", [])
        if f["properties"].get("codice_distretto") in codici_comune
    ]

    mesi_disponibili = sorted(str(m) for m in risultato.volumi_utenza_mese["Mese"].unique())
    return templates.TemplateResponse(request, "comune.html", {
        "request": request,
        "pagina_attiva": "mappa",
        "comuni_disponibili": _comuni_disponibili(),
        "comune_selezionato": comune_trovato,
        "comune": comune_trovato,
        "distretto_evidenziato": distretto.strip().upper() if distretto else None,
        "ultimo_mese": ultimo_mese,
        "stato_comune": _stato_dato(
            sum(d["volume"] * d["pct_provvisorio"] / 100 for d in righe_distretti) / volume_mese * 100 if volume_mese else 0,
            sum(d["volume"] * d["pct_interpolato"] / 100 for d in righe_distretti) / volume_mese * 100 if volume_mese else 0,
            ultimo_incompleto,
        ) if volume_mese else None,
        "mesi_incompleti": mesi_incompleti,
        "volume_mese": round(volume_mese, 2),
        "ls_mese": round(_litri_secondo(volume_mese, giorni_mese), 2),
        "giorni_mese": giorni_mese,
        "ls_trimestre": round(_litri_secondo(trimestre_kpi["volume"], _giorni_periodo(trimestre_kpi["trimestre"])), 2) if trimestre_kpi else None,
        "variazione": variazione,
        "trimestre_kpi": trimestre_kpi,
        "utenze_totali": sum(d["utenze"] for d in righe_distretti),
        "n_segnalazioni": sum(d["n_segnalazioni"] for d in righe_distretti),
        "righe_distretti": righe_distretti,
        "righe_fuori_distretto": righe_fuori_distretto,
        "geojson": geojson,
        "origine_mensile": _tabella_json(origine),
        "classe_uso": _tabella_json(risultato.statistiche_classe_uso),
        "stato_chiusura": _tabella_json(risultato.stato_chiusura_mesi),
        "mesi_disponibili": mesi_disponibili,
    })


# ---------------------------------------------------------------------------
# Prese da associare a un distretto (tab "Prese", Daniele 24/09/2026) — la
# logica e' in app/prese.py. Le conferme servono solo al file per Neta: non
# toccano Metodo B ne' Import_WMS.
# ---------------------------------------------------------------------------

def _punti_json(df: pd.DataFrame, colonne: list[str]) -> list[dict]:
    return _tabella_json(df[colonne]) if not df.empty else []


@app.get("/pagine/prese")
def pagina_prese(request: Request, comune: str | None = None, vista: str = "assegnare"):
    """Senza comune: conteggi per comune. Con comune: vista "assegnare"
    (mappa + elenco delle prese NODMA / * / di altro comune / con distretto
    che non torna con la posizione, con proposta
    dalla posizione e conferma), vista "mappa" (tutte le prese del comune,
    un puntino del colore del suo distretto) o vista "stradario" (via ->
    distretto, generato una tantum dalle prese)."""
    comuni_disponibili = _comuni_disponibili()
    contesto = {
        "request": request,
        "pagina_attiva": "prese",
        "comuni_disponibili": comuni_disponibili,
        "comune_selezionato": comune,
        "comune": None,
        "vista": vista if vista in ("mappa", "stradario") else "assegnare",
        "ricostruzione": dict(prese.STATO_RICOSTRUZIONE),
        "distanza_max": prese.DISTANZA_MAX_PROPOSTA_M,
    }
    if not comune:
        riepilogo = [] if contesto["ricostruzione"]["in_corso"] else prese.riepilogo_comuni(comuni_disponibili)
        # Lista di lavoro: prima i comuni con piu' da confermare, in fondo quelli finiti.
        contesto["riepilogo"] = sorted(riepilogo, key=lambda r: (r["aperti"] == 0, -r["aperti"], r["comune"]))
        contesto["totali"] = {
            "aperti": sum(r["aperti"] for r in riepilogo), "sicure": sum(r["sicure"] for r in riepilogo),
            "confermati": sum(r["confermati_tot"] for r in riepilogo),
            "comuni_completi": sum(1 for r in riepilogo if r["aperti"] == 0), "comuni": len(riepilogo),
            "coordinate": sum(r["coordinate"] for r in riepilogo), "coordinate_inviate": sum(r["coordinate_inviate"] for r in riepilogo),
        }
        t = contesto["totali"]
        t["percentuale"] = round(100 * t["confermati"] / (t["confermati"] + t["aperti"])) if (t["confermati"] + t["aperti"]) else 100
        contesto["invii"] = prese.elenco_invii()
        contesto["puo_modificare"] = request.state.utente["ruolo"] in ("editor", "admin")
        return templates.TemplateResponse(request, "prese.html", contesto)

    trovato = next((c for c in comuni_disponibili if c.strip().upper() == comune.strip().upper()), None)
    if not trovato:
        raise HTTPException(status_code=404, detail=f"Nessun archivio trovato per il comune '{comune}'")
    contesto.update(comune=trovato, comune_selezionato=trovato)

    if contesto["vista"] == "stradario":
        s = stradario.carica(trovato)
        osm = prese.distretti_osm_per_via(trovato, sorted(set(s["via"]))) if not s.empty else {}
        s["osm_nome"] = [osm.get(v, ("", ""))[0] for v in s["via"]]
        s["osm_distretti"] = [osm.get(v, ("", ""))[1] for v in s["via"]]
        contesto.update(
            puo_modificare=request.state.utente["ruolo"] in ("editor", "admin"),
            stradario=[
                {k: (None if pd.isna(v) else (int(v) if isinstance(v, float) else v)) for k, v in r.items()}
                for r in s.to_dict("records")
            ],
        )
        return templates.TemplateResponse(request, "prese.html", contesto)

    tutte = prese.prese_comune(trovato)
    validi = tutte[tutte["COORD_VALIDE"]] if not tutte.empty else tutte
    if validi.empty:
        geojson = {"type": "FeatureCollection", "features": []}
    else:
        geojson = prese.geojson_attorno(validi["LAT"].min(), validi["LAT"].max(), validi["LON"].min(), validi["LON"].max())
    elenco = motore_calcolo.carica_mappa_distretti_df()
    del_comune = elenco[
        (elenco["comune_ufficiale"] == trovato)
        | elenco["comuni_associabili"].str.split(";").apply(lambda l: trovato in [c.strip() for c in l])
    ]
    contesto.update(
        geojson=geojson,
        distretti_comune=sorted(del_comune["codice_distretto"]),
        distretti_tutti=sorted(prese.distretti_noti()),
        nomi_distretti=dict(zip(elenco["codice_distretto"], elenco["nome_distretto"])),
        comuni_distretti=dict(zip(elenco["codice_distretto"], elenco["comune_ufficiale"])),
        puo_modificare=request.state.utente["ruolo"] in ("editor", "admin"),
    )
    if contesto["vista"] == "mappa":
        # Array compatti (non dict con i nomi dei campi): Voghera ha ~9.000
        # prese e la pagina pesava 2,7 MB. Ordine: vedi PUNTI in prese.html.
        conferme = prese.conferme_comune(trovato)
        contesto["punti"] = [
            [r.DP, r.INDIRIZZO, r.SERVIZI, r.N_SERVIZI, r.DISTRETTO, r.DISTRETTO_PRINCIPALE, r.MOTIVO,
             round(float(r.LAT), 6), round(float(r.LON), 6), r.CHIAVE,
             conferme.get(r.CHIAVE, {}).get("DISTRETTO", ""), conferme.get(r.CHIAVE, {}).get("ORIGINE", ""),
             r.D_CIVICO, r.D_STRADARIO, r.D_OSM]
            for r in validi.itertuples(index=False)
        ]
        contesto["n_totale"] = len(tutte)
        contesto["n_senza_coord"] = len(tutte) - len(validi)
    else:
        da_fare = prese.prese_da_assegnare(trovato)
        contesto["righe"] = _punti_json(da_fare, [
            "CHIAVE", "DP", "INDIRIZZO", "CAP", "SERVIZI", "N_SERVIZI", "DISTRETTO", "MOTIVO",
            "LAT", "LON", "COORD_VALIDE", "PROPOSTA", "DISTANZA_M", "CONFERMATO", "CONFERMATO_DA",
            "CONFERMATO_IL", "RECEPITO", "DISTRETTO_VIA", "N_INVII", "ULTIMO_INVIO", "PROPOSTA_DA", "VALIDATA", "FONTI", "CONCORDI", "ORIGINE", "NOTA",
            "LAT_SIS", "LON_SIS", "FONTE_SIS", "AFF_SIS", "NODMA_CONTRO_DA", "OMONIMA", "NON_DECIDONO",
        ])
    return templates.TemplateResponse(request, "prese.html", contesto)


@app.post("/prese/assegna")
async def prese_assegna(request: Request):
    """Conferma (o toglie, con distretto vuoto) il distretto di una o piu'
    prese: JSON {"comune": ..., "voci": [{"chiave": ..., "distretto": ...}]}.
    Solo editor/admin (middleware: ogni POST)."""
    corpo = await request.json()
    comune = str(corpo.get("comune", "")).strip()
    voci = [{"chiave": str(v.get("chiave", "")).strip(), "distretto": str(v.get("distretto", "")),
             "validata": bool(v.get("validata")), "origine": str(v.get("origine", "") or ""), "nota": str(v.get("nota", "") or "")}
            for v in corpo.get("voci", [])]
    voci = [v for v in voci if v["chiave"]]
    if not comune or not voci:
        raise HTTPException(status_code=400, detail="Comune o punti di erogazione mancanti.")
    try:
        salvate, tolte = prese.salva_assegnazioni(comune, voci, request.state.utente["username"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    dettaglio = ", ".join(
        f"{v['chiave']}→{v['distretto'].strip().upper() or '(tolta)'}"
        f"{' (' + prese.ORIGINI.get('mantieni' if v['validata'] else v['origine'] or 'proposta', '') + ')'}"
        for v in voci[:20])
    if voci and voci[0]["nota"]:
        dettaglio += f" — nota: {voci[0]['nota']}"
    if len(voci) > 20:
        dettaglio += f" … (+{len(voci) - 20})"
    auth.registra(
        request.state.utente["username"], "prese_assegna",
        f"{comune}: {salvate} confermate, {tolte} tolte — {dettaglio}", accessi.ip_client(request),
    )
    # Le conferme entrano nel calcolo: ricalcolo del comune rimandato di
    # qualche minuto (una presa alla volta) o di pochi secondi (tante insieme).
    _programma_ricalcolo(comune, RITARDO_RICALCOLO_GRUPPO_S if len(voci) > 1 else RITARDO_RICALCOLO_S)
    return {"salvate": salvate, "tolte": tolte}


@app.post("/prese/stradario")
async def prese_stradario(request: Request):
    """Genera (o rigenera) lo stradario via -> distretto di un comune dalla
    posizione delle sue prese: JSON {"comune": ...}. Solo editor/admin."""
    corpo = await request.json()
    comune = str(corpo.get("comune", "")).strip().upper()
    if comune not in [c.strip().upper() for c in _comuni_disponibili()]:
        raise HTTPException(status_code=400, detail=f"Comune '{comune}' non trovato.")
    try:
        nuovo = await run_in_threadpool(prese.genera_stradario, comune)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    a_cavallo = int((nuovo["distretto"] == "").sum())
    auth.registra(
        request.state.utente["username"], "prese_stradario",
        f"{comune}: {nuovo['via'].nunique()} vie, {len(nuovo)} righe, {a_cavallo} a cavallo", accessi.ip_client(request),
    )
    return {"vie": int(nuovo["via"].nunique()), "righe": len(nuovo), "a_cavallo": a_cavallo}


@app.post("/prese/invio")
async def prese_invio(request: Request):
    """Registra un invio a Neta e scarica il file da mandare: form con tipo
    ('distretti' = conferme non ancora recepite, 'coordinate' = coordinate
    da verificare) e comune (vuoto = tutti). Solo editor/admin."""
    form = await request.form()
    tipo = str(form.get("tipo", ""))
    comune = str(form.get("comune", "")).strip().upper()
    comuni = [comune] if comune else _comuni_disponibili()
    try:
        id_invio, n, contenuto = await run_in_threadpool(prese.registra_invio, tipo, comuni, request.state.utente["username"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    auth.registra(
        request.state.utente["username"], "prese_invio_neta",
        f"invio {id_invio}, {tipo}, {comune or 'tutti i comuni'}: {n} punti di erogazione", accessi.ip_client(request),
    )
    nome = f"neta_{tipo}_{comune.replace(' ', '_') or 'tutti'}_{time.strftime('%Y%m%d')}_invio{id_invio}.xlsx"
    return Response(
        content=contenuto,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@app.get("/mappe/confini-comuni.geojson")
def confini_comuni_geojson():
    """Confini dei comuni ISTAT per lo strato attivabile delle mappe (vedi
    static/confini_comuni.js); il file e' in project_docs/."""
    if not prese.PERCORSO_CONFINI_COMUNI.exists():
        raise HTTPException(status_code=404, detail="Confini dei comuni non disponibili.")
    return Response(
        content=prese.PERCORSO_CONFINI_COMUNI.read_bytes(), media_type="application/geo+json",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/prese/coordinate")
def prese_coordinate(request: Request, comune: str = ""):
    """Excel per Neta delle prese con coordinate da verificare (mancanti,
    fuori comune, lontane dal distretto della via). Senza comune: tutti."""
    comuni = [comune.strip().upper()] if comune else _comuni_disponibili()
    contenuto = prese.esporta_coordinate_excel(comuni)
    nome = f"prese_coordinate_da_verificare_{comune.strip().replace(' ', '_') or 'tutti'}_{time.strftime('%Y%m%d')}.xlsx"
    auth.registra(request.state.utente["username"], "prese_coordinate", comune or "tutti i comuni", accessi.ip_client(request))
    return Response(
        content=contenuto,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@app.get("/prese/gis")
def prese_gis(request: Request, comune: str = ""):
    """Excel per il GIS (non per Neta): DP gia' confermati in un distretto ma fuori dal suo confine,
    probabili confini da rivedere. Senza comune: tutti."""
    comuni = [comune.strip().upper()] if comune else _comuni_disponibili()
    contenuto = prese_gis_modulo.esporta_gis_excel(comuni)
    nome = f"prese_fuori_confine_per_GIS_{comune.strip().replace(' ', '_') or 'tutti'}_{time.strftime('%Y%m%d')}.xlsx"
    auth.registra(request.state.utente["username"], "prese_gis", comune or "tutti i comuni", accessi.ip_client(request))
    return Response(
        content=contenuto,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@app.get("/prese/vie")
async def prese_vie(request: Request, comune: str = ""):
    """Excel di controllo dei nomi: le vie e le frazioni di Neta che non si abbinano a OpenStreetMap o ad ANNCSU
    (o si abbinano in modo ambiguo), con i candidati vicini. Non modifica niente. Senza comune: tutti (la prima
    volta ci vuole circa un minuto, poi il risultato e' in cache)."""
    comuni = [comune.strip().upper()] if comune else _comuni_disponibili()
    contenuto = await run_in_threadpool(prese_vie_modulo.esporta_vie_excel, comuni)
    nome = f"prese_vie_da_verificare_{comune.strip().replace(' ', '_') or 'tutti'}_{time.strftime('%Y%m%d')}.xlsx"
    auth.registra(request.state.utente["username"], "prese_vie", comune or "tutti i comuni", accessi.ip_client(request))
    return Response(
        content=contenuto,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@app.get("/prese/gis.geojson")
def prese_gis_geojson(request: Request, comune: str = ""):
    """Gli stessi punti in GeoJSON, da aprire in QGIS insieme ai poligoni dei distretti."""
    comuni = [comune.strip().upper()] if comune else _comuni_disponibili()
    contenuto = prese_gis_modulo.geojson_gis(comuni)
    nome = f"prese_fuori_confine_per_GIS_{comune.strip().replace(' ', '_') or 'tutti'}_{time.strftime('%Y%m%d')}.geojson"
    auth.registra(request.state.utente["username"], "prese_gis_geojson", comune or "tutti i comuni", accessi.ip_client(request))
    return Response(
        content=contenuto, media_type="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@app.get("/prese/via")
def prese_via(comune: str, indirizzo: str):
    """Dove sta la via di un indirizzo Neta: il tracciato OSM (lat/lon) e, se
    c'e', il punto del civico ANNCSU. Serve alla pagina Prese per confrontare
    la coordinata di Neta con la via (Daniele, 30/09/2026). Sola lettura."""
    via, civico = stradario.normalizza_indirizzo(indirizzo)
    osm = vie_osm.vie_comune(comune)
    nome = vie_osm.abbina([via], list(osm)).get(via, "") if osm and via else ""
    civ = anncsu.civici_per_indirizzo(comune, [indirizzo]).iloc[0]
    punto_civico = None
    if pd.notna(civ["CIV_LAT"]) and pd.notna(civ["CIV_LON"]):
        punto_civico = {"lat": float(civ["CIV_LAT"]), "lon": float(civ["CIV_LON"]), "civico": civico}
    return {
        "nome_osm": nome,
        "tratti": [[[float(y), float(x)] for x, y in tr] for tr in osm[nome]] if nome else [],
        "civico": punto_civico,
    }


@app.get("/prese/esporta")
def prese_esporta(request: Request, comune: str = "", tutte: int = 0):
    """Excel una riga per presa. Di default solo le conferme (il file per
    Neta); ?tutte=1 l'elenco completo con le proposte. Senza comune: tutti."""
    comuni = [comune.strip().upper()] if comune else _comuni_disponibili()
    contenuto = prese.esporta_excel(comuni, solo_confermate=not tutte)
    nome = f"prese_{'elenco' if tutte else 'distretti_confermati'}_{comune.strip().replace(' ', '_') or 'tutti'}_{time.strftime('%Y%m%d')}.xlsx"
    auth.registra(
        request.state.utente["username"], "prese_esporta",
        f"{comune or 'tutti i comuni'} ({'elenco completo' if tutte else 'confermate'})", accessi.ip_client(request),
    )
    return Response(
        content=contenuto,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )
