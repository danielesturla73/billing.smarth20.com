# Riepilogo di progetto — WMS SmartH2O e Applicativo Fatturazione Utenze
Broni-Stradella Pubblica S.r.l. — Pavia Acque · Ing. Daniele Sturla · Aggiornato settembre 2026

## 1. Cos'è WMS SmartH2O
WMS SmartH2O è il sistema di monitoraggio e bilancio idrico della rete acquedottistica gestita da Broni-Stradella Pubblica S.r.l., nell'ambito del progetto PNRR di distrettualizzazione di Pavia Acque. Integra dati da 142 misuratori LoRaWAN su 22 comuni con dati da RTU GeoSCADA Schneider Electric, con un approccio bottom-up basato sui totalizzatori cumulativi (non sulla portata istantanea).

L'obiettivo finale è calcolare, per ogni distretto idrico e per ogni mese, il bilancio: volumi immessi in rete, volumi consumati dagli utenti (fatturati), e perdite (differenza tra i due).

## 2. Architettura attuale
Il sistema gira su un VPS, in un container Docker unico che contiene:
- PostgreSQL + TimescaleDB — database delle serie temporali (letture dei misuratori)
- FastAPI (Python) — API REST, con uno scheduler interno per i calcoli automatici
- Nginx — serve il frontend e smista le richieste verso l'API

È raggiungibile pubblicamente su wms.smarth20.com, dietro Caddy (reverse proxy) e HTTPS automatico.

## 3. Stato di avanzamento

| Fase | Stato |
|---|---|
| 1 — Infrastruttura (container, database, dominio) | ✅ Completata |
| 2 — Upload: Master Anagrafica | ✅ Completata |
| 2 — Upload: Configurazione Distretti | ✅ Completata |
| 2 — Upload: ZIP LoRa (anagrafica + storici) | ✅ Completata |
| 2 — Upload: ZIP RTU GeoSCADA | ⏳ Da fare |
| 3 — Calcoli e bilanci idrici | ⏳ Da fare |
| 4 — Scheduler automatico | ⏳ Da fare |
| 5 — Frontend integrato | ⏳ Da fare |

## 4. Il nuovo applicativo: Fatturazione Utenze

È un secondo programma, indipendente, sviluppato per ora in locale (dentro questo progetto Claude, non ancora collegato al server WMS). Legge i dati di fatturazione dal CRM Neta H2O e calcola, per ogni distretto idrico, il volume totale consumato/fatturato dalle utenze in un dato mese.

Il suo output — un file — verrà in un secondo momento caricato manualmente in WMS SmartH2O, per confrontare il volume fatturato con il volume netto misurato dai misuratori (ingressi meno uscite del distretto), ottenendo così le perdite idriche (NRW, Non-Revenue Water):

```
PERDITE = VOLUME NETTO MISURATO − VOLUME FATTURATO
```

### 4.1 Stato di avanzamento del prototipo
Il prototipo (`motore_calcolo.py`, Python) è già funzionante end-to-end e testato su dati reali di due comuni:

- **Belgioioso** (distretti DBLG01/02/03): 2 estrazioni caricate, finestra affidabile luglio 2025 – aprile 2026 (10 mesi).
- **Mortara** (distretti DMR01/03/04/05/06/08/09/10): 3 estrazioni caricate, che insieme coprono un **anno intero consecutivo** (luglio 2025 – giugno 2026, 4 trimestri completi). Totale comune: 1.580.066 m³/anno (Metodo A). È stato anche testato con successo il caso di una singola utenza (verifica di dettaglio, non solo aggregati).

Il motore genera automaticamente, per ogni comune, un file Excel di 16 fogli con: i volumi per distretto e per mese/trimestre, il totale dell'intero comune (distretti + case sparse + anomalie), il conteggio delle utenze attive e per classe d'uso, le segnalazioni di anomalie anagrafiche, e — punto importante per l'affidabilità del dato — due calcoli paralleli e indipendenti del volume (vedi 4.4), con un foglio di confronto tra i due.

Non è ancora collegato al server WMS SmartH2O: oggi produce solo il file Excel/CSV, che va caricato a mano.

### 4.2 Il database è già pronto a riceverlo
Nello schema di WMS SmartH2O esiste già una tabella pensata esattamente per questo output, chiamata `district_billed`:

