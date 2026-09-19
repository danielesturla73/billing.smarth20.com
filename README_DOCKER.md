# Fatturazione Utenze — container Docker "billing"

Nota per chi continua lo sviluppo (Claude Code) e per Daniele: cosa c'è già, cosa manca, come si avvia. **Aggiornato al 17/09/2026; sezione Accessi aggiunta il 19/09/2026.**

Cartella sul VPS: **`/opt/billing`**. Nome del container: **`billing`** (vedi `docker-compose.yml`). Repository GitHub: **`github.com/danielesturla73/billing.smarth20.com`** (branch `main`, già collegato con `git remote`).

## Stato reale sulla VPS (verificato il 17/09/2026)

- Container `billing`: **su e "healthy"**, porta `8010` pubblicata **solo su 127.0.0.1** (dal 19/09/2026) → `8000` (interna), pubblico su `billing.smarth20.com` (HTTPS via Caddy, che raggiunge il container dalla rete Docker).
- Rete Docker condivisa `rete-interna-idrico`: **collegata** (`billing`, `wms-smarth20`, `caddy`).
- `.env` presente e compilato (token vero, `WMS_API_URL=http://wms-smarth20:80`) — **non ancora usato da nessun endpoint** (il pulsante "a un click" verso WMS SmartH2O non esiste ancora, vedi sotto).
- Repository git **inizializzato e pushato** su GitHub (vedi sopra). `.gitignore`/`.dockerignore` già a posto: niente segreti, dati reali o CSV/DB finiscono in git.

## Cosa c'è nel codice (fatto in questa sessione)

- **`app/main.py`** — servizio FastAPI con:
  - `GET /health`, `GET /` — health-check e stub.
  - `POST /upload` — carica una o più estrazioni Neta H2O (.xlsx), riconosce il comune dalla colonna `LOCALITA` (non dal nome file), aggiorna l'archivio storico. Gestisce comuni nuovi automaticamente.
  - `GET /riepilogo`, `GET /diagnostica` — API JSON (parametro opzionale `?comune=`): riepilogo dei volumi (Import_WMS, trimestrale, per comune) e dettaglio anomalie riga per riga.
  - `GET /pagine/riepilogo`, `GET /pagine/diagnostica` — le **prime pagine web vere** (Jinja2 + `app/templates/`, stile preso da `style-guide-wms-smarth2o.md`: header scuro con filtro Comune, tab di navigazione, KPI, tabelle). Nessun grafico ancora (specifiche §6.2, non confermate da Daniele).
