# Specifiche tecniche — Applicativo Fatturazione Utenze
Aggiornato: settembre 2026, dopo analisi di due file reali di Belgioioso (trimestri consecutivi lug25-gen26 e feb26-apr26) e TRE file di Mortara che insieme coprono un anno intero (lug25-giu26). **Aggiornamento importante (15/09/2026): il nodo centrale Metodo A vs Metodo B (vedi 2.4/2.10) è stato chiarito direttamente da Daniele, in base alla sua conoscenza professionale del settore: al cliente viene fatturato quanto EFFETTIVAMENTE consumato; le letture STIMATE sono provvisorie e vengono poi conguagliate (assorbite/sostituite) dalla lettura reale successiva, non sommate ad essa. Questo conferma che il Metodo B è quello corretto — vedi 2.4 per il dettaglio. La riunione con Neta H2O (fissata inizialmente per il 26/08/2026) resta comunque utile per gli altri punti ancora aperti (GG_LETT_PREC, letture sentinella, cambi contatore non marcati — vedi elenco domande in coda al documento), ma non è più il nodo bloccante per scegliere il metodo di calcolo.**

## 1. Struttura del file di estrazione Neta H2O
Un file Excel per comune, un foglio, una riga per ogni LETTURA (non per utenza — ogni utenza ha in media 4 letture nel periodo). Colonne principali:

| Colonna | Significato |
|---|---|
| CODICE_SERVIZIO | Identificativo univoco dell'utenza/fornitura. Cambia ad ogni subentro. |
| DP | Punto di Erogazione: la presa fisica, **fissa nel tempo**, indipendente dal contratto. Su uno stesso DP possono succedersi nel tempo contratti, CODICE_SERVIZIO e matricole del contatore diverse (subentri, sostituzioni). Un DP può anche non avere in un dato momento nessuna fornitura attiva (contatore chiuso). Confermato da Daniele e verificato nei dati (112 casi di subentro individuati tra i due file di Belgioioso, stesso DP con CODICE_SERVIZIO diverso). |
| PRODOTTO_CODICE | Classe d'uso/tariffa (es. "DOM_RES-USO DOMESTICO RESIDENTE") |
| LOCALITA | Comune |
| DISTRETTO | Codice distretto **già presente in Neta H2O**, non serve tabella di mappatura esterna. Il prefisso cambia per comune (es. "DBLG" per Belgioioso, "DMR" per Mortara) |
| DATA_INIZIO_FORNITURA / DATA_FINE_FORNITURA | Date di attivazione/cessazione della fornitura (usate per il conteggio utenze attive, vedi 2.7) |
| DATA_LETTURA | Data in cui è stata presa la lettura |
| LETTURA | Valore del contatore (si azzera quando il contatore viene sostituito). **Attenzione**: valori ≥999999 sono codici sentinella "lettura non disponibile", non letture vere — vedi 2.10 |
| CONSUMO | Volume (m³) — **interpretazione da confermare, vedi punto 2.4** |
| GG_LETT_PREC | Numero di giorni "dichiarati" coperti dalla lettura — **da confermare, vedi punto 2.4** |
| STATO_SERVIZIO | Stato del contratto (ATT-ATTIVATA, CFAT-CESSATA, ecc.) |
| MODULO_RADIO | Codice del modulo di telelettura radio. Se vuoto, il contatore non ha modulo radio (rete con tecnologie miste: LoRa, wireless M-Bus, lettura manuale). Confermato da Daniele. |

Elenco completo dei 25 campi del file (con esempi reali e domande aperte) consegnato a Daniele come documento Word a parte, per la riunione con Neta H2O.

## 2. Logica di calcolo implementata (prototipo Python, `motore_calcolo.py`)

### 2.1 Ripartizione mensile (prorata) — METODO A
Il CONSUMO di ogni lettura viene ripartito sui mesi di calendario coperti dal periodo [DATA_LETTURA − GIORNI_EFFETTIVI, DATA_LETTURA], in proporzione ai giorni. Questo è il "Metodo A", il calcolo principale del prototipo. Vedi 2.10 per il Metodo B, un calcolo alternativo aggiunto per confronto.

### 2.2 Classificazione del DISTRETTO (confermata da Daniele, aggiornata — vedi "NODMA" sotto)
- Prefisso coerente con i distretti dominanti nel file (es. "DBLG" per Belgioioso, "DMR" per Mortara) → **valido**.
- "NO DISTRETTO" → **case sparse**, condizione attesa e non un errore.
- Altro valore (prefisso di altro comune, "*", vuoto) → **anomalia anagrafica**, in "Segnalazioni".

**Aggiornamento — il codice "nessun distretto" non è uguale in tutti i comuni ("NODMA")**: Daniele ha segnalato che il codice usato da Neta H2O per "punto non districtualizzato" (case isolate, frazioni non distrettualizzate) cambia da comune a comune — a Belgioioso/Mortara è il testo letterale "NO DISTRETTO", ma altri comuni possono usare convenzioni diverse (es. "NODMA"). Il motore, prima di questa correzione, riconosceva SOLO la stringa esatta "NO DISTRETTO": un codice come "NODMA" sarebbe stato classificato per errore come "anomalia anagrafica" invece che come case sparse (un errore di classificazione, non un errore nei volumi).

**Correzione applicata** (`classifica_distretto`): finché non arriva l'elenco ufficiale comuni/distretti (vedi 6.1), si usa un'euristica più larga — qualunque valore di DISTRETTO che, ripulito da spazi e maiuscole, inizia per "NO" (e non è vuoto) viene classificato come case sparse, non più solo la stringa esatta "NO DISTRETTO". Verificato che questo allargamento non cambia nessuna classificazione già esistente su Belgioioso e Mortara (stessi conteggi case sparse/anomalia di prima). Da sostituire con un confronto esatto contro l'elenco ufficiale dei codici distretto appena disponibile (vedi 6.1) — l'euristica sul prefisso "NO" è una soluzione temporanea, non definitiva (un domani un codice anomalo che per caso inizia per "NO" verrebbe classificato erroneamente come case sparse invece che come vera anomalia).

### 2.3 Finestra utile / mesi "Fuori Periodo" (confermata, in uso)
Mesi tra [mese prima DATA_LETTURA, mese ultima DATA_LETTURA] dell'intero archivio caricato → foglio "Import_WMS". Il resto → "Fuori_Periodo".

**Come funziona il ricalcolo nel tempo (aggiornato — vedi 2.6)**: il motore ricalcola sempre tutto l'archivio storico insieme (non solo il file appena arrivato), così la correzione delle sovrapposizioni e la finestra utile restano corrette anche quando arriva un nuovo trimestre. Da quando esiste l'archivio persistente (2.6), Daniele non deve più ricaricare a mano i file vecchi ad ogni sessione: basta il file nuovo, l'archivio si aggiorna da solo.

### 2.4 ✅ RISOLTO (15/09/2026): cosa rappresentano davvero CONSUMO e GG_LETT_PREC — confermato da Daniele
**Risposta di Daniele**, in base alla sua esperienza diretta nel settore del servizio idrico integrato: *"viene fatturato quello che effettivamente consuma...le letture stimate sono stime poi conguagliate"*. In parole semplici: al cliente si fattura il consumo VERO, misurato dal contatore. Una lettura STIMATA è solo un valore provvisorio "di passaggio" — quando arriva la lettura reale successiva, quella stima viene CONGUAGLIATA, cioè corretta/assorbita dal dato vero, non sommata ad esso.

Applicato all'esempio concreto di 53587909 (vedi sotto): il consumo vero fatturabile su quel periodo è **58 m³** (la differenza fisica letta dal contatore, dalla prima alla penultima lettura reale), non 87 m³ (la somma di EFFETTIVA + STIMATA + RIMOZIONE). I 6 m³ dichiarati nella STIMATA del 30/11/2025 non vanno sommati: erano solo una stima provvisoria di quanto si pensava fosse stato consumato fino a quel momento, poi superata/corretta dalla lettura reale del 27/01/2026.

**Conseguenza diretta**: questo conferma che il **Metodo B è quello corretto** (vedi 2.10) — è costruito esattamente secondo questa logica fin dall'inizio (solo le letture reali "contano", le STIMATE intermedie vengono scartate perché sono provvisorie). Il **Metodo A**, sommando tutti i CONSUMO dichiarati riga per riga comprese le STIMATE, **sovrastima sistematicamente** proprio perché conta come consumo aggiuntivo qualcosa che in realtà era solo una stima già superata dal conguaglio successivo — coerente con quanto osservato empiricamente su tutti i comuni testati finora (Metodo A sempre più alto del Metodo B, dal 30% al 220% a seconda del trimestre/distretto, vedi 2.10 e 2.11).

**Nota sulla provenienza di questa risposta**: è la conoscenza professionale diretta di Daniele (non ancora una conferma scritta da Neta H2O né dal collega interno dell'ufficio fatturazione). Per il modo in cui useremo il dato nell'applicativo è sufficiente; se in futuro emergesse un'eccezione (es. per un caso particolare come l'utenza industriale 58051668, vedi sotto), si potrà comunque verificare con Neta H2O o il collega, ma non è più un punto bloccante per decidere quale metodo usare.