| Colonna | Significato | Esempio |
|---|---|---|
| month | Primo giorno del mese di riferimento | 2026-07-01 |
| district_id | Riferimento interno al distretto | (assegnato dal DB) |
| vol_billed_m3 | Volume fatturato nel mese, in m³ | 1250.75 |
| source | Origine del dato | "Neta H2O" |
| note | Note libere (opzionale) | |

### 4.3 Formato del file di output
Dato che `district_id` è un numero interno al database (non noto all'applicativo locale), il file prodotto usa il CODICE del distretto (lo stesso codice usato nel file Configurazione Distretti, es. DBLG03, DMR10) invece dell'ID numerico. In WMS SmartH2O, al momento del caricamento, sarà il sistema a tradurre il codice nell'ID interno corretto.

Il prototipo produce già questo formato nel foglio **"Import_WMS"** dell'Excel di output (una riga per distretto per mese):

| Mese | Codice Distretto | Volume Fatturato (m³) |
|---|---|---|
| 2026-07 | DBLG03 | 1250.75 |
| 2026-07 | DMR10 | 980.20 |

Gli altri 15 fogli (dettaglio trimestrale, utenze attive, segnalazioni, confronto metodi, ecc.) sono materiale di lavoro/controllo per Daniele, non destinati al caricamento diretto in WMS.

### 4.4 ✅ Risolto (15/09/2026): quale volume caricare in `district_billed` — Metodo B
Il CONSUMO dichiarato nel file Neta H2O non sempre coincide con la differenza fisica di lettura del contatore (vedi analisi dettagliata nel documento tecnico di specifica). Per questo il prototipo calcola **due metodi indipendenti**:

- **Metodo A**: somma i CONSUMO dichiarati da Neta H2O, ripartiti sui mesi in proporzione ai giorni dichiarati (GG_LETT_PREC). È il metodo più vicino a come *sembrava* lavorare l'ufficio fatturazione, ma soffre di doppio conteggio (vedi sotto).
- **Metodo B**: calcola la differenza fisica tra letture reali consecutive (le letture EFFETTIVA/RIMOZIONE/FINALE "vincono" sulle STIMATE intermedie, che vengono scartate). Più aderente al dato fisico del contatore.

Sui dati reali i due metodi divergevano parecchio — nell'ordine del 30-60% a seconda del distretto. **Daniele ha chiarito il nodo direttamente**, in base alla sua conoscenza professionale del settore: al cliente si fattura il consumo VERO misurato dal contatore; le letture STIMATE sono solo provvisorie e vengono CONGUAGLIATE (corrette/assorbite) dalla lettura reale successiva, non sommate ad essa. Questo corrisponde esattamente al modo in cui è costruito il Metodo B fin dall'inizio, e spiega perché il Metodo A sovrastimava sistematicamente (contava come consumo aggiuntivo delle stime già superate da un conguaglio).

**Decisione**: `district_billed` verrà popolato con i volumi del **Metodo B** (foglio "Import_WMS" dell'Excel).

**✅ Implementato (15/09/2026)**: il prototipo (`motore_calcolo.py`) è stato aggiornato — Daniele ha confermato "Metodo B tutta la vita". Il foglio "Import_WMS" (e tutti gli altri fogli "ufficiali": Riepilogo_Trimestrale, Riepilogo_Comune, Fuori_Periodo, Dettaglio_Classi) ora usano il Metodo B come fonte.

**✅ Metodo A rimosso del tutto (16/09/2026)**: Daniele ha chiesto esplicitamente di non considerare mai più il Metodo A, nemmeno come confronto. Il codice è stato aggiornato di conseguenza: non esiste più nessun calcolo, campo o foglio Excel basato sul Metodo A (rimossi anche i fogli di solo-riferimento e il foglio di confronto diretto che erano stati introdotti il giorno prima). L'applicativo oggi calcola e mostra un solo numero per ogni distretto/mese/trimestre — quello del Metodo B — semplificando sia il codice che l'Excel di output (da 17 a 13 fogli). Chi vorrà in futuro un controllo incrociato indipendente dovrà appoggiarsi a un'altra fonte (es. confronto diretto con le letture grezze, o con Neta H2O), non più a un doppio calcolo interno.

*Nota sulla provenienza*: è la conoscenza professionale diretta di Daniele, non ancora una conferma scritta di Neta H2O. Per l'uso pratico è sufficiente; la riunione con Neta H2O (fissata inizialmente per il 26/08/2026, non ancora svolta al 15/09/2026) resta comunque utile per gli altri punti tecnici ancora aperti (GG_LETT_PREC, letture sentinella, cambi contatore non marcati — vedi documento di specifica), non più per la scelta del metodo.

### 4.5 Il collegamento utenza → distretto (aggiornato rispetto alla versione precedente di questo documento)
**Aggiornamento importante**: si era inizialmente pensato che il collegamento utenza→distretto non fosse disponibile in forma strutturata né in Neta H2O né in WMS SmartH2O, e che andasse costruito a parte (es. tramite comune/indirizzo). Analizzando i file reali, questo NON è vero: **il campo DISTRETTO è già presente direttamente nell'estrazione Neta H2O**, riga per riga, con un codice coerente con quello usato in WMS SmartH2O (es. "DBLG03" per Belgioioso, "DMR10" per Mortara) — non serve nessuna mappatura esterna né incrocio con l'anagrafica indirizzi.

Resta da verificare solo che la CODIFICA dei distretti in Neta H2O coincida esattamente, comune per comune, con quella già caricata in WMS SmartH2O (file "Configurazione Distretti", Fase 2) — un controllo di corrispondenza dei codici, non una costruzione ex-novo del collegamento.

### 4.6 Archiviazione storica e aggiornamento incrementale
Ogni trimestre arriva una nuova estrazione da Neta H2O. Il prototipo mantiene un archivio CSV persistente per comune (deduplicato automaticamente), così ogni nuovo file si aggiunge senza dover ricaricare a mano tutto lo storico. Oggi questo archivio vive solo nella sessione di lavoro locale (non ancora su un server); quando l'applicativo diventerà uno script/servizio vero, l'archivio andrà mantenuto in modo permanente sul computer di Daniele o su un piccolo database dedicato — non necessariamente dentro WMS SmartH2O stesso, dato che è un dato di dettaglio (riga per lettura) molto più granulare di quanto serva a `district_billed` (riga per mese).

### 4.7 Decisione architetturale: due app separate, connesse tramite `district_billed` — non un database condiviso
Domanda posta da Daniele: dato che WMS SmartH2O ha già un database Postgres/TimescaleDB, conviene integrare l'applicativo Fatturazione Utenze dentro la stessa app, o tenerli separati?

**Deciso: due applicazioni separate, ciascuna col proprio database, connesse solo tramite un'interfaccia stabile** (la tabella `district_billed` già prevista, in futuro eventualmente tramite l'API FastAPI di WMS SmartH2O invece che un caricamento manuale). Non condividere direttamente le tabelle interne di Postgres tra le due app — è un principio generale di buona architettura software (due servizi non dovrebbero mai leggere/scrivere lo stesso database "sotto banco": si parlano attraverso un contratto di dati esplicito e stabile).

Motivi, specifici per questa situazione:
- L'applicativo Fatturazione Utenze è ancora sperimentale e in piena evoluzione (due bug reali nei dati scoperti e corretti solo in questa settimana di lavoro, logica di calcolo ancora in discussione in attesa di Neta H2O). WMS SmartH2O è già online e usato per il PNRR: non va esposto agli esperimenti ancora instabili dell'altro applicativo.
- Tenerli disaccoppiati protegge entrambi: un cambiamento nello schema di uno non rischia di rompere in silenzio l'altro.

**Roadmap in due fasi**:
1. **Ora**: Fatturazione Utenze resta completamente autonoma, con il proprio database locale (SQLite, vedi discussione tecnica nel documento di specifica). Il risultato finale (foglio "Import_WMS") viene caricato in WMS SmartH2O manualmente, seguendo il formato già concordato in 4.3.
2. **Più avanti**, quando la logica di calcolo sarà stabile e confermata (dopo Neta H2O): automatizzare il caricamento con una chiamata all'API di WMS SmartH2O (FastAPI) invece che un caricamento manuale — l'app di fatturazione "chiama" l'API e passa i nuovi volumi, senza mai toccare Postgres direttamente.

Nota: "dove gira" l'applicativo (es. in futuro potrebbe girare sullo stesso VPS di WMS per comodità/automazione) è una domanda distinta da "dove tiene i dati" — anche se un giorno girasse sulla stessa macchina, il database resterebbe comunque separato per i motivi sopra.

**Deciso anche sul deployment**: quando l'applicativo Fatturazione Utenze sarà pronto per girare su un server, avrà un **container Docker separato** da quello di WMS SmartH2O — non aggiunto al container unico esistente. Può comunque stare sullo stesso VPS (nessun server nuovo da comprare/gestire): è un secondo container accanto al primo, non una seconda macchina. Motivi: dipendenze Python isolate (un aggiornamento di libreria per un applicativo non rischia di rompere l'altro), riavvii indipendenti (un problema nell'app di fatturazione, ancora sperimentale, non deve poter toccare WMS SmartH2O che è già online e pubblico), e profili d'uso molto diversi (WMS sempre acceso; Fatturazione Utenze lavora "a scatti", solo quando arriva una nuova estrazione Neta H2O, potenzialmente anche non sempre attivo). Il container della fatturazione sarà molto più leggero (solo Python + script + database SQLite su un volume Docker), senza bisogno di Postgres/Nginx al suo interno.

### 4.8 Un dettaglio importante per l'integrazione: i valori possono essere "provvisori"
Emerso ragionando sul caso d'uso reale del bilancio trimestrale: alla chiusura di un trimestre non è detto che tutte le utenze abbiano già una lettura reale disponibile (alcune hanno solo stime in attesa di conferma). Il prototipo ora gestisce questo caso dando comunque un valore stimato (mai zero) e segnalandolo come "provvisorio" in una colonna dedicata; quando arriva la lettura reale mancante (nell'estrazione successiva), il ricalcolo automatico sull'intero archivio storico corregge da solo il dato dei mesi/trimestri passati.

