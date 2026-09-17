# Fatturazione Utenze — container Docker "billing" (scheletro minimo)

Nota per chi continua lo sviluppo (Claude Code) e per Daniele: cosa c'è già, cosa manca, come si avvia.

Cartella sul VPS: **`/opt/billing`**. Nome del container: **`billing`** (vedi `docker-compose.yml`).

## Stato reale sulla VPS (verificato il 16/09/2026 — non più solo un piano, è già così)

- Container `billing`: **su e "healthy"**, build riuscito al primo tentativo, porta `8010` (host) → `8000` (interna).
- Rete Docker condivisa `rete-interna-idrico`: **creata**, e `billing` è già collegato.
- Container di WMS SmartH2O (nome reale: **`wms-smarth20`**, non "wms-backend") è **già collegato** alla stessa rete — raggiungibile da `billing` all'indirizzo `http://wms-smarth20:80` (attenzione: porta **80**, non 8080 — quella è solo la porta pubblicata verso l'host/Caddy).
- Confermato anche un container `caddy` sulla stessa VPS (reverse proxy pubblico, porte 80/443) — non ancora collegato alla rete condivisa, non serve finché non esiste un'interfaccia web vera da esporre (vedi punto 3 sotto).
- `.env` già presente e compilato con un token generato per davvero (non più il segnaposto) e `WMS_API_URL=http://wms-smarth20:80`.

## Cosa c'è nel codice

- Un servizio web minimo (FastAPI) che risponde su `/health` e `/` — non fa ancora nulla di utile, serve solo a verificare che il container sia su e raggiungibile.
- Il motore di calcolo vero (`app/motore_calcolo.py`, copia di `motore_calcolo.py` in radice) — tutta la logica del Metodo B, archivio storico, statistiche per distretto, ecc. È importato da `app/main.py` solo per verificare che carichi senza errori all'avvio; non è ancora collegato a nessun endpoint.
- `Dockerfile` e `docker-compose.yml`, già collegati alla rete Docker condivisa con WMS SmartH2O (vedi il project doc `riepilogo-progetto-wms-smarth2o.md`, sezione 4.9, per il perché di queste scelte).

## ⚠️ Da portare qui prima di far partire Claude Code

Questa cartella di codice NON include ancora, di suo:
- **I tre documenti di specifica del progetto** (`specifiche-applicativo-fatturazione-utenze.md`, `riepilogo-progetto-wms-smarth2o.md`, `style-guide-wms-smarth2o.md`) — servono a Claude Code per non reinventare decisioni già prese (es. perché solo Metodo B, perché niente dotazione pro-capite, lo stile grafico da seguire). Vanno messi in una cartella `project_docs/` qui dentro.
- **Dati veri** per testare: l'archivio storico (`archivio/archivio_letture.csv`, `archivio/archivio_letture_mortara.csv`) e le estrazioni Excel originali (`input/*.xlsx`) — oggi le cartelle `archivio/`, `input/`, `output/` sulla VPS sono vuote.

Se hai ricevuto da Daniele l'archivio `billing-docs-e-dati.zip`, ti basta spacchettarlo dentro `/opt/billing` (sovrascrive le cartelle vuote con i dati veri):

```bash
cd /opt/billing
unzip -o billing-docs-e-dati.zip
docker compose restart   # per far ripartire il container con l'archivio popolato nei volumi
```

## Cosa manca lato applicativo (per Claude Code)

1. **Upload delle estrazioni Neta H2O** — oggi i file Excel vengono passati a mano; serve un endpoint di upload che li salvi e aggiorni l'archivio (vedi `aggiorna_archivio()` in `motore_calcolo.py`, già pronta).
2. **Le pagine vere** — dashboard, riepiloghi, tutto quello che oggi sta nei 17 fogli dell'Excel di output (vedi `specifiche-applicativo-fatturazione-utenze.md`, sezione 3, per l'elenco completo).
3. **Il pulsante "a un click"** verso WMS SmartH2O — un endpoint che chiama l'API di WMS SmartH2O sulla rete Docker interna (usando `WMS_API_URL` e `INTERNAL_API_TOKEN` da `.env`) e gli manda i dati del foglio Import_WMS. Per ora resta un invio con conferma umana, non uno scheduler automatico (vedi 4.9 del riepilogo di progetto per il perché).
4. **Un vero database** — oggi l'archivio è un CSV su disco (`archivio/archivio_letture.csv`); è già stato proposto SQLite (vedi documento di specifica), ma non ancora implementato.
5. **Autenticazione** dell'interfaccia web (chi può caricare file, chi può premere il pulsante di invio).
6. **Grafici** (proposta già scritta, vedi specifiche sezione 6.2) e lo stile grafico coerente con WMS SmartH2O (vedi `style-guide-wms-smarth2o.md`).

## Come si riavvia (i comandi di primo avvio sono già stati fatti — questo è solo per riferimento futuro)

La rete condivisa e il collegamento a WMS SmartH2O sono già a posto (vedi sopra). Per un riavvio, dentro `/opt/billing`:

```bash
docker compose up -d --build
```

Verifica che risponda:

```bash
curl http://localhost:8010/health
# -> {"status":"ok","servizio":"billing"}
```

Verifica che il container si chiami davvero "billing" e sia sulla rete condivisa:

```bash
docker ps --filter name=billing
docker network inspect rete-interna-idrico
```

## Struttura

```
.
├── app/
│   ├── __init__.py
│   ├── main.py            # servizio FastAPI (scheletro minimo)
│   └── motore_calcolo.py  # motore di calcolo (copia di quello in radice)
├── archivio/               # CSV storico delle letture (montato come volume)
├── input/                  # estrazioni Excel caricate (montato come volume)
├── output/                 # Excel generati (montato come volume)
├── project_docs/           # copie locali delle specifiche del progetto Claude
├── motore_calcolo.py       # ⚠️ copia originale in radice, usata finora per lo sviluppo/test in questa sessione Claude — da considerare superata rispetto ad app/motore_calcolo.py una volta che lo sviluppo prosegue nel container
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .dockerignore / .gitignore
```