**✅ Implementato nel codice (15/09/2026)**: Daniele ha confermato "Metodo B tutta la vita" — `motore_calcolo.py` è stato aggiornato di conseguenza: tutti i fogli "ufficiali" dell'Excel (Import_WMS, Riepilogo_Trimestrale, Fuori_Periodo, Dettaglio_Classi, Riepilogo_Comune, Riepilogo_Comune_Trimestre) ora usano il Metodo B come fonte, non più il Metodo A.

**✅ Metodo A rimosso del tutto dal codice (16/09/2026)**: Daniele ha chiesto esplicitamente di non prendere mai più in considerazione il Metodo A, nemmeno come confronto/diagnostica. `motore_calcolo.py` è stato aggiornato di conseguenza: sono state eliminate le funzioni `prorata_mensile` ed `elimina_sovrapposizioni` (Metodo A) e tutti i calcoli/fogli che ne dipendevano — non esiste più nessun foglio "MetodoA_Rif" né il foglio "Confronto_Metodi_Trimestrale", né il foglio "Sovrapposizioni_Corrette" (era una correzione specifica del Metodo A). L'applicativo ora calcola e mostra **solo** il Metodo B, ovunque. Vedi sezione 3 per l'elenco fogli aggiornato (13 fogli, da 17). Entrambi gli Excel di output (Belgioioso e Mortara) sono stati rigenerati e verificati: nessun errore, e i numeri del Metodo B sono identici a prima (rimuovere il Metodo A non cambia in nulla il calcolo del Metodo B, che non ne dipendeva).

Le sezioni seguenti di questo documento (2.4, 2.10, 2.11, sezione 4) mantengono comunque i riferimenti storici al Metodo A e ai confronti A/B fatti durante l'analisi: sono il "diario" di come si è arrivati a capire che il Metodo B è quello giusto, e restano utili come racconto/motivazione della decisione — ma non descrivono più nulla che il codice calcoli oggi.

---

**Sezione seguente, mantenuta come analisi originale che ha portato a questa conferma:**
**Ipotesi di lavoro attuale** (basata sui pattern osservati sui dati, non su documentazione ufficiale Neta H2O):

Analizzando 11 utenze di Belgioioso su entrambi i file, emerge uno schema ricorrente:
- Le letture di tipo **STIMATA** seguono date fisse aziendali di chiusura fatturazione (es. 31/8, 30/11, 28/2) e il loro CONSUMO combacia quasi sempre esattamente con la differenza tra LETTURA attuale e LETTURA precedente (verificato: 98,6% di corrispondenza sulle STIMATA in tutto il file 1).
- Le letture di tipo **EFFETTIVA** (controllo reale) hanno invece un GG_LETT_PREC molto piu' lungo (170-180 giorni tipicamente), che risale ben oltre l'ultima STIMATA nota, e il loro CONSUMO e' sistematicamente PIU' ALTO della semplice differenza rispetto all'ultima lettura nota nel file — come se conguagliassero rispetto a una lettura reale ancora precedente, non visibile nei file caricati finora.
- Su tutto il file 1, solo il 52,3% delle righe ha CONSUMO == differenza di LETTURA (percentuale che scende al 27,9% se si guarda solo un campione di utenze con letture sia stimate che effettive).
- **Prova ancora più diretta** (utenza 53587909, matricola 2572504544): tra due letture fisiche consecutive (31/08/2025 → 27/01/2026), il contatore si è mosso di 58 m³ (differenza di LETTURA). Ma sommando i CONSUMO dichiarati nelle letture intermedie in quello stesso periodo (EFFETTIVA 58 + STIMATA 6 + RIMOZIONE 23) si ottiene 87 m³ — 29 m³ in più di quello che il contatore ha realmente registrato.

**Correzione applicata** (`elimina_sovrapposizioni`, Metodo A): indipendentemente dal perche' di questo pattern, il motore evita il doppio conteggio tagliando il periodo di ogni lettura in modo che non si sovrapponga mai a una lettura cronologicamente precedente della stessa utenza (dentro lo stesso file o tra file diversi caricati insieme). Il totale generale del consumo non cambia con questa correzione — cambia solo la distribuzione tra i mesi (impatto misurato: fino a +/-40% su singoli mesi).

**Domande aperte per la riunione con Neta H2O (26/08/2026)**:
1. GG_LETT_PREC è sempre "giorni dalla lettura precedente dello stesso contatore", o può indicare altro?
2. CONSUMO nelle letture STIMATE è sempre = differenza di LETTURA, o a volte è una stima indipendente scollegata dal periodo dichiarato?
3. ~~Il CONSUMO di una lettura EFFETTIVA include un conguaglio rispetto a stime precedenti...~~ — **RISOLTO da Daniele (vedi sopra)**: sì, la lettura reale conguaglia (corregge/assorbe) le stime precedenti, non si somma ad esse.
4. ~~Quando due letture si sovrappongono... il consumo della STIMATA va sommato o sostituito dalla EFFETTIVA?~~ — **RISOLTO da Daniele (vedi sopra)**: sostituito, non sommato — confermato che è la logica già usata dal Metodo B.
5. Cosa rappresenta il valore LETTURA ≥999999? Confermato dai dati che è un codice sentinella (sempre con CONSUMO=0 sulla stessa riga) — chiedere se ci sono altri codici simili da riconoscere.
6. Un cambio di contatore è SEMPRE marcato con INIZIALE ESCLUSO/INCLUSO + RIMOZIONE PER CAMBIO, o può succedere che la LETTURA si azzeri/diminuisca senza queste righe (vedi 2.10 — trovati ~180 casi sospetti)?

Se la risposta di Neta H2O cambia questa interpretazione, la logica di ripartizione mensile (punto 2.1) andrà rivista di conseguenza.

**Secondo caso reale usato come riferimento** (oltre a 53587909 di Belgioioso): utenza **58051668** di Mortara (DMR10, categoria INDUSTRIALE). Qui il divario tra Metodo A e Metodo B è particolarmente clamoroso perché il consumo in gioco è alto: Metodo A = 36.701 m³ nei 10 mesi disponibili (31/08/2025-30/06/2026), Metodo B = 14.660 m³ — quasi 2,5 volte meno. Causa identificata con precisione: una singola lettura RIMOZIONE PER CAMBIO del 19/06/2026 dichiara un CONSUMO di 22.652 m³ con GG_LETT_PREC=547 giorni (quasi un anno e mezzo, ben oltre l'ultima lettura nota), mentre la differenza fisica di LETTURA nello stesso arco visibile è solo 14.115 m³ su 292 giorni. Daniele ha segnato questo caso come **da riprendere e discutere più avanti** (probabile esempio concreto da portare alla riunione Neta H2O, essendo un'utenza industriale ad alto impatto economico se il metodo sbagliato viene scelto per il bilancio). Annualizzando linearmente i 303 giorni coperti: Metodo A ≈ 44.200 m³/anno, Metodo B ≈ 17.700 m³/anno — ma su una singola utenza industriale l'annualizzazione lineare è meno affidabile che sulla media di un intero comune (rischio di stagionalità produttiva non nota).