**Conseguenza per `district_billed`**: il caricamento da questo applicativo non potrà essere un semplice inserimento "aggiungi il mese nuovo" — servirà poter AGGIORNARE anche righe di mesi/trimestri già caricati in precedenza, quando le stime provvisorie di allora vengono nel frattempo confermate da letture reali. Da tenere presente nella progettazione dell'endpoint/procedura di caricamento (Fase 3, vedi punto 5 sotto).

### 4.9 Come si parleranno i due container: rete Docker interna (deciso il 16/09/2026)
Daniele ha chiesto se, sapendo già che i due applicativi gireranno su container Docker diversi ma sulla STESSA VPS (vedi 4.7), è il caso di tenerne conto già in fase di configurazione Docker — invece di scoprirlo dopo. Risposta: sì, e costa pochissimo farlo bene da subito.

**Decisioni prese**:
- I due container andranno collegati fin da subito a una **rete Docker interna condivisa** (un bridge network Docker) — permette a un container di chiamare l'altro per NOME (es. "wms-backend"), senza passare da internet, senza aprire porte pubbliche nuove sulla VPS, e senza bisogno del certificato HTTPS che serve invece per l'accesso pubblico a wms.smarth20.com. Se oggi WMS SmartH2O gira come container singolo (non docker-compose multi-servizio), potrebbe servire un piccolo aggiustamento alla sua configurazione per agganciarlo a questa rete nominata — una modifica minima.
- Anche se il traffico resta "in casa" sulla stessa macchina, verrà comunque usato un **token/chiave condiviso** tra le due app per autenticare le chiamate — protezione economica ma utile, visto che il dato finisce nel bilancio idrico del PNRR.
- L'endpoint futuro di caricamento in `district_billed` (Fase 3, vedi 4.8 e punto 5 sotto) dovrà supportare l'**aggiornamento** di righe già caricate (upsert), non solo l'inserimento di righe nuove — necessario per via dei valori "provvisori" che si correggono da soli quando arriva la lettura reale mancante.

