"""
Punto d'ingresso del servizio web di Fatturazione Utenze.

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
- l'autenticazione (sia per chi usa l'interfaccia, sia il token interno
  verso WMS SmartH2O, vedi INTERNAL_API_TOKEN in .env.example)
- upload/gestione utenti dalle pagine web (oggi solo via /upload, API)

Il motore di calcolo vero e proprio resta in motore_calcolo.py (Metodo
B, statistiche per distretto, ecc.) — questo file lo importa ma non ne
cambia la logica. La persistenza (SQLite) e' in database.py.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import pandas as pd

from app import database, motore_calcolo

app = FastAPI(
    title="Fatturazione Utenze",
    description="Calcolo dei volumi fatturati per distretto idrico, a partire dalle estrazioni Neta H2O.",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Percorso relativo alla working directory del processo ("input/"),
# coerente con quello gia' usato da motore_calcolo.py e con i volumi
# Docker montati in docker-compose.yml.
INPUT_DIR = Path("input")


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
    """Ora che esiste una pagina vera (/pagine/riepilogo), la radice ci
    rimanda direttamente invece di mostrare un JSON di stato."""
    return RedirectResponse(url="/pagine/riepilogo")


@app.post("/upload")
async def upload_estrazioni(files: list[UploadFile] = File(...)):
    """Carica una o piu' estrazioni Neta H2O (.xlsx) in un solo passaggio.

    Per ogni file: lo salva in input/, riconosce il comune dalla colonna
    LOCALITA dentro il file (non dal nome del file — vedi specifiche,
    sezione 6.0) e lo aggiunge all'archivio storico (SQLite) di quel
    comune, creandolo automaticamente se il comune non era mai stato
    visto. Un file con struttura non valida (colonne mancanti, file non
    Excel, ecc.) viene segnalato con un errore SENZA bloccare il
    caricamento degli altri file del batch.

    Non fa ancora nessun ricalcolo dei volumi: quello serve a un endpoint
    separato, per non ripetere un calcolo pesante ad ogni singolo upload
    quando si caricano piu' file insieme.
    """
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    risultati_file = []
    file_per_comune: dict[str, list[Path]] = {}

    for upload in files:
        nome_originale = upload.filename or "estrazione.xlsx"
        percorso_salvato = INPUT_DIR / nome_originale
        if percorso_salvato.exists():
            # Non sovrascrivere silenziosamente un file gia' presente (es.
            # stesso nome ricaricato per errore): si tiene comunque traccia
            # del nuovo caricamento con un suffisso univoco. La deduplica
            # vera e propria delle LETTURE avviene poi in database.aggiorna_letture,
            # su chiave utenza+data+tipo, non sul nome del file.
            percorso_salvato = INPUT_DIR / (
                f"{percorso_salvato.stem}_{int(time.time())}{percorso_salvato.suffix}"
            )

        percorso_salvato.write_bytes(await upload.read())

        try:
            df = motore_calcolo.carica_estrazione(percorso_salvato)
        except Exception as exc:
            risultati_file.append({
                "file": nome_originale,
                "salvato_come": percorso_salvato.name,
                "comune": None,
                "righe_lette": None,
                "errore": str(exc),
            })
            continue

        comune = motore_calcolo.comune_dominante(df) or "SCONOSCIUTO"
        risultati_file.append({
            "file": nome_originale,
            "salvato_come": percorso_salvato.name,
            "comune": comune,
            "righe_lette": len(df),
            "errore": None,
        })
        file_per_comune.setdefault(comune, []).append(percorso_salvato)

    archivi_aggiornati = []
    for comune, percorsi in file_per_comune.items():
        _, stats = database.aggiorna_letture(percorsi, comune)
        archivi_aggiornati.append({"comune": comune, **stats})

    return {"file": risultati_file, "archivi_aggiornati": archivi_aggiornati}


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


def _risultati_per_comune(comune_filtro: str | None):
    """Ricalcola il Metodo B sull'archivio storico (SQLite) di ogni
    comune, uno alla volta (mai comuni diversi insieme: vedi /riepilogo
    per il perché), e restituisce (comune, risultato) per ognuno. Usato
    sia da /riepilogo sia da /diagnostica, per non duplicare la stessa
    selezione dei comuni noti al database.
    """
    with database.connessione() as conn:
        comuni = database.elenco_comuni(conn)
        if comune_filtro:
            comuni = [c for c in comuni if c.strip().upper() == comune_filtro.strip().upper()]
        for comune in comuni:
            df = database.carica_letture(conn, comune)
            if df.empty:
                continue
            yield comune, motore_calcolo.elabora_dataframe(df)


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
        })

    if comune and not comuni_risultato:
        raise HTTPException(status_code=404, detail=f"Nessun archivio trovato per il comune '{comune}'")

    return {"comuni": comuni_risultato}


def _comuni_disponibili() -> list[str]:
    with database.connessione() as conn:
        return database.elenco_comuni(conn)


@app.get("/pagine/riepilogo")
def pagina_riepilogo(request: Request, comune: str | None = None):
    """Stessi dati di /riepilogo, mostrati come pagina HTML invece che
    JSON grezzo — la vista principale prevista dalle specifiche (sezione
    6.0): filtro Comune nell'header (persistente tra le pagine), KPI in
    alto, tabella Import_WMS sotto. Senza ?comune= mostra un colpo
    d'occhio su tutti i comuni invece del dettaglio.
    """
    comuni_disponibili = _comuni_disponibili()

    if comune:
        trovati = list(_risultati_per_comune(comune))
        if not trovati:
            raise HTTPException(status_code=404, detail=f"Nessun archivio trovato per il comune '{comune}'")
        comune_trovato, risultato = trovati[0]
        trimestri_provvisori = risultato.volumi_distretto_trimestre[
            risultato.volumi_distretto_trimestre["Contiene Stime Provvisorie"] == "Sì"
        ]["Trimestre"].nunique()
        return templates.TemplateResponse(request, "riepilogo.html", {
            "request": request,
            "pagina_attiva": "riepilogo",
            "comuni_disponibili": comuni_disponibili,
            "comune_selezionato": comune_trovato,
            "comune": comune_trovato,
            "warning": risultato.warning,
            "volume_totale": float(risultato.volumi_distretto_mese["Volume Fatturato (m3)"].sum()),
            "n_mesi": int(risultato.volumi_distretto_mese["Mese"].nunique()),
            "trimestri_provvisori": int(trimestri_provvisori),
            "volumi_distretto_mese": _tabella_json(risultato.volumi_distretto_mese),
        })

    riepilogo_comuni = [
        {"comune": c, "volume_totale": float(r.volumi_distretto_mese["Volume Fatturato (m3)"].sum())}
        for c, r in _risultati_per_comune(None)
    ]
    return templates.TemplateResponse(request, "riepilogo.html", {
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

    return templates.TemplateResponse(request, "diagnostica.html", {
        "request": request,
        "pagina_attiva": "diagnostica",
        "comuni_disponibili": comuni_disponibili,
        "comune_selezionato": comune_trovato,
        "comune": comune_trovato,
        "segnalazioni": _tabella_json(risultato.segnalazioni),
        "anomalie_metodo_b": _tabella_json(risultato.anomalie_metodo_b),
        "utenze_scomparse_da_verificare": utenze_scomparse_da_verificare,
        "cessate_con_stima_finale": _tabella_json(risultato.cessate_con_stima_finale),
    })