### 2.5 Utenze che spariscono tra un file e il successivo (confermato, in uso)
Quando si caricano più file in ordine cronologico, il motore confronta le utenze dell'ultimo file con quelle dei file precedenti. Un'utenza presente in un file vecchio ma assente nel più recente è normale SE il suo ultimo stato noto è di chiusura (CFAT-CESSATA, SOSPESO, MOROSITA'...). Se invece l'ultimo stato noto era ancora "ATT-ATTIVATA", viene segnalata nel foglio "Utenze_Scomparse" come da verificare (potrebbe comparire nel file successivo, o essere un problema di estrazione).

Test su Belgioioso (file1→file2): 99 utenze sparite, 98 con contratto chiuso (regolare), 1 ancora attiva (da verificare).

**Idea di miglioramento non ancora implementata**: da quando è confermato che il DP resta fisso durante un subentro (vedi tabella §1), si potrebbe incrociare l'utenza sparita con il DP: se sullo stesso DP compare una nuova utenza nel file più recente, si tratta di un subentro regolare (non un caso da verificare), anche se lo stato dell'utenza vecchia risultava ancora "ATT-ATTIVATA". Individuati 7 casi così nel confronto file1→file2 di Belgioioso, oggi segnalati come "da verificare" ma probabilmente falsi allarmi. Proposto a Daniele, in attesa di conferma per l'implementazione.

### 2.6 Archivio storico persistente (risponde a "come ricordo le letture già caricate?")
Problema: ogni trimestre arriva un nuovo file, ma il motore deve poter ricalcolare tutto l'archivio insieme (vedi 2.3) senza che Daniele debba ricaricare a mano tutti i file vecchi ogni volta.

Soluzione implementata: un archivio CSV (`archivio/archivio_letture.csv`) che accumula tutte le letture mai caricate, con deduplica automatica sulla chiave (CODICE_SERVIZIO, DATA_LETTURA, TIPO_LETTURA) — così se per sbaglio si ricarica due volte lo stesso file (o file che si sovrappongono), le righe duplicate non vengono contate due volte.

Funzioni principali:
- `carica_archivio(percorso)` — legge l'archivio CSV esistente (o restituisce un archivio vuoto se è la prima volta).
- `aggiorna_archivio(nuovi_file, percorso)` — legge i file nuovi, li unisce all'archivio esistente, elimina i duplicati, salva il CSV aggiornato. Restituisce anche delle statistiche (righe prima/dopo, quante erano duplicate).
- `elabora_dataframe(...)` — il motore di calcolo vero e proprio (classificazione distretti, correzione sovrapposizioni, prorata Metodo A e Metodo B, aggregazione, trimestri, segnalazioni, utenze scomparse, utenze attive, totale comune, conteggio utenze per classe/stato), ora lavora sempre sull'intero archivio, non sui singoli file.
- `elabora_file(...)` resta come funzione di comodo per elaborare direttamente uno o più file senza passare dall'archivio (utile per test rapidi, es. un comune nuovo — vedi 2.11).

Il flusso da riga di comando ora è: `python motore_calcolo.py nuovo_file.xlsx` → aggiorna l'archivio con SOLO il file nuovo → rielabora tutto l'archivio → genera l'Excel di output. Non serve più ripassare i file vecchi.

**Test di verifica eseguito** (3 sessioni simulate):
1. Solo file1 (lug25-gen26) → archivio a 10.978 righe.
2. Aggiunto file2 (feb26-apr26) senza ricaricare file1 → archivio a 16.536 righe, risultati identici a quelli ottenuti elaborando i due file insieme direttamente (nessuna differenza).
3. Ricaricato per errore di nuovo file1 → 0 righe nuove aggiunte, archivio resta a 16.536 righe (la deduplica ha funzionato).

**Nota importante su dove vive l'archivio**: per ora, durante lo sviluppo dentro Claude, l'archivio CSV vive nella cartella di lavoro di questa sessione (`archivio/archivio_letture.csv`), che **non è permanente tra una chat e l'altra**. È stato fatto un primo tentativo di salvarlo come documento nel progetto "VOLUMI DISTRETTI" per portarlo da una chat all'altra, ma il file (4,5 MB, ~2,45 milioni di "token") ha superato da solo il limite di spazio del progetto (2 milioni di token), quindi è stato rimosso dal progetto. **Conclusione**: il progetto Claude va bene per le specifiche e le decisioni (questo documento), ma NON è il posto giusto per l'archivio dati vero e proprio, che è troppo grande e destinato comunque a diventare permanente. Quando l'applicativo diventerà uno script/app locale vero e proprio sul computer di Daniele (fuori da Claude), l'archivio CSV vivrà lì, in una cartella normale, senza questo problema di spazio. Nel frattempo, in ogni nuova chat su questo progetto va ricostruito rilanciando il motore sui file Excel originali già disponibili.

### 2.7 Utenze attive per distretto, per trimestre (nuovo, su richiesta di Daniele)
Oltre ai volumi, il foglio "Riepilogo_Trimestrale" ora ha anche la colonna **"Utenze Attive"**: per ogni distretto valido, quante utenze avevano una fornitura attiva in QUALCHE momento di quel trimestre.

Calcolo: si usano le date DATA_INIZIO_FORNITURA/DATA_FINE_FORNITURA (non le letture, che potrebbero non cadere proprio in quel trimestre). Un'utenza conta come attiva nel trimestre se è iniziata prima della fine del trimestre e (se già cessata) non è terminata prima dell'inizio del trimestre. Per ogni utenza si usano l'anagrafica e le date più recenti note su tutto l'archivio, così una chiusura successiva si riflette anche sui trimestri passati.

Scelta confermata da Daniele: granularità **trimestrale** (coerente col bilancio, non mensile), non un'istantanea unica.

Risultato su Belgioioso: DBLG03 oscilla tra 2209 e 2239 utenze attive nei 4 trimestri disponibili, DBLG02 tra 413 e 419, DBLG01 stabile a 64 — variazioni piccole e plausibili, coerenti con normale turnover (subentri, nuovi allacci, cessazioni).

### 2.8 Totale consumi del comune intero (nuovo, su richiesta di Daniele)
Oltre ai volumi per distretto, servono anche i volumi del COMUNE INTERO, comprensivi di quello che i distretti da soli non coprono: le case sparse (NO DISTRETTO) e le utenze con distretto anomalo/mancante. Due nuovi fogli:

- **Riepilogo_Comune** (mensile): una riga per (mese, comune), con colonne separate "Volume nei Distretti (m3)", "Volume Case Sparse (m3)", "Volume Distretto Anomalo/Mancante (m3)" e "Totale Comune (m3)" (somma delle tre), più una colonna "Affidabile" (Sì/No, stessa logica della finestra utile del punto 2.3).
- **Riepilogo_Comune_Trimestre**: lo stesso, sommato per trimestre solare (solo mesi affidabili), con flag "Completo"/"Parziale" come nel Riepilogo_Trimestrale per distretto.

Verifica di coerenza eseguita: la colonna "Volume nei Distretti (m3)" di luglio 2025 (40.726,48 m³) coincide esattamente con la somma dei volumi per distretto dello stesso mese nel foglio "Import_WMS" — conferma che la scomposizione per categoria non altera il totale, lo spiega soltanto meglio.

### 2.9 Numero di utenze per distretto, classe d'uso e stato servizio (nuovo, su richiesta di Daniele)
Due nuovi fogli, entrambi una **FOTO ATTUALE** (l'ultimo stato noto di ogni utenza in archivio, non uno storico nel tempo — a differenza del punto 2.7 che è per trimestre):

- **Utenze_per_Distretto**: per ogni distretto valido, una riga con il numero di utenze per ciascuno stato servizio (colonne separate: ATT-ATTIVATA, CFAT-CESSATA FATTURATA, SOSP-SOSPESO, ecc.) più "Totale Utenze". Vista d'insieme rapida.
- **Utenze_Distretto_Classe**: lo stesso ma con il dettaglio anche per classe d'uso (PRODOTTO_CODICE) — una riga per (distretto, classe d'uso), sempre con una colonna per stato servizio.

Nota importante: questi numeri sono uno "scatto fotografico" dell'anagrafica ad oggi, quindi NON coincidono esattamente con "Utenze Attive" del punto 2.7 (che è "attiva in qualche momento di un trimestre passato", basata sulle date di inizio/fine fornitura) — sono due domande diverse. Su Belgioioso, ad esempio, DBLG03 ha oggi 2.173 utenze con stato ATT-ATTIVATA, mentre nel 1° trimestre 2026 ne risultavano attive in qualche momento 2.239 (alcune si sono chiuse nel frattempo).

Verifica di coerenza eseguita: la somma del "Totale Utenze" per ogni distretto nel foglio dettagliato per classe coincide esattamente con il "Totale Utenze" del foglio riassuntivo per distretto (64 / 426 / 2.317).

### 2.10 METODO B: differenza di letture, le reali vincono sulle stimate (nuovo, proposto da Daniele dopo confronto con i colleghi della fatturazione)
Daniele ha riportato come lavora l'ufficio fatturazione: prendono il CONSUMO, lo dividono per i giorni, e assegnano quel valore mc/giorno andando all'indietro dalla DATA_LETTURA — che è esattamente il principio del Metodo A (2.1). Approfondendo con un esempio numerico reale (utenza 53587909), è emerso però un problema: quando una lettura EFFETTIVA dichiara un periodo che si sovrappone a una STIMATA precedente, sommare entrambe (come fa il Metodo A) rischia di sovrastimare, perché la EFFETTIVA sembra già includere un "conguaglio" per quel periodo (vedi 2.4).

Daniele ha proposto una regola alternativa, raffinata insieme passo per passo con esempi numerici:

1. Le letture si dividono in **segmenti** delimitati dai cambi di contatore (INIZIALE ESCLUSO/INCLUSO apre un segmento nuovo): non si calcola mai una differenza di lettura tra contatori diversi.
2. Dentro ogni segmento, la **prima lettura nota** (qualunque tipo) è il punto di partenza: non genera consumo da sola (non sappiamo cosa è successo prima).
3. Ogni lettura **REALE** (misurata fisicamente: LETTURA EFFETTIVA, RIMOZIONE PER CAMBIO, FINALE, CHIUSURA PER MOROSITA', APERTURA DA SOSPENSIONE FORNITURA, LETTURA A GIRO) è un'**ancora**: il consumo tra due ancore consecutive è la differenza di LETTURA, spalmata sui giorni di calendario tra le due date.
4. Una STIMATA (o assimilata: LETTURA RIPROPORZIONATA, SPEZZATURA — **ipotesi da confermare con Neta**, non letture fisiche per nome) "in mezzo" tra due ancore viene **cancellata**: non conta.
5. Se l'ULTIMA lettura nota di un segmento è una STIMATA (nessuna reale successiva), si considera comunque, usando il suo CONSUMO/GG_LETT_PREC dichiarati così come sono — **eccetto** quando l'utenza è CESSATA (vedi sotto, è un'anomalia).

**Validato** sull'utenza 53587909 (stesso esempio usato per costruire la regola): risultato 66 m³ sui mesi coperti, contro i 108 m³ del Metodo A sullo stesso periodo — coerente con i calcoli fatti a mano in chat.

**Tre problemi di qualità dati scoperti applicando il Metodo B su larga scala** (il Metodo A non li aveva mai fatti emergere, perché non guarda mai la LETTURA grezza):
- **Letture "sentinella"**: alcuni valori di LETTURA sono codici come 999999 o 9999999 (sempre con CONSUMO=0 sulla stessa riga) che significano "lettura non disponibile", non una lettura vera. Se usati come ancora, generano differenze di milioni di m³. **Protezione aggiunta**: LETTURA ≥ 999999 non fa mai da ancora (costante `LETTURA_SENTINELLA_MIN`).
- **Reset di contatore NON marcati**: in ~180 casi su 16.536 righe (Belgioioso), la LETTURA scende tra due letture reali consecutive SENZA che ci sia un INIZIALE ESCLUSO/RIMOZIONE PER CAMBIO a segnalarlo — quindi un cambio contatore silenzioso, o un altro tipo di errore. **Protezione aggiunta**: una differenza negativa tra due ancore non viene mai sommata al totale (impossibile fisicamente fuori da un cambio contatore); il periodo finisce nel foglio "Anomalie_MetodoB" per verifica manuale, e l'ancora si sposta comunque in avanti sulla nuova lettura per non propagare l'errore.
- **Ritmo di consumo implicito assurdo** (scoperto su Mortara, vedi 2.11 per il caso completo): un valore di LETTURA può essere vicino a 999.999 senza raggiungere la soglia sentinella (es. 999.996) — capitato con un salto da 0 a 999.996 in 80 giorni per una sola utenza, un ritmo di 12.500 m³/giorno, fisicamente impossibile. Semplicemente allargare la soglia sentinella avrebbe escluso per errore anche i contatori a 6 cifre genuinamente vicini al "giro di boa" (letture reali trovate nell'archivio con consumi piccoli e normali). **Protezione aggiunta**: se il ritmo m³/giorno implicito da una singola differenza tra due ancore supera 500 m³/giorno (costante `MC_GIORNO_SANITA_MASSIMA`, con ampio margine sopra il valore genuino più alto mai osservato, ~139 m³/giorno), quella differenza non viene contata: va in "Anomalie_MetodoB", l'ancora avanza comunque.

**Nuovo controllo collegato** (confermato da Daniele: alla cessazione di un'utenza è obbligatoria una lettura reale): utenze con contratto chiuso la cui ultima lettura nota NON è reale → foglio "Cessate_Con_Stima_Finale" (20 casi trovati su Belgioioso).

**Aggiornamento importante — valori provvisori invece di sottostime** (proposto da Daniele pensando al bilancio idrico trimestrale reale): inizialmente, se un segmento non era ancora chiuso da una lettura reale (tipico caso: le ultime letture arrivate sono tutte STIMATA, in attesa della prossima estrazione), il Metodo B contava SOLO l'ultima stima nota, scartando tutte le stime precedenti pendenti — con il risultato di sottostimare pesantemente il consumo "in tempo reale" (esempio concreto: l'utenza 58051668, interrogata come se fossimo al 01/03/2026 con solo stime disponibili, dava 3.635 m³ col Metodo B contro 9.746 m³ del Metodo A).

Daniele ha fatto notare il caso d'uso reale: il bilancio si fa per trimestre, confrontando i volumi immessi in rete (da WMS SmartH2O) con l'ultima stima disponibile per quel trimestre — che potrebbe non avere ancora tutte le letture reali. Nel trimestre successivo, quando arrivano le letture reali mancanti, i valori dei trimestri passati vengono corretti "da soli" (perché il motore ricalcola sempre tutto l'archivio da capo, vedi 2.6) — e sommando su un periodo più lungo (es. un semestre) il risultato diventa via via più verosimile. **Una stima è sempre meglio di uno zero.**

**Correzione applicata**: ora TUTTE le stime pendenti dopo l'ultima ancora reale (non solo l'ultima) vengono contate, ciascuna con il proprio valore dichiarato, come valore "provvisorio" (ORIGINE: "stima provvisoria (in attesa di lettura reale che confermi il periodo)"). Quando in un caricamento successivo arriva finalmente una lettura reale, il ricalcolo automatico sull'intero archivio sostituisce da solo questi valori provvisori con la differenza fisica reale — non serve nessuna logica di "sovrascrittura" a parte, è una conseguenza naturale del fatto che il motore riparte sempre da zero su tutto l'archivio.

**Nuova colonna "Contiene Stime Provvisorie" (Sì/No)**, aggiunta ai fogli MetodoB_Trimestrale e Confronto_Metodi_Trimestrale: segnala, per ogni trimestre e distretto, se una parte del volume viene da stime non ancora confermate da una lettura reale — quindi un numero che potrebbe ancora cambiare in un ricalcolo futuro. Esempio su Mortara: il 3° trimestre 2025 (il più vecchio, appena dopo l'inizio dell'archivio) ha differenze enormi tra Metodo A e B (fino a +3.500%) perché quasi tutte le utenze non avevano ancora una lettura reale a chiudere il periodo; il divario si restringe progressivamente nei trimestri successivi (da centinaia di % a poche unità/decine di %) man mano che le letture reali arrivano — la dimostrazione pratica di quello che Daniele descriveva: i numeri si "consolidano" con il tempo, e un confronto cumulato su più trimestri è più affidabile di uno singolo trimestre appena chiuso.

**Implicazione per l'integrazione con WMS SmartH2O** (da tenere presente, vedi anche il documento di riepilogo progetto): se in futuro si userà il Metodo B per popolare `district_billed`, il caricamento non potrà essere un semplice "aggiungi il mese nuovo" — un ricalcolo periodico dovrà poter AGGIORNARE anche mesi/trimestri già caricati in precedenza, quando le stime provvisorie vengono confermate da letture reali arrivate nel frattempo.

**Confronto Metodo A vs Metodo B** (foglio "Confronto_Metodi_Trimestrale"): il Metodo A dà SEMPRE un volume più alto del Metodo B, ma la differenza si riduce nel tempo (dal +220% circa nel 3° trimestre 2025 al +40-70% nel trimestre più recente) — plausibile, perché il primo trimestre soffre di più "letture orfane" (prime letture di tante utenze, mai contate nel Metodo B perché non hanno un punto prima con cui fare la differenza).

**Nuovi fogli nell'Excel**: MetodoB_Trimestrale, Confronto_Metodi_Trimestrale, Anomalie_MetodoB, Cessate_Con_Stima_Finale.

**Ancora aperto** (da chiedere/verificare, non solo a Neta ma anche internamente): come valorizzare una STIMATA finale che segue un'altra STIMATA finale (due stime consecutive senza reale in mezzo, mai capitato nell'esempio usato per validare) — per ora si considera solo l'ultima, scartando le precedenti, per coerenza con la regola "si usa sempre il dato più recente disponibile", ma non è stato esplicitamente confermato da Daniele.

### 2.11 Secondo comune testato: Mortara — ora con un anno intero di dati (aggiornato)
Daniele ha caricato la prima estrazione di un comune diverso da Belgioioso: Mortara, periodo 01/05/2026–30/06/2026 (`MORTARA_Script PVACQUE0103_01052026_30062026.xlsx`), chiedendo un "calcolo veloce" e se esistevano estrazioni precedenti già caricate per questo comune.

**Risposta iniziale**: no — l'archivio persistente di Belgioioso non conteneva dati di Mortara; era il primo file di questo comune mai caricato.

**Verifiche fatte prima di calcolare**:
- Struttura colonne identica a Belgioioso (stesse 26 colonne di `COLONNE_ATTESE`) — il motore non ha bisogno di adattamenti per cambiare comune.
- Prefisso distretto dominante: **DMR** (es. DMR10, DMR03, DMR04...) invece di DBLG — confermato che la classificazione "valido/case sparse/anomalia" (2.2) funziona automaticamente anche su un comune nuovo, senza bisogno di configurazione manuale.

**Aggiornamento importante**: Daniele ha poi caricato altri due file di Mortara (01/07/2025–31/01/2026 e 01/02/2026–30/04/2026), che sommati al primo coprono **un anno intero consecutivo, luglio 2025 – giugno 2026**, senza buchi. È stato creato un archivio persistente dedicato (`archivio/archivio_letture_mortara.csv`, tenuto separato da quello di Belgioioso — vedi "Aperto" più sotto), con lo stesso meccanismo di deduplica del punto 2.6: caricando i 3 file insieme sono state scartate solo 5 righe duplicate sui punti di sovrapposizione tra un'estrazione e la successiva.

**Risultato: 4 trimestri consecutivi COMPLETI (non più stime parziali)** — 2025-T3, 2025-T4, 2026-T1, 2026-T2:

| Distretto | Volume Metodo A anno (m³) | Volume Metodo B anno (m³) | Differenza |
|---|---|---|---|
| DMR01 | 130.020 | 86.285 | B più basso del 34% |
| DMR03 | 69.996 | 44.734 | B più basso del 36% |
| DMR04 | 77.954 | 49.341 | B più basso del 37% |
| DMR05 | 14.468 | 9.822 | B più basso del 32% |
| DMR06 | 23.279 | 14.517 | B più basso del 38% |
| DMR08 | 22.318 | 14.594 | B più basso del 35% |
| DMR09 | 19.723 | 13.097 | B più basso del 34% |
| DMR10 | 1.194.192 | 1.808.249 | **B più ALTO del 51%** |

Totale comune Metodo A, anno intero (distretti + case sparse + anomalie): **1.580.066 m³** — non è più una proiezione, è il consumo effettivamente registrato nell'arco di 12 mesi consecutivi.

**Scoperta e poi RISOLTA — inversione A/B su DMR10**: inizialmente, in **DMR10** (il distretto di gran lunga più grande, oltre 3.000 utenze) il Metodo B risultava più alto del Metodo A del 51%, l'opposto di ogni altro distretto. Indagando riga per riga (confrontando, per ogni coppia di letture reali consecutive, la differenza fisica di LETTURA con la somma dei CONSUMO dichiarati nella stessa finestra) è emerso che la causa era **un singolo dato sporco**, non un fenomeno reale del distretto: l'utenza **70342477** (categoria DOM_NO_RES, Mortara) ha una lettura che salta da 0 (31/12/2025) a 999.996 (18/03/2026) — un "consumo" fisicamente assurdo di quasi 1.000.000 m³ in 80 giorni (12.500 m³/giorno, contro un massimo genuino osservato in tutto l'archivio di appena ~139 m³/giorno su un'utenza industriale vera). Il valore non veniva intercettato dal filtro "letture sentinella" (2.10) perché quel filtro scatta solo per LETTURA ≥ 999.999, mentre qui il valore era 999.996 — appena sotto la soglia. Da solo, questo singolo errore valeva più dell'intero divario del distretto.

**Attenzione — non basta allargare la soglia sentinella**: analizzando l'archivio è emerso che molti contatori hanno legittimamente valori di LETTURA vicini a 999.999 (contatori a 6 cifre prossimi al "giro di boa"), con consumi piccoli e normali (es. LETTURA 999.970→999.985→999.990 con consumi di pochi m³ per lettura) — quindi alzare semplicemente la soglia sentinella avrebbe escluso per errore anche queste letture vere.

**Correzione applicata** (nuova protezione nel Metodo B, oltre alle due già presenti — vedi 2.10): aggiunto un controllo sul RITMO di consumo implicito da ogni singola differenza tra due letture reali (m³/giorno = differenza di lettura ÷ giorni trascorsi). Se questo ritmo supera una soglia di sicurezza di 500 m³/giorno (scelta con ampio margine sopra il valore genuino più alto trovato in tutto l'archivio, ~139 m³/giorno), quella differenza NON viene contata come consumo: finisce invece nel foglio "Anomalie_MetodoB" per verifica manuale, e l'ancora si sposta comunque in avanti (stessa logica già usata per i reset di contatore non marcati). **Dopo la correzione, DMR10 torna in linea con tutti gli altri distretti**: Metodo B più basso del Metodo A del 32,3% (praticamente identico al 32-38% di tutti gli altri distretti di Mortara e coerente con Belgioioso).

| Distretto | Metodo A (m³/anno) | Metodo B (m³/anno, corretto) | B più basso di |
|---|---|---|---|
| DMR01 | 130.020 | 86.285 | 33,6% |
| DMR03 | 69.996 | 44.734 | 36,1% |
| DMR04 | 77.954 | 49.341 | 36,7% |
| DMR05 | 14.468 | 9.822 | 32,1% |
| DMR06 | 23.279 | 14.517 | 37,6% |
| DMR08 | 22.318 | 14.594 | 34,6% |
| DMR09 | 19.723 | 13.097 | 33,6% |
| DMR10 | 1.194.192 | 808.253 | 32,3% |

Altre segnalazioni sull'anno intero di Mortara: 7.169 sovrapposizioni corrette (fisiologico, scala x3 rispetto al confronto sul solo primo file), 148 anomalie totali nel Metodo B (letture sentinella, reset non marcati, o ritmo assurdo), 43 utenze cessate con ultima lettura stimata invece che reale, 24 utenze ancora "attive" nell'ultima lettura nota ma sparite dal file più recente (da verificare, stesso controllo del punto 2.5). Solo l'1,2% del volume totale del file ricade ancora fuori dalla finestra affidabile (contro il 42% del primo file da solo) — la copertura di un anno intero ha risolto quasi del tutto l'effetto di bordo iniziale.

**Conclusione**: il motore generalizza correttamente a un comune diverso senza modifiche al codice, e ora anche Mortara ha una base annuale solida quanto quella di Belgioioso (anzi più lunga: 12 mesi contro 9-10). Il pattern è ora coerente ovunque: **il Metodo A è sempre più alto del Metodo B**, di circa il 30-40% a Mortara e fino a quasi il doppio a Belgioioso — nessuna eccezione residua nei dati analizzati finora. La caccia all'inversione di DMR10 è stata anche un buon banco di prova per il motore: ha dimostrato che le protezioni già esistenti (letture sentinella, reset non marcati) non bastavano a coprire ogni caso limite, ed è servita una terza protezione basata sul ritmo di consumo implicito piuttosto che sul valore assoluto della lettura.

### 2.12 Cosa rappresenta davvero GG_LETT_PREC, e nuovo foglio di riferimento "consumo pro-die" (16/09/2026)
Daniele ha spiegato come lo usa l'ufficio fatturazione: GG_LETT_PREC serve a costruire le stime di fine mese (es. fatturazione al 30 del mese), prendendo il ritmo di consumo (m³/giorno) e applicandolo ai giorni passati dall'ultima lettura per ottenere un valore plausibile — un controllo di coerenza ("è in linea?"), non un dato che determina il volume fatturato. La sua descrizione iniziale: GG_LETT_PREC è la differenza tra la lettura corrente e l'ultima lettura EFFETTIVA (quindi con le STIMATE consecutive il valore cresce ogni volta, finché non arriva una nuova lettura reale).

**Verifica sui dati (archivio Belgioioso)**: questa descrizione è confermata al 100% per le letture di tipo **LETTURA RIPROPORZIONATA** (207/207 casi) e quasi sempre per **SPEZZATURA** (27/29). Per contro, sul tipo più comune, **LETTURA STIMATA**, i dati mostrano un comportamento diverso: GG_LETT_PREC NON si accumula dall'ultima reale, ma riparte ogni volta dalla lettura immediatamente precedente nel file, qualunque essa sia (238/243 casi, 98%). Esempio concreto: utenza 53588919, STIMATA del 28/02/2026 con GG_LETT_PREC=90 — corrisponde ai giorni dalla riga precedente (anch'essa una STIMATA), non ai 107 giorni dall'ultima lettura reale.

**Perché non è un problema per l'applicativo**: il Metodo B non usa mai GG_LETT_PREC per calcolare il volume fatturato (vedi 2.10) — scarta sempre le STIMATE intermedie e calcola la differenza fisica tra letture reali consecutive. Quindi questa incoerenza nel campo dichiarato, per quanto interessante da sapere, non tocca in nessun modo i numeri che l'applicativo produce.

**Nuovo foglio "Rif_ConsumoProDie"**: su richiesta di Daniele, è stato comunque aggiunto un riferimento visivo del ritmo di consumo (m³/giorno), utile per un controllo rapido "a colpo d'occhio" anche sui casi normali (prima il foglio Anomalie_MetodoB mostrava solo i casi sopra la soglia di allarme). A differenza del campo dichiarato GG_LETT_PREC (che abbiamo appena visto essere incoerente per le STIMATE), questo riferimento è calcolato dal Metodo B stesso — cioè sulla differenza fisica tra due letture reali consecutive, oppure sul valore dichiarato SOLO per le stime provvisorie pendenti (quando non c'è ancora una lettura reale successiva con cui calcolare la differenza vera) — quindi è più affidabile del campo del file. Una riga per ogni confronto fatto dal Metodo B, con colonna ESITO: "OK" (caso normale), "Escluso: ..." (le stesse due anomalie del foglio Anomalie_MetodoB, qui con in più il valore m³/giorno che le ha fatte scattare), oppure "Stima provvisoria..." (valore dichiarato nel file, in attesa di conferma). Non è una funzione di calcolo nuova: usa la stessa soglia di sanità già esistente (`MC_GIORNO_SANITA_MASSIMA`, 500 m³/giorno) — solo che ora il ritmo è visibile per ogni riga, non solo per quelle sopra soglia.

### 2.13 Segnalazione (non bloccante) di estrazione forse incompleta per comune (16/09/2026)
Daniele ha chiesto un controllo per accorgersi se, caricando un file, si è dimenticato un pezzo del comune (es. l'estrazione Neta H2O era parziale e mancava un intero distretto) — **esplicitamente NON bloccante**: solo una segnalazione da verificare, il calcolo va avanti comunque.

**Come funziona**: per ogni comune, il motore costruisce l'insieme di tutti i distretti "validi" mai visti in archivio (sommando tutti i file caricati nel tempo per quel comune). Poi confronta ogni singolo file con questo insieme: se un distretto che conosciamo per quel comune (da un altro file, un altro trimestre) NON compare affatto nel file appena caricato, viene segnalato.

**Limite noto, dichiarato apertamente**: non è un controllo definitivo, perché non abbiamo ancora l'elenco ufficiale dei distretti di ogni comune (vedi 6.1) — usiamo come riferimento lo storico stesso dell'archivio. Due conseguenze:
- Il **primo file mai caricato** di un comune nuovo non genera mai una segnalazione (non c'è ancora nessuno storico con cui confrontarlo).
- Un distretto che in un trimestre non ha semplicemente avuto NESSUNA lettura (capita, è normale) genererebbe una segnalazione anche se l'estrazione era completa — per questo resta un avviso da controllare a mano, non un errore bloccante.

**Dove si vede**: una nuova colonna nel foglio "Riepilogo_File", **"Distretti Noti Assenti in Questo File"** (elenco dei distretti mancanti, o "Nessuno"), più un avviso testuale riassuntivo se almeno un file ha qualcosa da segnalare.

**Test eseguito**: sui dati reali di Belgioioso e Mortara nessuna segnalazione scatta (come atteso, le estrazioni erano complete). Test di verifica del meccanismo: rimosso artificialmente un intero distretto (DBLG01) da un file di prova → la segnalazione è scattata correttamente, indicando file e distretto mancante, senza alterare il calcolo del Metodo B sugli altri distretti.

### 2.14 Statistiche per comune/distretto (16/09/2026, su richiesta di Daniele)
Daniele ha chiesto un "tab statistiche" per comune/distretto. Proposte quattro statistiche (tutte confermate da Daniele), implementate in tre nuovi fogli — tutte calcolate sul Metodo B, solo sui mesi/trimestri della finestra affidabile (2.3):

**Statistiche_Trimestrali** — una riga per (Trimestre, Distretto):
- **Dotazione idrica** (m³/utenza/giorno): Volume del trimestre ÷ Utenze Attive (già calcolato, vedi 2.7) ÷ giorni veri del trimestre. Daniele ha chiesto la dotazione PER UTENZA (non pro-capite, non avendo il dato di popolazione residente) — resta quindi un indicatore "per fornitura", non per abitante.
- **Variazione % vs trimestre precedente** e **variazione % vs stesso trimestre dell'anno precedente**: calcolate solo quando esiste davvero il dato di confronto (nessun buco tra i due trimestri) — altrimenti la cella resta vuota, non si inventa un confronto. Con l'archivio attuale (max 4 trimestri consecutivi) la colonna "vs anno precedente" è quasi sempre vuota: servirà un secondo anno di dati per popolarla.

**Statistiche_Classe_Uso** — una riga per (Distretto, Classe d'uso):
- Volume totale sull'intera finestra affidabile e % sul totale del distretto (risponde a "quanto pesano industriale/domestico/ecc. su questo distretto").
- Consumo medio annuo per utenza attiva (m³/utenza/anno): il volume del periodo, **annualizzato** in proporzione a quanti giorni copre davvero l'archivio disponibile (es. con 10 mesi di dati si proietta a un anno intero) — con un archivio ancora corto questo è una proiezione, non una misura su un anno vero, e lo diventerà via via che l'archivio si allunga. Le utenze al denominatore sono quelle con stato ATT-ATTIVATA **oggi** (foto attuale, stessa logica già usata al punto 2.9), non quelle attive nel periodo passato — un'approssimazione dichiarata.

**Coefficiente_Punta** — una riga per Distretto:
- Rapporto tra la **portata media del mese più carico** (m³/giorno) e la **portata media di tutti i mesi disponibili** — l'indicatore classico per dimensionare reti e impianti (di quanto il picco stagionale supera la media). Usa la portata media giornaliera di ciascun mese (volume ÷ giorni del mese), non il volume grezzo, altrimenti un mese di 31 giorni risulterebbe sempre "più alto" di uno di 28 solo per conteggio dei giorni.
- Con meno di 12 mesi in archivio (caso di Belgioioso, 10 mesi) la "media" è quella dei mesi disponibili, non di un anno solare completo: il numero è indicativo, si consolida quando l'archivio copre un anno intero. Su Mortara (12 mesi pieni) il coefficiente è già una misura vera: tra 1,33 (DMR10) e 1,55 (DMR09), coerente con un picco stagionale moderato (idrico civile, non particolarmente estremo).

**Statistiche NON incluse, per scelta esplicita di Daniele**: dotazione pro-capite (avrebbe richiesto il dato di popolazione residente, non disponibile — si resta sul dato per utenza).

**Verificato**: rigenerati entrambi gli Excel di output, il totale Import_WMS resta identico a prima (239.098,11 m³ Belgioioso, 1.044.820,99 m³ Mortara) — le nuove statistiche sono calcoli aggiuntivi che non toccano il Metodo B.

## 3. Formato di output del prototipo (Excel, 17 fogli — aggiornato 16/09/2026: solo Metodo B, più i fogli di riferimento pro-die e statistiche)
1. **Riepilogo_File** — controllo su cosa è stato elaborato
2. **Import_WMS** — Mese, Codice Distretto, Volume Fatturato (m³) → per `district_billed`
3. **Riepilogo_Trimestrale** — somma per trimestre solare, con flag "Completo"/"Parziale", colonna "Utenze Attive" (vedi 2.7) e colonna "Contiene Stime Provvisorie" (vedi 2.10)
4. **Fuori_Periodo** — mesi parziali/incompleti
5. **Dettaglio_Classi** — diviso per classe d'uso
6. **Segnalazioni** — utenze con distretto anomalo
7. **Utenze_Scomparse** — utenze presenti in un file precedente ma non nell'ultimo, con lo stato che avevano (vedi 2.5)
8. **Riepilogo_Comune** — totale del comune intero, mensile, diviso per categoria (vedi 2.8)
9. **Riepilogo_Comune_Trimestre** — lo stesso, per trimestre solare (vedi 2.8)
10. **Utenze_per_Distretto** — foto attuale, utenze per distretto e stato servizio (vedi 2.9)
11. **Utenze_Distretto_Classe** — foto attuale, utenze per distretto, classe d'uso e stato servizio (vedi 2.9)
12. **Anomalie_MetodoB** — letture sentinella, reset contatore non marcati o ritmo di consumo assurdo, escluse dal calcolo (vedi 2.10)
13. **Statistiche_Trimestrali** — dotazione idrica e variazioni % nel tempo, per distretto (nuovo, vedi 2.14)
14. **Statistiche_Classe_Uso** — ripartizione consumi per classe d'uso, per distretto (nuovo, vedi 2.14)
15. **Coefficiente_Punta** — coefficiente di punta stagionale, per distretto (nuovo, vedi 2.14)
16. **Rif_ConsumoProDie** — m³/giorno per ogni confronto tra letture fatto dal Metodo B, non solo le anomalie (nuovo, vedi 2.12)
17. **Cessate_Con_Stima_Finale** — utenze chiuse la cui ultima lettura non è reale (vedi 2.10)

**Rimossi il 16/09/2026** (erano tutti legati al vecchio Metodo A): Sovrapposizioni_Corrette, Confronto_Metodi_Trimestrale, e i due fogli di riferimento Trimestrale_MetodoA_Rif / Riepilogo_Comune_MetodoA_Rif (questi ultimi due erano esistiti solo per un giorno, introdotti il 15/09 e già rimossi il 16/09 su richiesta di Daniele).

Ogni comune ha il proprio Excel di output separato (es. `Volumi_Fatturati_per_Distretto.xlsx` per Belgioioso, `Volumi_Fatturati_MORTARA.xlsx` per Mortara) finché non si decide una struttura unica multi-comune (vedi "Aperto" più sotto).

## 4. Risultato più aggiornato

**Belgioioso** (2 file, motore corretto):
- Finestra affidabile: luglio 2025 – aprile 2026
- 1° trimestre 2026 Metodo A (completo): DBLG01 ≈ 4.576 m³ (64 utenze attive), DBLG02 ≈ 11.127 m³ (419 utenze attive), DBLG03 ≈ 90.242 m³ (2.239 utenze attive)
- 1° trimestre 2026 Metodo B: DBLG01 ≈ 2.910 m³, DBLG02 ≈ 8.473 m³, DBLG03 ≈ 63.856 m³ (dal 41 al 57% in meno del Metodo A)
- Comune di Belgioioso, 1° trimestre 2026 Metodo A: 105.945 m³ nei distretti + 286 m³ case sparse + 366 m³ distretto anomalo = 106.598 m³ totali
- Foto attuale utenze (stato ATT-ATTIVATA): DBLG01 = 64, DBLG02 = 412, DBLG03 = 2.173 (totali con tutti gli stati: 64 / 426 / 2.317)

**Mortara** (3 file, anno intero lug25-giu26 — vedi 2.11 per dettagli):
- Finestra affidabile: 4 trimestri consecutivi completi, luglio 2025 – giugno 2026 (non più una proiezione: è l'anno intero misurato)
- Totale comune, Metodo A: **1.580.066 m³/anno**
- Distretto più grande: DMR10 ≈ 1.194.192 m³/anno Metodo A, ≈ 808.253 m³/anno Metodo B (32,3% più basso — dopo la correzione del bug descritto in 2.11, ora in linea con gli altri distretti)
- Tutti gli altri distretti (DMR01/03/04/05/06/08/09) seguono lo stesso pattern: Metodo B più basso di circa un terzo rispetto al Metodo A

## 5. Aperto / da decidere
- **✅ Risolto (15-16/09/2026)**: quale metodo usare per il bilancio — **Metodo B**, confermato da Daniele in base alla logica di fatturazione reale (si fattura il consumo vero, le stime vengono conguagliate, non sommate — vedi 2.4). Su richiesta esplicita di Daniele il Metodo A è stato rimosso del tutto dal motore e dall'Excel (16/09/2026) — non resta più nemmeno come confronto/diagnostica (vedi sezione 3).
- La riunione con Neta H2O (inizialmente prevista 26/08/2026) non si è ancora svolta (al 15/09/2026) — resta utile per gli altri punti ancora aperti (GG_LETT_PREC, letture sentinella, cambi contatore non marcati), non più per la scelta del metodo. Nel frattempo, restano pronte anche delle domande per un confronto informale con un collega interno (vedi elenco dedicato in coda a questo documento, punti 3-9 ancora validi).
- Tipo di applicativo finale: script vs interfaccia semplice (Streamlit) — non ancora deciso
- **Fatto**: motore testato con successo su un secondo comune (Mortara, vedi 2.11), ora con un anno intero di dati (lug25-giu26, 4 trimestri completi) — struttura del file identica a Belgioioso, nessuna modifica al codice necessaria. Archivio tenuto in un CSV separato (`archivio_letture_mortara.csv`) da quello di Belgioioso; resta da decidere se in futuro conviene un archivio unico multi-comune con colonna LOCALITA, oppure mantenerli separati per comune (probabilmente più semplice da gestire e meno rischioso).
- **Risolto**: l'inversione A/B su DMR10 (Mortara) era un singolo dato sporco (utenza 70342477, vedi 2.11), non un fenomeno reale del distretto — corretta con una terza protezione nel Metodo B (soglia di ritmo m³/giorno).
- **Da riprendere con Daniele**: caso specifico dell'utenza industriale 58051668 di Mortara (vedi 2.4) come esempio concreto ad alto impatto economico del divario Metodo A / Metodo B — utile probabilmente come caso di studio da portare o citare alla riunione Neta H2O.
- Migliorare "Utenze_Scomparse" incrociando il DP per riconoscere i subentri regolari (vedi 2.5) — proposto, non ancora implementato
- Confermare con Daniele come valorizzare due STIMATE finali consecutive nel Metodo B (vedi 2.10)
- **Deciso**: l'archivio CSV non va salvato come documento del progetto Claude (troppo grande, ha superato il limite di spazio). Resta da decidere come far sopravvivere l'archivio tra una chat e l'altra finché l'app non diventa uno script locale vero — per ora Daniele deve tenere a portata di mano i file Excel originali e ricaricarli a ogni nuova chat.

## 6. Interfaccia utente (pianificazione — in parte già online, vedi 6.0)
Decisioni prese con Daniele su come sarà l'app quando avrà un frontend vero:

### 6.0 ✅ Deciso (16/09/2026): il frontend web è il canale PRINCIPALE, l'Excel diventa un export secondario
Finora l'unico "prodotto" dell'applicativo era il file Excel a 17 fogli (Riepilogo_File, Import_WMS, le statistiche, le segnalazioni, ecc.), generato ad ogni elaborazione e aperto a mano da Daniele. Con il frontend web (già iniziato: vedi 6.3, il container `billing` è online su `billing.smarth20.com`), questo cambia:

- **Tutte le tabelle che oggi sono fogli Excel** (riepiloghi per distretto/mese/trimestre, le tre statistiche del punto 2.14, segnalazioni, anomalie, utenze scomparse/cessate, ecc.) diventano **pagine web vere**, consultabili a schermo — non più file da aprire e scorrere manualmente.
- **L'Excel non sparisce**, ma passa in secondo piano: resta disponibile come **pulsante di export/download opzionale** in ciascuna pagina (o uno generale "scarica tutto"), utile per archiviare un mese/trimestre, o per condividere i numeri con qualcuno che non ha accesso al sito. Costa poco tenerlo, dato che `esporta_excel()` in `motore_calcolo.py` esiste già e continua a funzionare: il frontend la richiama solo su richiesta, invece di generarla sempre come prodotto principale.
- **Import_WMS smette di essere "un foglio da esportare"**: i suoi dati restano interni all'applicativo, pronti per il pulsante "a un click" che li invia a WMS SmartH2O via l'API interna sulla rete Docker condivisa (vedi il documento di riepilogo, sezione 4.9) — nessun file di mezzo in quel passaggio.

- **Stile grafico**: simile a WMS SmartH2O — Daniele porterà le impostazioni principali (colori, font, layout) dalla chat/progetto di WMS SmartH2O per copiarle.
- **Scelta tecnica**: FastAPI con pagine HTML/CSS (stesso framework backend già usato da WMS SmartH2O) — confermato, il primo scheletro del servizio FastAPI è già online (vedi 6.3).
- **Caricamento file**: un'unica schermata che accetta PIÙ file Excel insieme in un solo caricamento (non un file per comune, uno alla volta). Il comune di ogni file viene riconosciuto automaticamente dalla colonna LOCALITA dentro il file stesso (non dal nome del file). Un comune o distretto mai visto prima viene gestito automaticamente, senza bisogno di configurazione (il motore già classifica i distretti dal prefisso dominante, vedi 2.2) — pensato esplicitamente per poter aggiungere in futuro altri comuni oltre a Belgioioso e Mortara senza modificare il codice. Dopo il caricamento, una schermata di riepilogo mostra per ogni file: comune riconosciuto, righe lette, eventuali errori.
- **Pagina di diagnostica**: cruscotto che riprende tutto quello che oggi sta nei fogli Excel di segnalazione — per ogni comune: anomalie Metodo B (sentinelle, ritmo assurdo, reset non marcati), utenze scomparse da verificare, cessate con stima finale, quali trimestri sono ancora "provvisori" (vedi 2.10). Obiettivo: vedere a colpo d'occhio se c'è qualcosa da controllare, senza aprire l'Excel (coerente con 6.0: qui è proprio il caso d'uso principale, non un'eccezione).
- **Prompt preparato per Daniele** da portare nella chat/progetto di WMS SmartH2O, per recuperare palette colori, tipografia, framework CSS, struttura di layout, logo/favicon e screenshot di riferimento — in attesa che torni con questi dati prima di costruire qualunque pagina.

### 6.4 Nuovo (16/09/2026): filtri in alto (Comune/Distretto) e gestione utenti/accessi, sull'esempio di WMS SmartH2O
Daniele ha mandato due screenshot di WMS SmartH2O (pagina "Trend", vista "Volume mensile") come esempio concreto di come deve comportarsi il frontend di `billing`. Cosa si vede nello screenshot, e cosa va ripreso:

- **Barra di filtri fissa in alto** (header), sempre visibile, con tendine a cascata: **COMUNE** → **DISTRETTO** ("Tutti i distretti" come opzione per non filtrare) → in WMS c'è anche **MISURATORE** (uno specifico impianto/contatore fisico); su `billing` l'equivalente naturale è probabilmente "classe d'uso" o nessun terzo filtro, da confermare — comunque il principio è: **si sceglie comune e distretto UNA VOLTA nell'header, e tutte le pagine sotto (riepiloghi, statistiche, grafici, segnalazioni) si aggiornano di conseguenza**, senza dover reimpostare il filtro ogni volta che si cambia scheda.
- **Navigazione a schede/tab** sotto l'header (in WMS: Bilancio, Minimo notturno, Connettività, Trend, Analisi dati device, Dati grezzi, Dati Distretti, Manutenzione, Letture manuali, Mappa) — su `billing` le schede saranno quelle già previste al punto 6.0 (i riepiloghi/statistiche che oggi sono fogli Excel), organizzate allo stesso modo: una fila di pulsanti/tab in alto nella pagina.
- **Box di riepilogo numerico** sopra i grafici (in WMS: "Mesi disponibili", "Media mensile", "Totale periodo") — stesso pattern utile anche su `billing`, es. sopra il grafico di un distretto: totale del periodo, utenze attive, dotazione idrica media (vedi le statistiche già definite al punto 2.14).
- **Nota metodologica sotto il titolo del grafico** (in WMS: spiega come viene calcolato il "Volume mensile" mostrato) — buona pratica da riprendere: ogni grafico/tabella di `billing` dovrebbe avere una riga di testo che spiega in breve cosa sta mostrando e con quale criterio (utile soprattutto per distinguere un valore "consolidato" da uno che contiene ancora stime provvisorie, vedi 2.10).

**Gestione accessi/utenti** (richiesta esplicitamente da Daniele, non ancora progettata nei dettagli): nello screenshot di WMS si vede in alto a destra "ACCESSO" con il livello dell'utente collegato (qui: "viewer") e un pulsante "Esci". Per `billing` serve lo stesso concetto:
- Più utenti con login, non un accesso anonimo condiviso.
- Almeno due livelli di permesso: uno di **sola consultazione** (vedere le pagine/tabelle/grafici, senza poter modificare nulla — coerente con "viewer" di WMS) e uno di **editing/operativo** (caricare nuove estrazioni Neta H2O, e soprattutto premere il pulsante "a un click" che invia i dati a WMS SmartH2O, vedi 6.0 e il documento di riepilogo sezione 4.9 — un'azione che scrive dati reali, quindi da riservare a chi ha i permessi giusti).
- Da decidere con Daniele, prima di costruire: quanti livelli servono davvero (es. serve anche un livello "amministratore" che gestisce gli utenti stessi?), e se il sistema di login può essere condiviso/riusato con quello già esistente di WMS SmartH2O oppure va costruito da zero per `billing`. Non ancora implementato: oggi il container `billing` non ha nessuna autenticazione (vedi 6.3, è solo uno scheletro con `/health`).

### 6.3 ✅ Fatto (16/09/2026): primo scheletro online
Container Docker `billing`, sulla VPS di Daniele, collegato alla rete Docker interna condivisa con WMS SmartH2O e raggiungibile pubblicamente su `billing.smarth20.com` (HTTPS automatico via Caddy). Oggi è solo un health-check (`/health`) — nessuna pagina vera ancora. Dettagli tecnici completi (nomi container, rete, Caddyfile) nel documento di riepilogo, sezioni 4.9 e 4.1.

### 6.1 Tabelle anagrafiche/di riferimento (in arrivo da Daniele: elenco comuni, elenco distretti)
Daniele fornirà elenchi ufficiali di comuni e distretti (probabilmente lo stesso file "Configurazione Distretti" già caricato in WMS SmartH2O, Fase 2). Distinzione importante nello schema del futuro database: dati che cambiano SPESSO (le letture, la parte che cresce ogni trimestre) vanno tenuti separati da dati "statici"/di riferimento che cambiano RARAMENTE (comuni, distretti, prese/DP) — un aggiornamento dell'elenco distretti non deve toccare lo storico delle letture.

Con l'elenco ufficiale sarà possibile sostituire l'euristica attuale (2.2: "valido" = prefisso più frequente nel file) con una verifica esatta contro la lista vera dei codici distretto — distinguendo con certezza le case sparse (nessun distretto, atteso) dalle vere anomalie (codice che non esiste nell'elenco ufficiale).

**Da costruire anche**: uno storico delle associazioni DP↔utenza (le "prese", vedi §1 e 2.5) — una tabella che tiene traccia di quale utenza è/era collegata a quale DP nel tempo, per riconoscere in modo pulito i subentri (proposto in 2.5, non ancora implementato) invece di doverlo dedurre ad ogni calcolo.

**Il distretto di un punto/utenza può cambiare nel tempo** (confermato da Daniele: sia un'utenza sia un punto/DP possono passare da un distretto all'altro, non solo comparire/sparire dalla lista "NODMA"/case sparse). Il motore, così com'è oggi, gestisce già questo caso in modo ragionevole SENZA bisogno di una tabella storica dedicata, grazie a come è costruito il Metodo B: ogni periodo (differenza tra due letture reali consecutive, vedi 2.10) prende il valore di DISTRETTO dalla lettura che CHIUDE il periodo (la più recente delle due), non da quella che lo apre. Quindi se un'utenza cambia distretto tra una lettura e la successiva, il consumo di quel periodo viene attribuito automaticamente al distretto NUOVO (quello dichiarato sulla lettura più recente) — e i periodi successivi continueranno a usarlo, finché non cambia di nuovo. Non serve nessuna tabella storica separata per questo: il dato "punto-nel-tempo" è già dentro ogni lettura del file Neta H2O.

**Una semplificazione nota**, da tenere presente: se il cambio di distretto avviene A META' di un periodo (tra le due letture reali che lo delimitano), oggi TUTTO il consumo di quel periodo va al distretto nuovo — non viene spezzato proporzionalmente tra il distretto vecchio e quello nuovo in base a quando esattamente è avvenuto il cambio (che spesso non è nemmeno noto con precisione, essendo un'informazione anagrafica, non una lettura). Per la maggior parte dei casi (cambi rari, periodi brevi) l'impatto è trascurabile; se in futuro servisse più precisione, si potrebbe valutare uno storico dei cambi di distretto con relativa data, da incrociare con le date dei periodi — non necessario per ora.

### 6.2 Grafici (pianificazione, su richiesta di Daniele — consumi mensili/trimestrali per comune e distretto)
Daniele ha chiesto che l'app produca grafici, stesso stile di WMS SmartH2O (vedi documento `style-guide-wms-smarth2o.md`: Chart.js 4.4.1 dentro `.chart-wrap`, palette teal/ambra/rosso), e che i grafici tengano conto anche dei punti senza distretto valido (case sparse tipo "NO DISTRETTO"/"NODMA") e dei casi anomali — non solo dei distretti veri e propri. Proposta (da confermare con Daniele prima di costruirla):

1. **Andamento mensile per distretto** (grafico a linee, uno per comune): una linea per ogni distretto valido, più due linee aggiuntive in colore diverso — "Case sparse" in grigio (stesso grigio neutro del badge `.nodata`) e "Distretto anomalo/mancante" in rosso (`--red`, stesso schema di `.lvl-critico`) — così anche i punti senza distretto vero restano visibili nel tempo, invece di sparire dal grafico. Dati: stessi del foglio Riepilogo_Comune (2.8).
2. **Totale comune per mese** (grafico a barre impilate, in cima alla pagina del comune): tre segmenti per barra — Volume nei Distretti, Volume Case Sparse, Volume Distretto Anomalo — stessa scomposizione del foglio Riepilogo_Comune, così il totale del comune è sempre leggibile a colpo d'occhio insieme alla sua composizione.
3. **Andamento trimestrale per distretto, con evidenza delle stime provvisorie** (grafico a barre): ripropone in forma visiva il foglio Riepilogo_Trimestrale. I trimestri ancora "provvisori" (colonna "Contiene Stime Provvisorie" = Sì) vengono segnalati graficamente — proposta: barra con bordo tratteggiato o un pallino ambra sopra la barra (stesso colore di `.stimato`/`--amber`), così si vede subito quali numeri potrebbero ancora cambiare in un ricalcolo futuro, senza dover aprire l'Excel. *(Aggiornato 16/09/2026: era originariamente pensato come confronto Metodo A vs B, non più applicabile dato che il Metodo A è stato rimosso — ora è solo l'andamento del Metodo B nel tempo, con la stessa segnalazione dei trimestri provvisori.)*
4. Ogni grafico ha un selettore comune/distretto in alto (coerente con i filtri già presenti nell'header di WMS SmartH2O) e vive nella pagina principale dell'app; un riepilogo equivalente ma più sintetico (senza dettaglio per distretto) può comparire anche nella pagina di diagnostica.

Nessun grafico è stato ancora costruito (l'app non ha ancora un frontend, vedi sopra) — questa è la proposta di cosa costruire, da confermare/aggiustare con Daniele prima di iniziare lo sviluppo vero.

## 7. Domande per un confronto informale con un collega interno (in attesa della riunione Neta H2O)
La riunione con Neta H2O non si è ancora svolta. Nel frattempo, queste domande sono pensate per un collega interno (es. ufficio fatturazione, che già in passato ha spiegato la logica del mc/giorno) — taglio pratico/operativo, non tecnico-schema come quelle per Neta H2O (vedi 2.4).

**Sul modo di lavorare della fatturazione**
1. ~~Domanda principale sul conguaglio...~~ — **RISOLTA direttamente da Daniele (15/09/2026, vedi 2.4)**, non serve più chiederla al collega: si fattura il consumo vero misurato dal contatore, le stime sono provvisorie e vengono conguagliate (corrette/assorbite) dalla lettura reale successiva, non sommate ad essa. Questo conferma il Metodo B come corretto.
2. Resta comunque utile, se capita l'occasione informale, un controllo di conferma pratico: è mai capitato che un cliente notasse un importo fatturato più alto del previsto proprio a causa di una stima non correttamente conguagliata (es. un errore del sistema, non della regola in sé)? Utile più che altro per intercettare eventuali eccezioni/bug nel gestionale di fatturazione, non per la regola generale (già chiarita).

**Sul caso concreto dell'utenza industriale 58051668 (Mortara)**
3. Una singola lettura RIMOZIONE PER CAMBIO che dichiara 22.652 m³ su 547 giorni dichiarati, quando il contatore fisicamente (nel periodo che vediamo noi) si è mosso solo di 14.115 m³ in 292 giorni: a voi risulta plausibile? Sapreste dire se quel dato include davvero un recupero di un periodo precedente non presente nel nostro estratto?
4. Per un'utenza industriale come questa, esiste una procedura diversa di stima rispetto a un'utenza domestica?

**Sulle anomalie tecniche scoperte nei dati**
5. Avete mai sentito parlare di letture con valore 999999 (o simili, tipo 9999989) nel sistema Neta H2O? Per noi sembrano un codice di "lettura non disponibile", confermate?
6. È normale che il numero di lettura di un contatore scenda da una lettura alla successiva SENZA che ci sia una riga di tipo INIZIALE ESCLUSO/RIMOZIONE PER CAMBIO a segnalare il cambio? Ne abbiamo trovati circa 180 casi solo a Belgioioso.
7. Alla cessazione di un contratto, è sempre obbligatoria una lettura reale (non stimata)? Abbiamo trovato una quarantina di casi a Mortara e una ventina a Belgioioso dove l'ultima lettura nota di un'utenza cessata è una stima.

**Sul distretto DMR10 di Mortara (risolto da soli, ma resta un buon esempio da citare)**
8. ~~Nel distretto DMR10...~~ — RISOLTO autonomamente: era un solo dato sporco (utenza 70342477, LETTURA che salta da 0 a 999.996 in 80 giorni, un ritmo di 12.500 m³/giorno). Guardando i dati grezzi si vede anche che qui la MATRICOLA cambia (da 2572511927 a 2572525161) SENZA nessuna riga INIZIALE/RIMOZIONE a segnalarlo — un altro caso di "cambio contatore non marcato" — e che il nuovo contatore, invece di ripartire da un valore basso, riporta subito un valore vicino a 999.999 e poi resta praticamente fermo per mesi: sembra un modulo di telelettura guasto/irraggiungibile che restituisce un codice di errore leggermente diverso dal sentinella esatto 999999. Domanda per il collega: esistono altri codici di errore "vicini a 999999" (es. 999.996, 999.998) usati da certi moduli radio o tipi di contatore, diversi dal valore esatto 999999?

**Sul collegamento distretto-utenza**
9. Il campo DISTRETTO che troviamo già dentro l'estrazione Neta H2O (es. DBLG03, DMR10): è aggiornato/affidabile allo stesso modo per tutti i comuni, o ci sono comuni dove questo campo è meno curato?