**Automazione: "a un click", non completamente automatica (per ora)**: Daniele ha confermato che, almeno all'inizio, il trasferimento a WMS SmartH2O sarà innescato da un'azione manuale — un pulsante nella futura interfaccia di Fatturazione Utenze che, premuto, invia i dati via chiamata diretta all'API di WMS SmartH2O sulla rete Docker interna (niente più scaricare/ricaricare un file a mano, ma resta una persona a decidere quando). Motivo: la logica di calcolo (Metodo B, GG_LETT_PREC, ecc.) non è ancora confermata al 100% da Neta H2O — un controllo umano prima dell'invio resta prudente. Il passaggio a uno scheduler completamente automatico (nessun click) resterà una modifica piccola da fare più avanti, quando la fiducia nel processo sarà consolidata: la parte tecnica più impegnativa (rete Docker, endpoint con upsert, token di autenticazione) è la stessa in entrambi i casi, quindi non c'è da rifare nulla per passare dall'uno all'altro.

**✅ Fatto (16/09/2026): primo scheletro del container creato, e messo online davvero sulla VPS di Daniele.** Contiene: un servizio FastAPI minimo con un endpoint `/health`, `Dockerfile`, `docker-compose.yml` collegato alla rete Docker condivisa, `requirements.txt`, `.env`. Non è stato possibile buildare l'immagine Docker dentro la sessione Claude (l'ambiente cloud di lavoro blocca l'accesso a Docker Hub per policy di rete) — il codice è stato comunque verificato lì facendolo girare direttamente, poi Daniele ha fatto il build vero sulla VPS, riuscito al primo tentativo.