- **`app/database.py`** (nuovo) — archivio storico su **SQLite** (`archivio/archivio.db`, un solo file con tutti i comuni, indicizzato, WAL mode), ha sostituito i CSV per-comune usati inizialmente. `motore_calcolo.carica_archivio`/`aggiorna_archivio` (CSV) restano nel codice solo per uso da riga di comando (`python -m app.motore_calcolo file.xlsx`), non più usati dall'app web.
- **`app/motore_calcolo.py`** — motore di calcolo (Metodo B), **ottimizzato** (~10x più rapido: il ciclo per-utenza e la ripartizione mensile erano il collo di botiglia; validato confrontando tutte le tabelle prodotte prima/dopo su dati reali, nessuna differenza). **Bug corretto**: `trova_cessate_con_stima_finale`/`trova_utenze_scomparse` ora usano la stessa regola di priorità-stesso-giorno di `calcola_periodi_metodo_b` (prima non erano coerenti tra loro — vedi commento `_ordina_priorita_stessa_data` nel codice per i dettagli).
- **`scripts/migra_csv_a_sqlite.py`** — migrazione one-off CSV→SQLite, già eseguita sui dati reali (i due CSV originali restano su disco come backup, non più letti dall'app).
- **`app/templates/`, `app/static/`** — template Jinja2 e CSS (palette/tipografia da `style-guide-wms-smarth2o.md`).

## Cosa manca lato applicativo (per la prossima sessione)

1. **Grafici** — proposta scritta in `specifiche-applicativo-fatturazione-utenze.md` §6.2, **non ancora confermata da Daniele**: non costruirli senza chiedere prima (che tipo di grafico, quali pagine, Chart.js come da style guide).
2. **Il pulsante "a un click"** verso WMS SmartH2O — endpoint che chiama l'API di WMS SmartH2O sulla rete Docker interna (`WMS_API_URL`, `INTERNAL_API_TOKEN` già in `.env`) e manda i dati di Import_WMS. Deve gestire l'upsert (i valori possono essere provvisori, vedi §4.8 del riepilogo di progetto).
3. **Token interno verso WMS SmartH2O** (`INTERNAL_API_TOKEN`): il login degli utenti c'è (vedi "Accessi"), ma la chiamata a WMS non esiste ancora. Lato WMS l'autenticazione va imposta dal server, non solo dal frontend.
4. **Upload dalla pagina web** — oggi `/upload` è solo un'API (va chiamata con `curl -F` o Postman); manca la schermata di caricamento file vera.

## Accessi (19/09/2026)

Login separato da WMS SmartH2O (le due app non condividono database). Codice in `app/auth.py` (utenti, sessioni, registro, hash scrypt) e `app/accessi.py` (middleware e pagine).

- **Ruoli a scalare**: viewer (consulta) < editor (upload, distretti, mappa, in futuro invio a WMS) < admin (utenti e registro). Un account per persona, così il registro dice chi ha fatto cosa.
- **Default deny**: il middleware richiede login su ogni rotta (eccetto `/health`, `/static`, `/login`, `/logout`, `/setup`, `/recupero`), editor per ogni POST, admin per `/admin/*`. Una rotta nuova è protetta anche senza pensarci. Il controllo è solo lato server.
- **Registro azioni** in `/admin/registro` (tabella `registro_azioni` in `archivio.db`): login e login falliti, upload, distretti/confini, gestione utenti.
- **Password**: solo hash, non recuperabili, minimo 10 caratteri. Dopo 5 tentativi falliti in 10 minuti (per username o IP) si risponde 429.
- **Prima configurazione**: senza utenti, `/setup` crea il primo admin, ma solo con `SETUP_CODE` del `.env`. Appena esiste un utente la pagina si chiude.
- **Password admin dimenticata**: aggiungere `SETUP_CODE=...` al `.env`, `docker compose up -d` (un semplice restart non rilegge il `.env`), usare `/recupero`, poi togliere la riga e ricreare il container. Senza la riga `/setup` e `/recupero` non esistono. Meglio avere due admin.
- La porta 8010 deve restare su 127.0.0.1: il limite tentativi si fida di `X-Forwarded-For`, che con la porta aperta si falsificherebbe.

## Ambiguità nota, non affrontata (bassa priorità)

Nel motore di calcolo, ~33 casi (su Belgioioso) di due letture della stessa utenza con la stessa data e la stessa priorità (nessuna regola esistente le distingue) causano una variazione trascurabile (~0,004% del volume totale) a seconda dell'ordine con cui le legge il database. Non è un problema introdotto da questa sessione (era già latente nei CSV), è stato solo scoperto migrando a SQLite. Daniele ha scelto di non affrontarlo ora — se dovesse servire, i casi sono trovabili con lo script di confronto usato in questa sessione (vedi conversazione, non salvato come file).

## Come si riavvia

```bash
cd /opt/billing
docker compose up -d --build
curl http://localhost:8010/health
# -> {"status":"ok","servizio":"billing"}
docker ps --filter name=billing
docker network inspect rete-interna-idrico
```

Per rifare la migrazione CSV→SQLite da capo (solo se serve, es. archivio.db corrotto o cancellato per errore — i CSV originali restano su disco):

```bash
docker compose run --rm billing python -m scripts.migra_csv_a_sqlite
```

## Git / GitHub

Repo già inizializzato, remote già collegato (`origin` → `github.com/danielesturla73/billing.smarth20.com`, branch `main`). Chiave SSH già autorizzata sulla VPS (`~/.ssh/id_ed25519_wms`, condivisa con il progetto WMS SmartH2O). Ciclo normale per i prossimi commit:

```bash
cd /opt/billing
git add <file modificati>
git commit -m "descrizione"
git push
```

## Struttura

```
.
├── app/
│   ├── __init__.py
│   ├── main.py              # servizio FastAPI: health, upload, riepilogo/diagnostica (JSON + pagine HTML)
│   ├── motore_calcolo.py    # motore di calcolo (Metodo B), ottimizzato
│   ├── database.py          # archivio storico su SQLite
│   ├── templates/           # pagine Jinja2 (base, riepilogo, diagnostica)
│   └── static/              # CSS (stile WMS SmartH2O)
├── scripts/
│   └── migra_csv_a_sqlite.py  # migrazione one-off, già eseguita
├── archivio/                 # archivio.db (SQLite) + i due CSV originali (backup, non più letti)
├── input/                    # estrazioni Excel caricate (montato come volume)
├── output/                   # Excel generati da esporta_excel() (montato come volume, non ancora collegato a un endpoint)
├── project_docs/             # specifiche/decisioni di progetto (leggere PRIMA di modificare la logica di calcolo)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── CLAUDE.md                 # guida per Claude Code su architettura/comandi
└── .dockerignore / .gitignore
```