**Nomi reali confermati sulla VPS** (non più segnaposto — utile per chi riprende il lavoro, incluso Claude Code):
- Container di Fatturazione Utenze: **`billing`** (cartella `/opt/billing` sulla VPS), porta pubblicata `8010` → `8000` interna. Stato verificato: `Up (healthy)`.
- Container di WMS SmartH2O: **`wms-smarth20`** (non "wms-backend" come nel placeholder iniziale) — pubblica `127.0.0.1:8080` verso l'esterno, ma la porta interna vera è **80** (quella dopo la freccia in `docker ps`): dalla rete Docker condivisa va quindi chiamato su `http://wms-smarth20:80`, non su `:8080` (quella è solo per l'accesso da fuori/da Caddy).
- Rete Docker condivisa: **`rete-interna-idrico`**, creata e verificata — `wms-smarth20` è già collegato (IP interno `172.26.0.2`), oltre a `billing`.
- Confermato anche che **Caddy gira come container Docker** sulla stessa VPS (nome container `caddy`, pubblica le porte 80/443) — coerente con quanto anticipato da Daniele; il collegamento tra Caddy e la futura interfaccia web di Fatturazione Utenze (quando esisterà) resta da fare più avanti, non necessario per il collegamento interno a WMS SmartH2O.
- La VPS ospita anche molti altri progetti indipendenti di Daniele (n8n, Portainer, ODK Central, Baserow, Ghost, ecc.) — nessuno di questi è coinvolto in questa integrazione, citato solo per contesto.

`README_DOCKER.md`, dentro la cartella del container, resta la nota di consegna per Claude Code (cosa c'è già, cosa manca: upload, interfaccia vera, pulsante "a un click", database vero, autenticazione).

## 5. Prossimi passi
1. **✅ Fatto (15/09/2026)**: deciso il metodo da usare per popolare `district_billed` — Metodo B (vedi 4.4) — e il foglio "Import_WMS" del prototipo è già stato aggiornato per usarlo come fonte.
2. Verificare la corrispondenza dei codici distretto tra Neta H2O e la Configurazione Distretti già in WMS SmartH2O (vedi 4.5) — probabilmente un controllo semplice, non un nuovo sviluppo
3. Decidere l'architettura finale dell'applicativo Fatturazione Utenze: script locale schedulato vs piccola interfaccia (es. Streamlit) vs endpoint dedicato dentro WMS SmartH2O
4. In parallelo, su WMS SmartH2O: completare l'upload ZIP RTU, poi i calcoli di bilancio (Fase 3)
5. In Fase 3 aggiungere anche l'endpoint/procedura per caricare il foglio "Import_WMS" dentro `district_billed` — con upsert (per i valori provvisori, vedi 4.8) e dietro token interno, chiamato "a un click" dalla futura interfaccia di Fatturazione Utenze sulla rete Docker condivisa (vedi 4.9)
6. Estendere il test dell'applicativo Fatturazione Utenze agli altri comuni della gestione (finora verificato su Belgioioso e Mortara, su 22 comuni totali)

*Per i dettagli tecnici completi del prototipo (logica di calcolo, casi reali analizzati, anomalie scoperte nei dati, elenco domande per Neta H2O) vedi il documento `specifiche-applicativo-fatturazione-utenze.md` in questo stesso progetto.*
