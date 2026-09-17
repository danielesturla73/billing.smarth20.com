"""
Motore di calcolo — Applicativo Fatturazione Utenze
=====================================================

Legge una o più estrazioni Excel dal CRM Neta H2O (una per comune, stessa
struttura di colonne) e calcola il volume fatturato per distretto per mese,
distinguendo anche per classe d'uso (PRODOTTO_CODICE).

LOGICA DI FONDO
----------------
Ogni riga del file Neta H2O NON è "il consumo del mese X": è una LETTURA
del contatore, con un CONSUMO che copre un periodo di GG_LETT_PREC giorni
che termina in DATA_LETTURA. Questo periodo può benissimo attraversare più
mesi di calendario (es. 76 giorni tra fine agosto e metà novembre).

Per ottenere un valore "mensile" corretto, il consumo di ogni lettura viene
quindi RIPARTITO (prorata) sui mesi coperti, in proporzione al numero di
giorni di calendario che ricadono in ciascun mese. Questo è il metodo
standard usato nei bilanci idrici per confrontare dati non sincronizzati
(fatturazione bimestrale/quadrimestrale vs bilancio mensile dei misuratori).

GESTIONE DISTRETTO
-------------------
- Un valore di DISTRETTO che inizia con lo stesso prefisso dei distretti
  dominanti nel file (es. "DBLG" per l'estrazione di Belgioioso) è
  considerato valido.
- "NO DISTRETTO" è una condizione ATTESA (case sparse, fuori dalla rete
  distrettualizzata): il volume viene ESCLUSO dal totale per distretto, ma
  NON è un errore.
- Qualunque altro valore (prefisso di un altro comune, "*", vuoto, ecc.) è
  considerato un potenziale ERRORE ANAGRAFICO e viene:
    1) escluso dal totale del distretto (per non sporcare il bilancio),
    2) elencato nel foglio "Segnalazioni" del file di output, per la
       verifica manuale da parte dell'ufficio tecnico.
"""

from __future__ import annotations

import itertools
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

COLONNE_ATTESE = [
    "CODICE_SERVIZIO", "LEGAMI_FORNITURA", "PRODOTTO_CODICE",
    "INDIRIZZO_UBICAZIONE", "CAP_UBICAZIONE", "LOCALITA", "STATO_SERVIZIO",
    "DATA_INIZIO_FORNITURA", "DATA_FINE_FORNITURA", "DP", "Latitudine",
    "Longitudine", "DISTRETTO", "MATRICOLA", "MODULO_RADIO", "BASE_COMP",
    "DATA_DECORR_BC", "DATA_LETTURA", "LETTURA", "CONSUMO", "GG_LETT_PREC",
    "COD_TIP_LETT", "TIPO_LETTURA", "COD_STATO_LETT", "STATO_LETTURA",
    "DATA_FATTURAZ_LETTURA",
]


@dataclass
class RisultatoElaborazione:
    """Contenitore dei risultati per uno o più file elaborati insieme.

    Dal 16/09/2026 l'applicativo usa SOLO il METODO B (confermato da
    Daniele: si fattura il consumo vero misurato dal contatore, le stime
    vengono conguagliate non sommate — vedi documento di specifica, 2.4).
    Su richiesta esplicita di Daniele, il vecchio Metodo A e' stato
    rimosso del tutto dal codice, anche come confronto/diagnostica: non
    esiste piu' nessun campo, calcolo o foglio Excel basato su di esso.
    """
    volumi_distretto_mese: pd.DataFrame           # per import in WMS SmartH2O (mesi affidabili)
    volumi_distretto_trimestre: pd.DataFrame      # per trimestre solare + utenze attive + flag stime provvisorie
    volumi_fuori_periodo: pd.DataFrame            # mesi parziali/incompleti, tenuti separati
    volumi_distretto_mese_classe: pd.DataFrame    # dettaglio per classe d'uso
    segnalazioni: pd.DataFrame                    # anomalie da verificare
    utenze_scomparse: pd.DataFrame                # utenze presenti in un file precedente ma non nell'ultimo, con lo stato che avevano
    volumi_comune_mese: pd.DataFrame              # totale comune per mese, con dettaglio validi/case sparse/anomalie
    volumi_comune_trimestre: pd.DataFrame         # lo stesso, sommato per trimestre solare (solo mesi affidabili)
    utenze_per_distretto: pd.DataFrame            # foto attuale: utenze per distretto e stato servizio
    utenze_per_distretto_classe: pd.DataFrame     # foto attuale: utenze per distretto, classe d'uso e stato servizio
    anomalie_metodo_b: pd.DataFrame               # letture sentinella o reset non marcati, escluse dal Metodo B
    riferimento_prodie: pd.DataFrame              # m3/giorno per ogni confronto tra letture del Metodo B (OK e non), a colpo d'occhio
    statistiche_trimestrali: pd.DataFrame         # dotazione idrica e variazioni % (trim. precedente, stesso trim. anno prima) per distretto
    statistiche_classe_uso: pd.DataFrame          # ripartizione consumi per classe d'uso, per distretto
    coefficiente_punta: pd.DataFrame              # coefficiente di punta stagionale (portata mese di punta / portata media) per distretto
    cessate_con_stima_finale: pd.DataFrame        # utenze chiuse la cui ultima lettura non e' reale (anomalia)
    riepilogo_file: pd.DataFrame                  # una riga per file caricato
    warning: list[str] = field(default_factory=list)


def carica_estrazione(path: str | Path) -> pd.DataFrame:
    """Legge un file Excel di estrazione Neta H2O e valida le colonne."""
    path = Path(path)
    df = pd.read_excel(path, sheet_name=0)

    mancanti = [c for c in COLONNE_ATTESE if c not in df.columns]
    if mancanti:
        raise ValueError(
            f"Il file '{path.name}' non ha la struttura attesa. "
            f"Colonne mancanti: {mancanti}"
        )

    df = df.copy()
    df["FILE_ORIGINE"] = path.name
    df["DATA_LETTURA"] = pd.to_datetime(df["DATA_LETTURA"])
    df["DISTRETTO"] = df["DISTRETTO"].astype(str).str.strip()
    return df


def comune_dominante(df: pd.DataFrame) -> str:
    """Comune riconosciuto per un file/gruppo di righe, dal valore più
    frequente della colonna LOCALITA (non dal nome del file — vedi
    specifiche, sezione 6.0). Usato sia dal motore (riepilogo per file)
    sia dall'endpoint di upload (per scegliere l'archivio storico giusto).
    """
    moda = df["LOCALITA"].mode()
    return moda.iat[0] if not moda.empty else ""


def _prefisso_dominante(distretti: pd.Series) -> str:
    """Trova il prefisso alfabetico più frequente tra i codici distretto
    validi (es. 'DBLG' da 'DBLG03'), usato per riconoscere i distretti
    del comune corrente rispetto a quelli di comuni limitrofi.
    """
    validi = distretti[~distretti.isin(["NO DISTRETTO", "*", "nan", ""])]
    prefissi = validi.str.extract(r"^([A-Za-z]+)")[0].dropna()
    if prefissi.empty:
        return ""
    return prefissi.value_counts().idxmax()


def classifica_distretto(df: pd.DataFrame) -> pd.DataFrame:
    """Aggiunge le colonne CATEGORIA_DISTRETTO e MOTIVO_SEGNALAZIONE.

    CATEGORIA_DISTRETTO puo' essere:
      - 'valido'        -> entra regolarmente nel calcolo
      - 'case_sparse'    -> punto non distrettualizzato (case isolate,
                            frazioni senza distretto...), escluso dai
                            volumi per distretto ma NON e' un errore
      - 'anomalia'       -> possibile errore anagrafico, da segnalare

    Il codice usato da Neta H2O per "punto non distrettualizzato" non e'
    uguale ovunque: a Belgioioso/Mortara e' il testo letterale "NO
    DISTRETTO", ma Daniele ha confermato che altri comuni possono usare
    convenzioni diverse (es. "NODMA"). Finche' non arriva l'elenco
    ufficiale comuni/distretti (vedi specifiche, sezione 6.1), si usa
    un'euristica piu' larga: qualunque codice che inizia per "NO" (case
    sparse/non distrettualizzato, per convenzione) e non e' il prefisso
    valido del comune viene trattato come case_sparse, non anomalia.
    Andra' sostituita da un controllo esatto contro l'elenco ufficiale
    quando sara' disponibile.
    """
    df = df.copy()
    prefisso = _prefisso_dominante(df["DISTRETTO"])

    def categoria(val: str) -> str:
        if prefisso and val.startswith(prefisso):
            return "valido"
        val_pulito = str(val).strip().upper()
        if val_pulito.startswith("NO") and val_pulito not in ("", "NAN"):
            return "case_sparse"
        return "anomalia"

    df["CATEGORIA_DISTRETTO"] = df["DISTRETTO"].apply(categoria)

    def motivo(row):
        if row["CATEGORIA_DISTRETTO"] != "anomalia":
            return ""
        if row["DISTRETTO"] in ("*", "nan", ""):
            return "Codice distretto mancante o non valido"
        return f"Distretto '{row['DISTRETTO']}' non appartiene al comune di questa estrazione (prefisso atteso '{prefisso}')"

    df["MOTIVO_SEGNALAZIONE"] = df.apply(motivo, axis=1)
    return df


# ---------------------------------------------------------------------------
# METODO B: differenza di letture, con le letture REALI che prevalgono
# sempre sulle STIMATA (proposto da Daniele dopo il confronto con i colleghi
# della fatturazione). E' l'UNICO metodo usato dall'applicativo dal
# 15/09/2026 (confermato da Daniele: si fattura il consumo vero, le stime
# vengono conguagliate non sommate — vedi documento di specifica, 2.4). Il
# vecchio Metodo A (basato su CONSUMO/GG_LETT_PREC dichiarati) e' stato
# rimosso dal codice il 16/09/2026 su richiesta esplicita di Daniele ("non
# lo devi prendere mai in considerazione, nemmeno per confronto") — non
# resta piu' nessun calcolo ne' foglio Excel basato su di esso. Qui si
# ignorano CONSUMO e GG_LETT_PREC quasi sempre, e si usa solo la differenza
# tra i valori di LETTURA di due letture REALI consecutive. Vedi
# discussione con Daniele del 25/08/2026 per la regola completa e
# l'esempio numerico (utenza 53587909) usato per validarla.
# ---------------------------------------------------------------------------

# Letture "reali" (misurate fisicamente): fanno sempre da ancora per il
# calcolo della differenza, e cancellano qualunque STIMATA/RIPROPORZIONATA/
# SPEZZATURA che trovano in mezzo. NOTA: RIPROPORZIONATA e SPEZZATURA sono
# trattate come "stimate" per ipotesi (nomi che suggeriscono un valore
# ricalcolato, non una lettura fisica) — da confermare con Neta H2O.
TIPI_LETTURA_REALE = frozenset({
    "LETTURA EFFETTIVA", "RIMOZIONE PER CAMBIO", "FINALE",
    "CHIUSURA PER MOROSITA'", "APERTURA DA SOSPENSIONE FORNITURA",
    "LETTURA A GIRO FF - CO - VF",
})
# Segnano l'inizio di un contatore nuovo (il contatore vecchio si azzera):
# interrompono sempre la catena, non si calcola mai una differenza di
# lettura tra un contatore e l'altro.
TIPI_INIZIO_CONTATORE = frozenset({"INIZIALE ESCLUSO", "INIZIALE INCLUSO"})

# Ordine di priorità per letture con la STESSA data (es. RIMOZIONE PER
# CAMBIO e INIZIALE ESCLUSO/INCLUSO dello stesso giorno): prima si chiude
# il contatore vecchio (reale), poi si apre il nuovo.
_PRIORITA_STESSA_DATA = {
    "RIMOZIONE PER CAMBIO": 0, "FINALE": 0, "CHIUSURA PER MOROSITA'": 0,
    "INIZIALE ESCLUSO": 2, "INIZIALE INCLUSO": 2,
}


def _ordina_priorita_stessa_data(df: pd.DataFrame) -> pd.DataFrame:
    """Ordina le letture per utenza e data, applicando la stessa regola
    di priorita' per due letture della stessa utenza con la STESSA data
    usata da calcola_periodi_metodo_b (vedi _PRIORITA_STESSA_DATA: prima
    chi chiude un contatore vecchio, poi chi apre quello nuovo). Usata
    anche da trova_cessate_con_stima_finale e trova_utenze_scomparse per
    essere coerenti su qual e' davvero "l'ultima lettura" di un'utenza:
    un semplice sort_values("DATA_LETTURA") non lo garantisce quando due
    letture cadono nello stesso giorno (l'ordine tra le due, altrimenti,
    dipenderebbe dall'ordine delle righe in ingresso, non dalla data).
    """
    df = df.copy()
    df["_PRIORITA"] = df["TIPO_LETTURA"].map(_PRIORITA_STESSA_DATA).fillna(1)
    return df.sort_values(["CODICE_SERVIZIO", "DATA_LETTURA", "_PRIORITA"])


# Sopra questa soglia, il valore di LETTURA e' un codice "sentinella" di
# Neta H2O per "lettura non disponibile/contatore non raggiungibile", non
# una lettura vera (scoperto testando il Metodo B su tutto l'archivio:
# valori come 999999 o 9999999, sempre con CONSUMO=0 sulla stessa riga).
# Una lettura sentinella non puo' MAI fare da ancora.
LETTURA_SENTINELLA_MIN = 999_999

# Soglia di sanita' sul RITMO di consumo (m3/giorno) implicito in una
# singola differenza tra due ancore reali. Scoperta analizzando DMR10 a
# Mortara (utenza 70342477): un valore di LETTURA come 999996 o 999998 NON
# supera la soglia sentinella sopra (999999), ma se la lettura precedente
# nota era vicina a 0 il "consumo" implicito e' comunque assurdo (in quel
# caso: quasi 1.000.000 m3 in 80 giorni, 12.500 m3/giorno). Il valore piu'
# alto genuino trovato in tutto l'archivio (un'utenza industriale reale) e'
# ~139 m3/giorno: questa soglia sta ben sopra, con ampio margine, senza
# escludere consumi industriali legittimi.
MC_GIORNO_SANITA_MASSIMA = 500


def calcola_periodi_metodo_b(df_tutti: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Per ogni utenza, calcola i periodi di consumo secondo il Metodo B:

    1. Le letture si dividono in SEGMENTI delimitati dai cambi di
       contatore (TIPI_INIZIO_CONTATORE apre un segmento nuovo): non si
       confrontano mai letture di contatori diversi.
    2. Dentro ogni segmento, la prima lettura nota (di qualunque tipo) è
       il punto di partenza (BASE): non genera consumo da sola, serve solo
       come riferimento — non sappiamo cosa è successo prima di lei.
    3. Ogni lettura REALE (TIPI_LETTURA_REALE) con una LETTURA valida (non
       sentinella, vedi LETTURA_SENTINELLA_MIN) è un'ancora: il consumo tra
       due ancore consecutive (o tra la base e la prima ancora) è la
       differenza tra le due LETTURA, spalmata sui giorni di calendario
       tra le due date.
    4. Una STIMATA (o assimilata, o una REALE con lettura sentinella) "in
       mezzo" tra due ancore viene cancellata: non conta, non sposta
       l'ancora.
    5. Se l'ULTIMA lettura nota di un segmento è una STIMATA (non ancora
       seguita da una reale), la si considera comunque, usando il suo
       CONSUMO e GG_LETT_PREC dichiarati così come sono (non essendoci una
       lettura reale successiva con cui calcolare una differenza).
    6. PROTEZIONE aggiunta dopo aver trovato reset di contatore NON
       marcati da INIZIALE/RIMOZIONE (la LETTURA scende bruscamente senza
       nessun segnale esplicito nel file): se la differenza tra due ancore
       risulta NEGATIVA, non è un consumo valido (un contatore dell'acqua
       non torna mai indietro fuori da un cambio). Il periodo viene
       escluso dal totale e finisce nel secondo DataFrame restituito
       (anomalie), invece che nel calcolo. L'ancora si sposta comunque
       alla nuova lettura, per non propagare l'errore in avanti.

    Restituisce (periodi, anomalie, riferimento): periodi pronto per
    _ripartisci_su_mesi; anomalie con le differenze negative escluse, da
    controllare a mano (probabile reset di contatore non marcato, o
    lettura sentinella non riconosciuta); riferimento e' una tabella di
    controllo con il ritmo di consumo (m3/giorno, "consumo pro-die") per
    OGNI confronto tra letture fatto dal Metodo B — non solo quelli
    esclusi per anomalia, ma anche quelli normali — cosi' si puo' vedere
    a colpo d'occhio se un valore e' in linea senza dover aprire il
    foglio delle sole anomalie (richiesto da Daniele il 16/09/2026, dopo
    aver verificato che il campo GG_LETT_PREC dichiarato nel file non e'
    abbastanza affidabile da soli per questo controllo — vedi 2.11).
    """
    df = _ordina_priorita_stessa_data(df_tutti)

    periodi = []
    anomalie = []
    riferimento = []

    # Un solo giro di itertuples() su tutto il df ordinato, invece di uno
    # per ogni utenza (df.groupby(...).itertuples() dentro un ciclo): con
    # migliaia di utenze il costo fisso di ogni chiamata a itertuples()
    # (che ricostruisce gli array per colonna) dominava il tempo totale.
    # df e' gia' ordinato per CODICE_SERVIZIO, quindi itertools.groupby
    # sul risultato flat produce esattamente gli stessi gruppi di prima.
    tutte_le_righe = df.itertuples(index=False)
    for _, gruppo_righe in itertools.groupby(tutte_le_righe, key=lambda r: r.CODICE_SERVIZIO):
        righe = list(gruppo_righe)
        segmento: list = []

        def chiudi_segmento(segmento):
            if not segmento:
                return
            base = segmento[0]
            ancora_lettura, ancora_data = base.LETTURA, base.DATA_LETTURA
            indice_ultima_ancora = 0  # indice in segmento dell'ultima ancora reale (0 = base)

            for indice, riga in enumerate(segmento[1:], start=1):
                e_reale = riga.TIPO_LETTURA in TIPI_LETTURA_REALE
                e_sentinella = riga.LETTURA >= LETTURA_SENTINELLA_MIN
                if e_reale and e_sentinella:
                    anomalie.append({
                        "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_LETTURA": riga.DATA_LETTURA,
                        "LETTURA": riga.LETTURA, "TIPO_LETTURA": riga.TIPO_LETTURA,
                        "MOTIVO": "Lettura sentinella (valore non valido, ignorata)",
                        "DISTRETTO": riga.DISTRETTO, "FILE_ORIGINE": riga.FILE_ORIGINE,
                    })
                elif e_reale:
                    giorni = (riga.DATA_LETTURA - ancora_data).days
                    delta = riga.LETTURA - ancora_lettura
                    ritmo = (delta / giorni) if giorni > 0 else 0
                    if giorni > 0 and delta < 0:
                        anomalie.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_LETTURA": riga.DATA_LETTURA,
                            "LETTURA": riga.LETTURA, "TIPO_LETTURA": riga.TIPO_LETTURA,
                            "MOTIVO": f"Lettura diminuita rispetto alla precedente ({ancora_lettura}->{riga.LETTURA}), probabile reset contatore non marcato",
                            "DISTRETTO": riga.DISTRETTO, "FILE_ORIGINE": riga.FILE_ORIGINE,
                        })
                        riferimento.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_FINE": riga.DATA_LETTURA,
                            "GIORNI": giorni, "VOLUME_M3": delta, "M3_GIORNO": round(ritmo, 2),
                            "LOCALITA": riga.LOCALITA, "DISTRETTO": riga.DISTRETTO, "FILE_ORIGINE": riga.FILE_ORIGINE,
                            "ESITO": "Escluso: lettura diminuita (probabile reset contatore non marcato)",
                        })
                    elif giorni > 0 and ritmo > MC_GIORNO_SANITA_MASSIMA:
                        anomalie.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_LETTURA": riga.DATA_LETTURA,
                            "LETTURA": riga.LETTURA, "TIPO_LETTURA": riga.TIPO_LETTURA,
                            "MOTIVO": f"Ritmo di consumo implicito assurdo ({ritmo:,.0f} m3/giorno: {ancora_lettura}->{riga.LETTURA} in {giorni} giorni), probabile lettura anomala/codice placeholder non riconosciuto come sentinella",
                            "DISTRETTO": riga.DISTRETTO, "FILE_ORIGINE": riga.FILE_ORIGINE,
                        })
                        riferimento.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_FINE": riga.DATA_LETTURA,
                            "GIORNI": giorni, "VOLUME_M3": delta, "M3_GIORNO": round(ritmo, 2),
                            "LOCALITA": riga.LOCALITA, "DISTRETTO": riga.DISTRETTO, "FILE_ORIGINE": riga.FILE_ORIGINE,
                            "ESITO": "Escluso: ritmo implausibile (sopra soglia di sanita')",
                        })
                    elif giorni > 0:
                        periodi.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_FINE": riga.DATA_LETTURA,
                            "GIORNI": giorni, "VOLUME_M3": delta,
                            "LOCALITA": riga.LOCALITA, "DISTRETTO": riga.DISTRETTO,
                            "CATEGORIA_DISTRETTO": riga.CATEGORIA_DISTRETTO,
                            "PRODOTTO_CODICE": riga.PRODOTTO_CODICE, "FILE_ORIGINE": riga.FILE_ORIGINE,
                            "ORIGINE": "differenza letture reali",
                        })
                        riferimento.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_FINE": riga.DATA_LETTURA,
                            "GIORNI": giorni, "VOLUME_M3": delta, "M3_GIORNO": round(ritmo, 2),
                            "LOCALITA": riga.LOCALITA, "DISTRETTO": riga.DISTRETTO, "FILE_ORIGINE": riga.FILE_ORIGINE,
                            "ESITO": "OK",
                        })
                    ancora_lettura, ancora_data = riga.LETTURA, riga.DATA_LETTURA
                    indice_ultima_ancora = indice
                # le stimate/assimilate "in mezzo" tra due ancore reali non
                # fanno nulla qui: restano cancellate, l'ancora non si
                # muove. Se pero' non arriva PIU' nessuna reale a chiudere
                # il segmento, vengono comunque contate in coda (sotto),
                # come valore provvisorio.

            # Coda: se il segmento non si chiude con una lettura reale
            # (nessuna reale e' ancora arrivata dopo l'ultima ancora nota),
            # tutte le stimate pendenti da li' in poi sono "in attesa di
            # conferma". Non sappiamo ancora se una lettura reale futura le
            # confermera' cosi' come sono o le correggera' (conguaglio):
            # anziche' scartarle o tenere solo l'ultima, le contiamo TUTTE
            # coi loro valori dichiarati, come valore PROVVISORIO — una
            # stima e' comunque meglio di uno zero. Quando arrivera' una
            # lettura reale (prossima estrazione), il ricalcolo automatico
            # su tutto l'archivio (vedi 2.6) le sostituira' da solo con la
            # differenza fisica reale, perche' il segmento verra' rifatto
            # da capo: non serve nessuna logica di "sovrascrittura" a parte.
            ultima = segmento[-1]
            if ultima.TIPO_LETTURA not in TIPI_LETTURA_REALE:
                for riga in segmento[indice_ultima_ancora + 1:]:
                    if riga.GG_LETT_PREC > 0:
                        periodi.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_FINE": riga.DATA_LETTURA,
                            "GIORNI": int(riga.GG_LETT_PREC), "VOLUME_M3": riga.CONSUMO,
                            "LOCALITA": riga.LOCALITA, "DISTRETTO": riga.DISTRETTO,
                            "CATEGORIA_DISTRETTO": riga.CATEGORIA_DISTRETTO,
                            "PRODOTTO_CODICE": riga.PRODOTTO_CODICE, "FILE_ORIGINE": riga.FILE_ORIGINE,
                            "ORIGINE": "stima provvisoria (in attesa di lettura reale che confermi il periodo)",
                        })
                        riferimento.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_FINE": riga.DATA_LETTURA,
                            "GIORNI": int(riga.GG_LETT_PREC), "VOLUME_M3": riga.CONSUMO,
                            "M3_GIORNO": round(riga.CONSUMO / riga.GG_LETT_PREC, 2),
                            "LOCALITA": riga.LOCALITA, "DISTRETTO": riga.DISTRETTO, "FILE_ORIGINE": riga.FILE_ORIGINE,
                            "ESITO": "Stima provvisoria (in attesa di lettura reale, valore dichiarato nel file)",
                        })

        for riga in righe:
            if riga.TIPO_LETTURA in TIPI_INIZIO_CONTATORE and segmento:
                chiudi_segmento(segmento)
                segmento = [riga]
            else:
                segmento.append(riga)
        chiudi_segmento(segmento)

    colonne = [
        "CODICE_SERVIZIO", "DATA_FINE", "GIORNI", "VOLUME_M3", "LOCALITA", "DISTRETTO",
        "CATEGORIA_DISTRETTO", "PRODOTTO_CODICE", "FILE_ORIGINE", "ORIGINE",
    ]
    colonne_anomalie = [
        "CODICE_SERVIZIO", "DATA_LETTURA", "LETTURA", "TIPO_LETTURA", "MOTIVO",
        "DISTRETTO", "FILE_ORIGINE",
    ]
    colonne_riferimento = [
        "CODICE_SERVIZIO", "DATA_FINE", "GIORNI", "VOLUME_M3", "M3_GIORNO",
        "LOCALITA", "DISTRETTO", "FILE_ORIGINE", "ESITO",
    ]
    df_periodi = pd.DataFrame(periodi)[colonne] if periodi else pd.DataFrame(columns=colonne)
    df_anomalie = pd.DataFrame(anomalie)[colonne_anomalie] if anomalie else pd.DataFrame(columns=colonne_anomalie)
    df_riferimento = (
        pd.DataFrame(riferimento)[colonne_riferimento] if riferimento else pd.DataFrame(columns=colonne_riferimento)
    )
    return df_periodi, df_anomalie, df_riferimento


def prorata_mensile_metodo_b(periodi: pd.DataFrame) -> pd.DataFrame:
    """Come prorata_mensile, ma a partire dai periodi del Metodo B
    (calcola_periodi_metodo_b) invece che dalle letture grezze.
    """
    righe = []
    for row in periodi.itertuples(index=False):
        ripartizione = _ripartisci_su_mesi(row.DATA_FINE, int(row.GIORNI))
        totale_giorni = sum(g for _, g in ripartizione)
        for mese, giorni in ripartizione:
            quota = row.VOLUME_M3 * (giorni / totale_giorni) if totale_giorni else 0
            righe.append({
                "CODICE_SERVIZIO": row.CODICE_SERVIZIO,
                "LOCALITA": row.LOCALITA,
                "DISTRETTO": row.DISTRETTO,
                "CATEGORIA_DISTRETTO": row.CATEGORIA_DISTRETTO,
                "PRODOTTO_CODICE": row.PRODOTTO_CODICE,
                "MESE": mese,
                "GIORNI_NEL_MESE": giorni,
                "VOLUME_MESE_M3": quota,
                "FILE_ORIGINE": row.FILE_ORIGINE,
                "ORIGINE": row.ORIGINE,
            })
    return pd.DataFrame(righe)


def flag_mesi_provvisori(df_prorata_b: pd.DataFrame) -> pd.DataFrame:
    """Per ogni (Mese, Distretto) del Metodo B, segnala se una PARTE del
    volume di quel mese viene da una stima ancora "in attesa di conferma"
    (nessuna lettura reale l'ha ancora chiusa — vedi calcola_periodi_metodo_b,
    ORIGINE "stima provvisoria..."). Un mese/distretto cosi' segnalato puo'
    cambiare valore in un ricalcolo futuro, quando arrivera' la lettura reale
    mancante: utile per sapere quali numeri di un bilancio trimestrale sono
    ancora "provvisori" e quali "consolidati".
    """
    if df_prorata_b.empty:
        return pd.DataFrame(columns=["MESE", "DISTRETTO", "Contiene Stime Provvisorie"])
    valide = df_prorata_b[df_prorata_b["CATEGORIA_DISTRETTO"] == "valido"].copy()
    valide["_provvisorio"] = valide["ORIGINE"].str.startswith("stima provvisoria")
    flag = (
        valide.groupby(["MESE", "DISTRETTO"], as_index=False)["_provvisorio"]
        .any()
        .rename(columns={"_provvisorio": "Contiene Stime Provvisorie"})
    )
    flag["Contiene Stime Provvisorie"] = flag["Contiene Stime Provvisorie"].map({True: "Sì", False: "No"})
    return flag


def _ripartisci_su_mesi(data_fine: pd.Timestamp, giorni: int) -> list[tuple[pd.Period, int]]:
    """Dato il giorno finale di una lettura e il numero di giorni coperti,
    restituisce la lista (mese, n_giorni_in_quel_mese) che ripartisce il
    periodo (data_fine - giorni, data_fine] sui mesi di calendario.

    Stessa logica di sempre, ma con aritmetica su date/timedelta di Python
    invece di pd.Timestamp/pd.Period dentro il ciclo: chiamata migliaia di
    volte (una per periodo di consumo), il costo per chiamata degli
    oggetti pandas era il secondo grosso collo di botiglia del motore
    (dopo quello risolto in calcola_periodi_metodo_b). Il pd.Period viene
    costruito solo alla fine, una volta per mese trovato, non ad ogni
    iterazione del ciclo.
    """
    fine = data_fine.date()
    if giorni <= 0:
        return [(pd.Period(year=fine.year, month=fine.month, freq="M"), 1)]

    inizio = fine - timedelta(days=giorni)
    # Il periodo e' (inizio, fine]: il giorno inizio stesso appartiene
    # alla lettura PRECEDENTE, quindi si parte dal giorno dopo.
    cursore = inizio + timedelta(days=1)
    ripartizione: dict[tuple[int, int], int] = {}
    while cursore <= fine:
        anno, mese_num = cursore.year, cursore.month
        ultimo_giorno_mese = date(anno, mese_num, monthrange(anno, mese_num)[1])
        fine_blocco = min(fine, ultimo_giorno_mese)
        giorni_in_mese = (fine_blocco - cursore).days + 1
        chiave = (anno, mese_num)
        ripartizione[chiave] = ripartizione.get(chiave, 0) + giorni_in_mese
        cursore = fine_blocco + timedelta(days=1)

    return [(pd.Period(year=anno, month=mese_num, freq="M"), g) for (anno, mese_num), g in ripartizione.items()]


def aggrega(df_prorata: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggrega le quote mensili per distretto+mese (solo righe valide) e,
    separatamente, anche per classe d'uso.
    """
    valide = df_prorata[df_prorata["CATEGORIA_DISTRETTO"] == "valido"]

    per_distretto_mese = (
        valide.groupby(["MESE", "DISTRETTO"], as_index=False)["VOLUME_MESE_M3"]
        .sum()
        .rename(columns={"MESE": "Mese", "DISTRETTO": "Codice Distretto", "VOLUME_MESE_M3": "Volume Fatturato (m3)"})
        .sort_values(["Mese", "Codice Distretto"])
    )
    per_distretto_mese["Volume Fatturato (m3)"] = per_distretto_mese["Volume Fatturato (m3)"].round(2)
    per_distretto_mese["Source"] = "Neta H2O"
    per_distretto_mese["Note"] = ""

    per_distretto_mese_classe = (
        valide.groupby(["MESE", "DISTRETTO", "PRODOTTO_CODICE"], as_index=False)["VOLUME_MESE_M3"]
        .sum()
        .rename(columns={
            "MESE": "Mese", "DISTRETTO": "Codice Distretto",
            "PRODOTTO_CODICE": "Classe d'uso", "VOLUME_MESE_M3": "Volume (m3)",
        })
        .sort_values(["Mese", "Codice Distretto", "Classe d'uso"])
    )
    per_distretto_mese_classe["Volume (m3)"] = per_distretto_mese_classe["Volume (m3)"].round(2)

    return per_distretto_mese, per_distretto_mese_classe


def aggrega_comune(df_prorata: pd.DataFrame) -> pd.DataFrame:
    """Aggrega i volumi mensili per COMUNE INTERO (non per distretto),
    includendo TUTTE le categorie: distretti validi, case sparse (NO
    DISTRETTO) e utenze con distretto anomalo/mancante. Utile come
    controllo complessivo del consumo del comune, indipendente dalla
    classificazione per distretto.

    Restituisce una riga per (mese, comune) con una colonna per ciascuna
    categoria piu' il totale, cosi' e' chiaro subito quanto del consumo
    del comune finisce davvero nei distretti e quanto no.
    """
    per_categoria = (
        df_prorata.groupby(["MESE", "LOCALITA", "CATEGORIA_DISTRETTO"], as_index=False)["VOLUME_MESE_M3"]
        .sum()
    )
    pivot = per_categoria.pivot_table(
        index=["MESE", "LOCALITA"], columns="CATEGORIA_DISTRETTO", values="VOLUME_MESE_M3", fill_value=0
    ).reset_index()
    pivot.columns.name = None

    for cat in ["valido", "case_sparse", "anomalia"]:
        if cat not in pivot.columns:
            pivot[cat] = 0.0

    pivot["Totale Comune (m3)"] = pivot["valido"] + pivot["case_sparse"] + pivot["anomalia"]
    pivot = pivot.rename(columns={
        "MESE": "Mese", "LOCALITA": "Comune",
        "valido": "Volume nei Distretti (m3)",
        "case_sparse": "Volume Case Sparse (m3)",
        "anomalia": "Volume Distretto Anomalo/Mancante (m3)",
    })
    colonne = [
        "Mese", "Comune", "Volume nei Distretti (m3)", "Volume Case Sparse (m3)",
        "Volume Distretto Anomalo/Mancante (m3)", "Totale Comune (m3)",
    ]
    for c in colonne[2:]:
        pivot[c] = pivot[c].round(2)
    return pivot[colonne].sort_values(["Mese", "Comune"])


def aggrega_comune_trimestre(volumi_comune_mese_affidabile: pd.DataFrame) -> pd.DataFrame:
    """Come calcola_riepilogo_trimestrale, ma per il totale comune (vedi
    aggrega_comune): somma per trimestre solare i soli mesi 'affidabili'
    (dentro la finestra utile) e segnala se il trimestre e' completo.
    """
    colonne_valore = [
        "Volume nei Distretti (m3)", "Volume Case Sparse (m3)",
        "Volume Distretto Anomalo/Mancante (m3)", "Totale Comune (m3)",
    ]
    colonne_output = ["Trimestre", "Comune"] + colonne_valore + ["Mesi Inclusi", "Completo"]
    if volumi_comune_mese_affidabile.empty:
        return pd.DataFrame(columns=colonne_output)

    df = volumi_comune_mese_affidabile.copy()
    df["Trimestre"] = df["Mese"].dt.year.astype(str) + "-T" + df["Mese"].dt.quarter.astype(str)

    riepilogo = (
        df.groupby(["Trimestre", "Comune"])
        .agg(
            **{c: (c, "sum") for c in colonne_valore},
            Mesi_Inclusi=("Mese", lambda s: ", ".join(str(m) for m in sorted(s.unique()))),
            N_Mesi=("Mese", "nunique"),
        )
        .reset_index()
    )
    for c in colonne_valore:
        riepilogo[c] = riepilogo[c].round(2)
    riepilogo["Completo"] = riepilogo["N_Mesi"].apply(lambda n: "Sì" if n == 3 else "No (parziale)")
    riepilogo = riepilogo.rename(columns={"Mesi_Inclusi": "Mesi Inclusi"}).drop(columns="N_Mesi")
    return riepilogo[colonne_output].sort_values(["Trimestre", "Comune"])


def dividi_per_finestra(
    per_distretto_mese: pd.DataFrame, mese_min: pd.Period, mese_max: pd.Period
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separa i mesi 'dentro' la finestra utile (i mesi in cui il file
    contiene davvero delle letture, cioe' tra il mese della prima e il
    mese dell'ultima DATA_LETTURA) da quelli 'fuori'.

    PERCHE' SERVE: alcune letture coprono periodi molto lunghi (contatori
    non letti per mesi/anni) e il loro consumo, ripartito sui giorni,
    finisce per toccare anche mesi molto precedenti alla finestra della
    estrazione. Quei mesi passati sono INCOMPLETI (mancano le altre
    letture di quei mesi, che si trovano in estrazioni precedenti non
    ancora caricate) quindi vanno tenuti separati e non usati come se
    fossero un dato affidabile.
    """
    dentro_mask = (per_distretto_mese["Mese"] >= mese_min) & (per_distretto_mese["Mese"] <= mese_max)
    dentro = per_distretto_mese[dentro_mask].copy()
    fuori = per_distretto_mese[~dentro_mask].copy()
    if not fuori.empty:
        fuori["Note"] = (
            f"Mese fuori dalla finestra di letture di questo file ({mese_min}→{mese_max}); "
            "dato parziale, incompleto senza le estrazioni dei periodi precedenti"
        )
    return dentro, fuori


def calcola_riepilogo_trimestrale(per_distretto_mese: pd.DataFrame) -> pd.DataFrame:
    """Somma i volumi mensili (solo quelli nella finestra affidabile) per
    trimestre solare (T1=gen-mar, T2=apr-giu, T3=lug-set, T4=ott-dic).

    Segnala anche se un trimestre e' 'completo' (tutti e 3 i mesi presenti
    nella finestra affidabile) o 'parziale' (ne mancano uno o due, tipico
    del trimestre a cavallo del bordo dei file caricati) — un trimestre
    parziale non va confuso con il totale vero del trimestre.
    """
    if per_distretto_mese.empty:
        return pd.DataFrame(columns=[
            "Trimestre", "Codice Distretto", "Volume Fatturato (m3)", "Mesi Inclusi", "Completo"
        ])

    df = per_distretto_mese.copy()
    df["Trimestre"] = df["Mese"].dt.year.astype(str) + "-T" + df["Mese"].dt.quarter.astype(str)

    riepilogo = (
        df.groupby(["Trimestre", "Codice Distretto"])
        .agg(
            **{"Volume Fatturato (m3)": ("Volume Fatturato (m3)", "sum")},
            Mesi_Inclusi=("Mese", lambda s: ", ".join(str(m) for m in sorted(s.unique()))),
            N_Mesi=("Mese", "nunique"),
        )
        .reset_index()
    )
    riepilogo["Volume Fatturato (m3)"] = riepilogo["Volume Fatturato (m3)"].round(2)
    riepilogo["Completo"] = riepilogo["N_Mesi"].apply(lambda n: "Sì" if n == 3 else "No (parziale)")
    riepilogo = riepilogo.rename(columns={"Mesi_Inclusi": "Mesi Inclusi"}).drop(columns="N_Mesi")

    return riepilogo.sort_values(["Trimestre", "Codice Distretto"])


def _ultima_anagrafica_utenze(df_tutti: pd.DataFrame) -> pd.DataFrame:
    """Per ogni utenza, l'anagrafica piu' recente nota (dall'ultima
    lettura in ordine di tempo su tutto l'archivio): distretto,
    categoria distretto, classe d'uso, stato servizio, date fornitura.

    E' una FOTO ATTUALE (l'ultimo stato conosciuto), non uno storico nel
    tempo — usata da piu' riepiloghi anagrafici (utenze attive per
    trimestre, conteggio utenze per classe d'uso/stato).
    """
    colonne = [
        "CODICE_SERVIZIO", "DISTRETTO", "CATEGORIA_DISTRETTO", "PRODOTTO_CODICE",
        "STATO_SERVIZIO", "DATA_INIZIO_FORNITURA", "DATA_FINE_FORNITURA",
    ]
    return df_tutti.sort_values("DATA_LETTURA").groupby("CODICE_SERVIZIO", as_index=False).last()[colonne]


def calcola_utenze_attive_trimestre(df_tutti: pd.DataFrame, trimestri: list[str]) -> pd.DataFrame:
    """Conta, per ogni distretto valido e ogni trimestre solare gia'
    presente nel Riepilogo_Trimestrale, quante utenze avevano una
    fornitura attiva in QUALCHE momento di quel trimestre.

    Si basa sulle date di inizio/fine fornitura (non sulle letture, che
    potrebbero non cadere proprio in quel trimestre): un'utenza conta
    come attiva nel trimestre se DATA_INIZIO_FORNITURA e' iniziata prima
    della fine del trimestre, e DATA_FINE_FORNITURA (se presente) non e'
    terminata prima dell'inizio del trimestre.

    Per ogni utenza si usano l'anagrafica e le date piu' recenti note (in
    base all'ultima lettura in ordine di tempo, su tutto l'archivio),
    cosi' se un contratto e' stato chiuso nel frattempo la chiusura si
    riflette anche sui trimestri passati.
    """
    colonne_output = ["Trimestre", "Codice Distretto", "Utenze Attive"]
    if not trimestri:
        return pd.DataFrame(columns=colonne_output)

    anagrafica = _ultima_anagrafica_utenze(df_tutti)
    valide = anagrafica[anagrafica["CATEGORIA_DISTRETTO"] == "valido"]

    righe = []
    for trimestre in trimestri:
        anno, num = trimestre.split("-T")
        num = int(num)
        mese_inizio = pd.Period(f"{anno}-{(num - 1) * 3 + 1:02d}", freq="M")
        mese_fine = pd.Period(f"{anno}-{num * 3:02d}", freq="M")
        inizio_trim = mese_inizio.start_time
        fine_trim = mese_fine.end_time

        attive = valide[
            (valide["DATA_INIZIO_FORNITURA"] <= fine_trim)
            & (valide["DATA_FINE_FORNITURA"].isna() | (valide["DATA_FINE_FORNITURA"] >= inizio_trim))
        ]
        conteggio = attive.groupby("DISTRETTO").size().rename("Utenze Attive").reset_index()
        conteggio["Trimestre"] = trimestre
        righe.append(conteggio)

    risultato = pd.concat(righe, ignore_index=True)
    return risultato.rename(columns={"DISTRETTO": "Codice Distretto"})[colonne_output]


def _pivot_stato(gruppo: pd.DataFrame, colonne_indice: list[str]) -> pd.DataFrame:
    """Trasforma un elenco (indice..., STATO_SERVIZIO) in una tabella
    larga con una colonna per ogni stato servizio trovato nei dati, piu'
    'Totale Utenze'. Le colonne di stato sono ordinate cosi' che
    ATT-ATTIVATA venga sempre prima (e' quella che interessa di piu'),
    le altre in ordine alfabetico.
    """
    conteggio = gruppo.groupby(colonne_indice + ["STATO_SERVIZIO"]).size().reset_index(name="N")
    pivot = conteggio.pivot_table(
        index=colonne_indice, columns="STATO_SERVIZIO", values="N", fill_value=0
    ).reset_index()
    pivot.columns.name = None

    stato_cols = [c for c in pivot.columns if c not in colonne_indice]
    stato_cols_ordinate = sorted(stato_cols, key=lambda s: (s != "ATT-ATTIVATA", s))
    for c in stato_cols_ordinate:
        pivot[c] = pivot[c].astype(int)
    pivot["Totale Utenze"] = pivot[stato_cols].sum(axis=1).astype(int)
    return pivot[colonne_indice + stato_cols_ordinate + ["Totale Utenze"]]


def conteggio_utenze_per_distretto(df_tutti: pd.DataFrame) -> pd.DataFrame:
    """Foto attuale: per ogni distretto valido, quante utenze per ogni
    stato servizio (ATT-ATTIVATA, CFAT-CESSATA, SOSP-SOSPESO, ecc.),
    indipendentemente dalla classe d'uso. Vista d'insieme rapida.
    """
    anagrafica = _ultima_anagrafica_utenze(df_tutti)
    valide = anagrafica[anagrafica["CATEGORIA_DISTRETTO"] == "valido"].copy()
    if valide.empty:
        return pd.DataFrame(columns=["Codice Distretto", "Totale Utenze"])

    risultato = _pivot_stato(valide, ["DISTRETTO"])
    return risultato.rename(columns={"DISTRETTO": "Codice Distretto"}).sort_values("Codice Distretto")


def conteggio_utenze_per_distretto_classe(df_tutti: pd.DataFrame) -> pd.DataFrame:
    """Foto attuale: per ogni distretto valido E ogni classe d'uso
    (PRODOTTO_CODICE), quante utenze per ogni stato servizio. Dettaglio
    completo, una riga per (distretto, classe d'uso).
    """
    anagrafica = _ultima_anagrafica_utenze(df_tutti)
    valide = anagrafica[anagrafica["CATEGORIA_DISTRETTO"] == "valido"].copy()
    if valide.empty:
        return pd.DataFrame(columns=["Codice Distretto", "Classe d'uso", "Totale Utenze"])

    risultato = _pivot_stato(valide, ["DISTRETTO", "PRODOTTO_CODICE"])
    return risultato.rename(
        columns={"DISTRETTO": "Codice Distretto", "PRODOTTO_CODICE": "Classe d'uso"}
    ).sort_values(["Codice Distretto", "Classe d'uso"])


# ---------------------------------------------------------------------------
# STATISTICHE PER COMUNE/DISTRETTO (proposte a Daniele il 16/09/2026, su sua
# richiesta di un "tab statistiche"): variazioni temporali, dotazione idrica
# per utenza, ripartizione per classe d'uso, coefficiente di punta
# stagionale. Tutte calcolate sul Metodo B, sui soli mesi/trimestri della
# finestra affidabile (vedi 2.3) — mai su dati fuori periodo.
# ---------------------------------------------------------------------------

def _giorni_nel_trimestre(trimestre: str) -> int:
    """Numero di giorni di calendario coperti da un trimestre solare
    (es. "2026-T1" -> 90 o 91 a seconda dell'anno bisestile).
    """
    anno_s, num_s = trimestre.split("-T")
    anno, num = int(anno_s), int(num_s)
    mese_inizio = 3 * (num - 1) + 1
    inizio = pd.Timestamp(year=anno, month=mese_inizio, day=1)
    fine = (inizio + pd.DateOffset(months=3)) - pd.Timedelta(days=1)
    return (fine - inizio).days + 1


def calcola_statistiche_trimestrali(volumi_distretto_trimestre: pd.DataFrame) -> pd.DataFrame:
    """Per ogni (Trimestre, Distretto) del Riepilogo_Trimestrale: dotazione
    idrica media (m3/utenza/giorno, usando "Utenze Attive" gia' calcolato),
    variazione % rispetto al trimestre precedente dello stesso distretto, e
    variazione % rispetto allo stesso trimestre dell'anno precedente.

    Le variazioni restano vuote (None) quando manca il dato di confronto
    (es. il primissimo trimestre in archivio, o un trimestre precedente non
    ancora caricato) — non si inventa mai un confronto con un buco in mezzo.
    """
    colonne = [
        "Trimestre", "Codice Distretto", "Volume Fatturato (m3)", "Utenze Attive",
        "Dotazione Idrica (m3/utenza/giorno)", "Variazione % vs Trimestre Precedente",
        "Variazione % vs Stesso Trimestre Anno Precedente", "Completo",
    ]
    if volumi_distretto_trimestre.empty:
        return pd.DataFrame(columns=colonne)

    df = volumi_distretto_trimestre.copy()
    df["_anno"] = df["Trimestre"].str[:4].astype(int)
    df["_num_trim"] = df["Trimestre"].str[-1].astype(int)
    df["_ord"] = df["_anno"] * 4 + df["_num_trim"]
    df["_giorni"] = df["Trimestre"].apply(_giorni_nel_trimestre)
    df["Dotazione Idrica (m3/utenza/giorno)"] = df.apply(
        lambda r: round(r["Volume Fatturato (m3)"] / r["Utenze Attive"] / r["_giorni"], 3)
        if r["Utenze Attive"] else None,
        axis=1,
    )

    df = df.sort_values(["Codice Distretto", "_ord"]).reset_index(drop=True)
    prec = df.groupby("Codice Distretto").shift(1)

    var_prec = []
    for v_att, v_prec, ord_att, ord_prec in zip(
        df["Volume Fatturato (m3)"], prec["Volume Fatturato (m3)"], df["_ord"], prec["_ord"]
    ):
        if pd.isna(v_prec) or pd.isna(ord_prec) or ord_prec != ord_att - 1 or v_prec == 0:
            var_prec.append(None)
        else:
            var_prec.append(round((v_att - v_prec) / v_prec * 100, 1))
    df["Variazione % vs Trimestre Precedente"] = var_prec

    riferimento = df.set_index(["Codice Distretto", "_anno", "_num_trim"])["Volume Fatturato (m3)"]
    var_anno = []
    for distretto, anno, num_trim, vol in zip(
        df["Codice Distretto"], df["_anno"], df["_num_trim"], df["Volume Fatturato (m3)"]
    ):
        chiave = (distretto, anno - 1, num_trim)
        if chiave in riferimento.index:
            v_prec = riferimento.loc[chiave]
            if isinstance(v_prec, pd.Series):
                v_prec = v_prec.iloc[0]
            var_anno.append(round((vol - v_prec) / v_prec * 100, 1) if v_prec else None)
        else:
            var_anno.append(None)
    df["Variazione % vs Stesso Trimestre Anno Precedente"] = var_anno

    return df[colonne].sort_values(["Trimestre", "Codice Distretto"])


def calcola_statistiche_classe_uso(
    volumi_distretto_mese_classe: pd.DataFrame,
    utenze_per_distretto_classe: pd.DataFrame,
    mese_min: pd.Period,
    mese_max: pd.Period,
) -> pd.DataFrame:
    """Per ogni (Distretto, Classe d'uso): volume totale sull'intera
    finestra affidabile, % sul totale del distretto, e consumo medio annuo
    per utenza ATTIVA (annualizzato in proporzione a quanti giorni copre
    davvero la finestra affidabile disponibile — con pochi mesi in archivio
    il numero e' una proiezione, non una misura su un anno vero, vedi nota
    nelle specifiche).

    Le utenze usate come denominatore sono quelle con stato ATT-ATTIVATA
    OGGI (foto attuale) — non quelle attive nel periodo passato — e' una
    approssimazione dichiarata, coerente con quella gia' usata per il
    conteggio utenze del punto 2.9.
    """
    colonne = [
        "Codice Distretto", "Classe d'uso", "Volume Periodo Affidabile (m3)",
        "% sul Totale del Distretto", "Utenze Attive Oggi", "Consumo Medio (m3/utenza/anno)",
    ]
    if volumi_distretto_mese_classe.empty:
        return pd.DataFrame(columns=colonne)

    tot = (
        volumi_distretto_mese_classe.groupby(["Codice Distretto", "Classe d'uso"], as_index=False)["Volume (m3)"]
        .sum()
        .rename(columns={"Volume (m3)": "Volume Periodo Affidabile (m3)"})
    )
    tot_distretto = tot.groupby("Codice Distretto")["Volume Periodo Affidabile (m3)"].transform("sum")
    tot["% sul Totale del Distretto"] = (tot["Volume Periodo Affidabile (m3)"] / tot_distretto * 100).round(1)

    colonna_utenze_attive = "ATT-ATTIVATA" if "ATT-ATTIVATA" in utenze_per_distretto_classe.columns else "Totale Utenze"
    utenze = utenze_per_distretto_classe[["Codice Distretto", "Classe d'uso", colonna_utenze_attive]].rename(
        columns={colonna_utenze_attive: "Utenze Attive Oggi"}
    )
    tot = tot.merge(utenze, on=["Codice Distretto", "Classe d'uso"], how="left")

    if mese_min is not None and mese_max is not None:
        giorni_finestra = (mese_max.to_timestamp(how="end") - mese_min.to_timestamp(how="start")).days + 1
        fattore_annuo = 365.25 / giorni_finestra if giorni_finestra else None
    else:
        fattore_annuo = None

    tot["Consumo Medio (m3/utenza/anno)"] = tot.apply(
        lambda r: round(r["Volume Periodo Affidabile (m3)"] * fattore_annuo / r["Utenze Attive Oggi"], 1)
        if fattore_annuo and r["Utenze Attive Oggi"] else None,
        axis=1,
    )
    tot["Volume Periodo Affidabile (m3)"] = tot["Volume Periodo Affidabile (m3)"].round(2)

    return tot[colonne].sort_values(["Codice Distretto", "% sul Totale del Distretto"], ascending=[True, False])


def calcola_coefficiente_punta(volumi_distretto_mese: pd.DataFrame) -> pd.DataFrame:
    """Per ogni distretto: il coefficiente di punta stagionale, cioe' il
    rapporto tra la portata media del mese piu' "carico" e la portata media
    di tutti i mesi disponibili nella finestra affidabile — un indicatore
    tipico per dimensionare reti/impianti (quanto il picco supera la media).

    Attenzione: e' calcolato sulla portata MEDIA GIORNALIERA di ciascun mese
    (volume del mese / giorni del mese), non sul volume grezzo, altrimenti
    un mese di 31 giorni risulterebbe sempre "piu' alto" di uno di 28 solo
    per conteggio dei giorni, non per consumo vero. Con meno di 12 mesi in
    archivio (caso attuale) la "media" e' quella dei mesi disponibili, non
    di un anno intero: il numero resta indicativo finche' l'archivio non
    copre un anno solare completo per quel distretto.
    """
    colonne = [
        "Codice Distretto", "Mese di Punta", "Portata Mese di Punta (m3/giorno)",
        "Portata Media Periodo (m3/giorno)", "Coefficiente di Punta", "N. Mesi nel Calcolo",
    ]
    if volumi_distretto_mese.empty:
        return pd.DataFrame(columns=colonne)

    df = volumi_distretto_mese.copy()
    df["_giorni_mese"] = df["Mese"].apply(lambda m: m.to_timestamp(how="end").day)
    df["_portata"] = df["Volume Fatturato (m3)"] / df["_giorni_mese"]

    righe = []
    for distretto, gruppo in df.groupby("Codice Distretto"):
        media = gruppo["_portata"].mean()
        idx_punta = gruppo["_portata"].idxmax()
        righe.append({
            "Codice Distretto": distretto,
            "Mese di Punta": str(gruppo.loc[idx_punta, "Mese"]),
            "Portata Mese di Punta (m3/giorno)": round(gruppo.loc[idx_punta, "_portata"], 2),
            "Portata Media Periodo (m3/giorno)": round(media, 2),
            "Coefficiente di Punta": round(gruppo.loc[idx_punta, "_portata"] / media, 2) if media else None,
            "N. Mesi nel Calcolo": len(gruppo),
        })
    return pd.DataFrame(righe)[colonne].sort_values("Codice Distretto")


def costruisci_segnalazioni(df: pd.DataFrame) -> pd.DataFrame:
    """Elenco delle utenze con distretto anomalo (possibile errore
    anagrafico), una riga per utenza con il dettaglio necessario alla
    verifica manuale.
    """
    anomale = df[df["CATEGORIA_DISTRETTO"] == "anomalia"]
    if anomale.empty:
        return pd.DataFrame(columns=[
            "Codice Servizio", "Comune Estrazione", "Indirizzo", "Distretto Riportato",
            "Motivo", "Stato Servizio", "Consumo Totale nel File (m3)", "File Origine",
        ])

    riepilogo = (
        anomale.groupby("CODICE_SERVIZIO")
        .agg(
            Comune_Estrazione=("LOCALITA", "first"),
            Indirizzo=("INDIRIZZO_UBICAZIONE", "first"),
            Distretto_Riportato=("DISTRETTO", "first"),
            Motivo=("MOTIVO_SEGNALAZIONE", "first"),
            Stato_Servizio=("STATO_SERVIZIO", "first"),
            Consumo_Totale=("CONSUMO", "sum"),
            File_Origine=("FILE_ORIGINE", "first"),
        )
        .reset_index()
        .rename(columns={
            "CODICE_SERVIZIO": "Codice Servizio",
            "Comune_Estrazione": "Comune Estrazione",
            "Distretto_Riportato": "Distretto Riportato",
            "Stato_Servizio": "Stato Servizio",
            "Consumo_Totale": "Consumo Totale nel File (m3)",
            "File_Origine": "File Origine",
        })
    )
    return riepilogo.sort_values(["Comune Estrazione", "Distretto Riportato"])


STATI_CHIUSURA_ATTESI = ("CESSATA", "CFAT", "SOSP", "MOROS")


def trova_utenze_scomparse(file_in_ordine: list[pd.DataFrame]) -> pd.DataFrame:
    """Quando si caricano piu' file in sequenza cronologica, individua le
    utenze presenti in un file ma assenti in TUTTI i file successivi piu'
    recenti, e verifica se lo stato servizio dell'ultima lettura nota
    giustifica la sparizione (contratto cessato/sospeso) o se e' un caso
    da controllare (utenza ancora attiva che semplicemente non compare
    piu').
    """
    if len(file_in_ordine) < 2:
        return pd.DataFrame(columns=[
            "Codice Servizio", "Indirizzo", "Ultimo Stato Servizio", "Ultima Data Lettura",
            "Ultimo File in cui Compare", "Da Verificare",
        ])

    ultimo_file = file_in_ordine[-1]
    utenze_ultimo_file = set(ultimo_file["CODICE_SERVIZIO"].unique())

    righe = []
    utenze_gia_viste = set()
    for df in file_in_ordine[:-1]:
        nome_file = df["FILE_ORIGINE"].iloc[0]
        for _, riga in (
            _ordina_priorita_stessa_data(df).groupby("CODICE_SERVIZIO").tail(1).iterrows()
        ):
            uid = riga["CODICE_SERVIZIO"]
            if uid in utenze_ultimo_file or uid in utenze_gia_viste:
                continue
            utenze_gia_viste.add(uid)
            stato = str(riga["STATO_SERVIZIO"])
            atteso = any(chiave in stato.upper() for chiave in STATI_CHIUSURA_ATTESI)
            righe.append({
                "Codice Servizio": uid,
                "Indirizzo": riga["INDIRIZZO_UBICAZIONE"],
                "Ultimo Stato Servizio": stato,
                "Ultima Data Lettura": riga["DATA_LETTURA"],
                "Ultimo File in cui Compare": nome_file,
                "Da Verificare": "No (contratto chiuso)" if atteso else "Sì (era ancora attiva)",
            })

    return pd.DataFrame(righe).sort_values("Da Verificare", ascending=False) if righe else pd.DataFrame(columns=[
        "Codice Servizio", "Indirizzo", "Ultimo Stato Servizio", "Ultima Data Lettura",
        "Ultimo File in cui Compare", "Da Verificare",
    ])


def trova_cessate_con_stima_finale(df_tutti: pd.DataFrame) -> pd.DataFrame:
    """Individua le utenze con contratto CHIUSO (STATO_SERVIZIO tra quelli
    di STATI_CHIUSURA_ATTESI) la cui ULTIMA lettura nota NON è una lettura
    reale (vedi TIPI_LETTURA_REALE piu' sopra, sezione Metodo B).

    PERCHE' E' UN'ANOMALIA (confermato da Daniele): quando un'utenza cessa
    e' obbligatorio effettuare una lettura reale di chiusura. Se l'ultima
    lettura nota e' invece una stima, il contratto potrebbe essere stato
    chiuso senza la lettura di chiusura dovuta — da verificare a mano.
    """
    colonne_output = [
        "Codice Servizio", "Indirizzo", "Distretto", "Stato Servizio",
        "Ultima Lettura (tipo)", "Ultima Data Lettura", "File Origine",
    ]
    if df_tutti.empty:
        return pd.DataFrame(columns=colonne_output)

    ultima = (
        _ordina_priorita_stessa_data(df_tutti)
        .groupby("CODICE_SERVIZIO", as_index=False)
        .last()
    )
    stato_upper = ultima["STATO_SERVIZIO"].astype(str).str.upper()
    chiusa = stato_upper.apply(lambda s: any(k in s for k in STATI_CHIUSURA_ATTESI))
    non_reale = ~ultima["TIPO_LETTURA"].isin(TIPI_LETTURA_REALE)

    anomale = ultima[chiusa & non_reale].rename(columns={
        "CODICE_SERVIZIO": "Codice Servizio", "INDIRIZZO_UBICAZIONE": "Indirizzo",
        "DISTRETTO": "Distretto", "STATO_SERVIZIO": "Stato Servizio",
        "TIPO_LETTURA": "Ultima Lettura (tipo)", "DATA_LETTURA": "Ultima Data Lettura",
        "FILE_ORIGINE": "File Origine",
    })
    if anomale.empty:
        return pd.DataFrame(columns=colonne_output)
    return anomale[colonne_output].sort_values("Ultima Data Lettura", ascending=False)


CHIAVE_ARCHIVIO = ["CODICE_SERVIZIO", "DATA_LETTURA", "TIPO_LETTURA"]


def carica_archivio(percorso_archivio: str | Path) -> pd.DataFrame:
    """Carica l'archivio storico delle letture da disco (un CSV che
    accumula tutte le letture mai caricate). Se l'archivio non esiste
    ancora, restituisce una tabella vuota con le colonne giuste.
    """
    percorso_archivio = Path(percorso_archivio)
    if not percorso_archivio.exists():
        return pd.DataFrame(columns=COLONNE_ATTESE + ["FILE_ORIGINE"])
    df = pd.read_csv(percorso_archivio)
    for col in ["DATA_LETTURA", "DATA_FATTURAZ_LETTURA", "DATA_INIZIO_FORNITURA", "DATA_FINE_FORNITURA", "DATA_DECORR_BC"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def aggiorna_archivio(nuovi_file: list[str | Path], percorso_archivio: str | Path) -> tuple[pd.DataFrame, dict]:
    """Aggiunge una o più nuove estrazioni all'archivio storico delle
    letture, evitando di duplicare righe già presenti (stessa utenza,
    stessa data di lettura, stesso tipo di lettura — così ricaricare per
    sbaglio lo stesso file due volte non crea doppioni). Salva l'archivio
    aggiornato su disco (CSV, apribile anche a mano in Excel per
    controllo) e restituisce il contenuto completo, pronto per il calcolo.

    A parità di chiave (utenza+data+tipo) viene tenuta la riga già
    presente in archivio: se Neta H2O dovesse mai correggere una lettura
    vecchia con lo stesso tipo/data, quella correzione NON verrebbe
    recepita automaticamente — andrebbe gestita a mano.
    """
    archivio = carica_archivio(percorso_archivio)
    righe_prima = len(archivio)

    nuovi_df = [carica_estrazione(p) for p in nuovi_file]
    aggiunte = pd.concat(nuovi_df, ignore_index=True) if nuovi_df else pd.DataFrame(columns=archivio.columns)

    # Concatenare con un archivio vuoto (dtype object su tutte le colonne,
    # perché senza righe pandas non puo' dedurre i tipi) trasformerebbe
    # anche le colonne buone in object: se l'archivio è vuoto si parte
    # direttamente dalle righe nuove, senza concatenare nulla.
    combinato = aggiunte.copy() if archivio.empty else pd.concat([archivio, aggiunte], ignore_index=True)
    prima_dedup = len(combinato)
    combinato = combinato.sort_values("DATA_LETTURA").drop_duplicates(subset=CHIAVE_ARCHIVIO, keep="first")
    duplicate_scartate = prima_dedup - len(combinato)

    percorso_archivio = Path(percorso_archivio)
    percorso_archivio.parent.mkdir(parents=True, exist_ok=True)
    combinato.to_csv(percorso_archivio, index=False)

    stats = {
        "righe_archivio_prima": righe_prima,
        "righe_lette_dai_nuovi_file": len(aggiunte),
        "righe_duplicate_scartate": duplicate_scartate,
        "righe_archivio_dopo": len(combinato),
        "righe_nuove_aggiunte_davvero": len(combinato) - righe_prima,
    }
    return combinato, stats


def elabora_file(paths: list[str | Path]) -> RisultatoElaborazione:
    """Elabora direttamente uno o più file di estrazione (senza passare
    dall'archivio storico). Utile per un test rapido su file singoli;
    per l'uso normale con più trimestri conviene invece passare da
    aggiorna_archivio + elabora_dataframe (vedi funzione main in fondo).
    """
    df_grezzo = pd.concat([carica_estrazione(p) for p in paths], ignore_index=True)
    return elabora_dataframe(df_grezzo)


def elabora_dataframe(df_grezzo: pd.DataFrame) -> RisultatoElaborazione:
    """Come elabora_file, ma parte da un DataFrame già caricato (es.
    l'archivio storico) invece che da percorsi di file su disco.
    """
    warning: list[str] = []
    riepilogo_righe = []
    grezzi = []
    distretti_visti_per_file = []  # indice allineato a riepilogo_righe/grezzi

    for nome_file, df in df_grezzo.groupby("FILE_ORIGINE", sort=False):
        df = classifica_distretto(df)
        grezzi.append(df)

        n_anomale = (df["CATEGORIA_DISTRETTO"] == "anomalia").sum()
        n_case_sparse = (df["CATEGORIA_DISTRETTO"] == "case_sparse").sum()
        comune_file = comune_dominante(df)
        riepilogo_righe.append({
            "File": nome_file,
            "Comune": comune_file,
            "Righe lette": len(df),
            "Utenze uniche": df["CODICE_SERVIZIO"].nunique(),
            "Righe case sparse (escluse, non errore)": int(n_case_sparse),
            "Righe con distretto anomalo (da verificare)": int(n_anomale),
            "Periodo letture": f"{df['DATA_LETTURA'].min().date()} → {df['DATA_LETTURA'].max().date()}",
        })
        distretti_visti_per_file.append(set(df.loc[df["CATEGORIA_DISTRETTO"] == "valido", "DISTRETTO"].unique()))

    # File in ordine cronologico (per data di lettura piu' vecchia), usato
    # per individuare le utenze che spariscono da un file all'altro.
    grezzi_ordinati = sorted(grezzi, key=lambda d: d["DATA_LETTURA"].min())
    utenze_scomparse = trova_utenze_scomparse(grezzi_ordinati)
    n_da_verificare = (utenze_scomparse["Da Verificare"] == "Sì (era ancora attiva)").sum() if not utenze_scomparse.empty else 0
    if n_da_verificare:
        warning.append(
            f"{n_da_verificare} utenze erano ancora ATTIVE nell'ultima lettura nota ma non "
            "compaiono nel file piu' recente caricato — non hanno un contratto chiuso che "
            "giustifichi la sparizione. Dettaglio nel foglio 'Utenze_Scomparse', da controllare."
        )

    df_tutti = pd.concat(grezzi, ignore_index=True)

    # Segnalazione (NON bloccante) — controllo di completezza per comune,
    # richiesto da Daniele: se un file caricato copre un comune per cui
    # in archivio conosciamo GIA' altri distretti (da altri file dello
    # stesso comune), ma quel distretto non compare in QUESTO file,
    # potrebbe voler dire che l'estrazione era parziale (mancava un
    # pezzo del comune). Non è un controllo definitivo (in un trimestre
    # normale è anche possibile che semplicemente non ci siano state
    # letture in un distretto), quindi resta solo un avviso da
    # verificare, non blocca mai il calcolo. In attesa dell'elenco
    # ufficiale comuni/distretti (vedi 6.1 delle specifiche) è l'unico
    # riferimento disponibile: lo storico stesso dell'archivio.
    distretti_noti_per_comune: dict[str, set] = {}
    for riga, distretti_file in zip(riepilogo_righe, distretti_visti_per_file):
        distretti_noti_per_comune.setdefault(riga["Comune"], set()).update(distretti_file)

    comuni_con_segnalazione = []
    for riga, distretti_file in zip(riepilogo_righe, distretti_visti_per_file):
        attesi = distretti_noti_per_comune.get(riga["Comune"], set())
        mancanti = sorted(attesi - distretti_file)
        riga["Distretti Noti Assenti in Questo File"] = ", ".join(mancanti) if mancanti else "Nessuno"
        if mancanti:
            comuni_con_segnalazione.append((riga["File"], riga["Comune"], mancanti))

    if comuni_con_segnalazione:
        dettaglio = "; ".join(f"{f} ({c}): {', '.join(m)}" for f, c, m in comuni_con_segnalazione)
        warning.append(
            "Possibile estrazione incompleta (segnalazione, NON blocca il calcolo): alcuni file "
            "non contengono letture per distretti che in archivio risultano appartenere allo "
            "stesso comune — verifica che l'estrazione Neta H2O coprisse tutto il comune. "
            f"Dettaglio: {dettaglio}. Vedi anche la colonna 'Distretti Noti Assenti in Questo "
            "File' nel foglio 'Riepilogo_File'."
        )

    # Finestra "utile": i mesi in cui questo set di file contiene davvero
    # delle letture (dalla piu' vecchia alla piu' recente DATA_LETTURA).
    mese_letture = df_tutti["DATA_LETTURA"].dt.to_period("M")
    mese_min, mese_max = mese_letture.min(), mese_letture.max()

    segnalazioni = costruisci_segnalazioni(df_tutti)

    # --- METODO B (differenza di letture, le reali vincono sulle
    # stimate) --- UNICO metodo usato dal 16/09/2026: confermato da
    # Daniele che e' questa la logica corretta di fatturazione (le
    # letture reali conguagliano le stime, non si sommano ad esse — vedi
    # 2.4/2.10). E' questo il volume che alimenta district_billed (foglio
    # Import_WMS).
    periodi_b, anomalie_metodo_b, riferimento_prodie = calcola_periodi_metodo_b(df_tutti)
    if not anomalie_metodo_b.empty:
        warning.append(
            f"Metodo B: {len(anomalie_metodo_b)} letture escluse dal calcolo perché "
            "sentinella (valore non valido) o perché la lettura risultava diminuita senza "
            "un cambio contatore marcato nel file. Dettaglio nel foglio 'Anomalie_MetodoB' "
            "— probabile problema di qualità dati da chiedere a Neta H2O."
        )
    df_prorata_b = prorata_mensile_metodo_b(periodi_b)
    volumi_distretto_mese_b_completo, volumi_distretto_mese_b_classe = aggrega(df_prorata_b)
    volumi_distretto_mese_b, volumi_fuori_periodo_b = dividi_per_finestra(
        volumi_distretto_mese_b_completo, mese_min, mese_max
    )
    volumi_distretto_trimestre_b = calcola_riepilogo_trimestrale(volumi_distretto_mese_b)

    volumi_comune_mese_b = aggrega_comune(df_prorata_b)
    volumi_comune_mese_b["Affidabile"] = volumi_comune_mese_b["Mese"].apply(
        lambda m: "Sì" if mese_min <= m <= mese_max else "No (fuori finestra)"
    )
    volumi_comune_trimestre_b = aggrega_comune_trimestre(
        volumi_comune_mese_b[volumi_comune_mese_b["Affidabile"] == "Sì"]
    )

    # Foto attuale delle utenze per distretto (e per distretto+classe
    # d'uso), con il dettaglio per stato servizio (richiesto da Daniele).
    utenze_per_distretto = conteggio_utenze_per_distretto(df_tutti)
    utenze_per_distretto_classe = conteggio_utenze_per_distretto_classe(df_tutti)

    # Aggiunge il conteggio delle utenze attive per distretto, trimestre
    # per trimestre (richiesto da Daniele per affiancarlo ai volumi).
    trimestri_presenti = (
        list(volumi_distretto_trimestre_b["Trimestre"].unique())
        if not volumi_distretto_trimestre_b.empty else []
    )
    utenze_attive_trimestre = calcola_utenze_attive_trimestre(df_tutti, trimestri_presenti)
    volumi_distretto_trimestre_b = volumi_distretto_trimestre_b.merge(
        utenze_attive_trimestre, on=["Trimestre", "Codice Distretto"], how="left"
    )
    volumi_distretto_trimestre_b["Utenze Attive"] = volumi_distretto_trimestre_b["Utenze Attive"].fillna(0).astype(int)

    # Segnala quali (trimestre, distretto) del Metodo B contengono ancora
    # stime "in attesa di conferma" (vedi flag_mesi_provvisori): un
    # trimestre cosi' segnalato puo' cambiare quando arrivera' la prossima
    # estrazione con la lettura reale mancante — utile per un bilancio
    # trimestrale dove i numeri vanno via via consolidati nel tempo.
    flag_mese = flag_mesi_provvisori(df_prorata_b)
    if not flag_mese.empty:
        flag_mese = flag_mese.rename(columns={"MESE": "Mese", "DISTRETTO": "Codice Distretto"})
        flag_mese["Trimestre"] = flag_mese["Mese"].dt.year.astype(str) + "-T" + flag_mese["Mese"].dt.quarter.astype(str)
        flag_trimestre = (
            flag_mese.groupby(["Trimestre", "Codice Distretto"], as_index=False)["Contiene Stime Provvisorie"]
            .apply(lambda s: "Sì" if (s == "Sì").any() else "No")
        )
    else:
        flag_trimestre = pd.DataFrame(columns=["Trimestre", "Codice Distretto", "Contiene Stime Provvisorie"])
    volumi_distretto_trimestre_b = volumi_distretto_trimestre_b.merge(
        flag_trimestre, on=["Trimestre", "Codice Distretto"], how="left"
    )
    volumi_distretto_trimestre_b["Contiene Stime Provvisorie"] = (
        volumi_distretto_trimestre_b["Contiene Stime Provvisorie"].fillna("No")
    )

    # Anomalia: utenze cessate la cui ultima lettura non e' reale
    # (obbligo di lettura reale alla cessazione, confermato da Daniele).
    cessate_con_stima_finale = trova_cessate_con_stima_finale(df_tutti)
    if not cessate_con_stima_finale.empty:
        warning.append(
            f"{len(cessate_con_stima_finale)} utenze con contratto chiuso hanno come ultima "
            "lettura nota una stima (non una lettura reale) — contro l'obbligo di lettura "
            "reale alla cessazione. Dettaglio nel foglio 'Cessate_Con_Stima_Finale'."
        )

    # Statistiche per comune/distretto (nuovo, su richiesta di Daniele il
    # 16/09/2026): dotazione idrica, variazioni % nel tempo, ripartizione
    # per classe d'uso, coefficiente di punta stagionale — vedi 2.14.
    statistiche_trimestrali = calcola_statistiche_trimestrali(volumi_distretto_trimestre_b)
    statistiche_classe_uso = calcola_statistiche_classe_uso(
        volumi_distretto_mese_b_classe, utenze_per_distretto_classe, mese_min, mese_max
    )
    coefficiente_punta = calcola_coefficiente_punta(volumi_distretto_mese_b)

    n_trimestri_parziali = (volumi_distretto_trimestre_b["Completo"] == "No (parziale)").sum()
    if n_trimestri_parziali:
        warning.append(
            f"{n_trimestri_parziali} righe del foglio 'Riepilogo_Trimestrale' sono trimestri "
            "PARZIALI (mancano uno o due mesi nella finestra affidabile) — tipicamente il "
            "trimestre a cavallo dell'inizio o della fine dei file caricati finora. Non "
            "trattarli come il totale vero del trimestre finche' non si carica il mese mancante."
        )

    if not volumi_fuori_periodo_b.empty:
        tot_fuori = volumi_fuori_periodo_b["Volume Fatturato (m3)"].sum()
        tot_tutto = volumi_distretto_mese_b_completo["Volume Fatturato (m3)"].sum()
        pct = (tot_fuori / tot_tutto * 100) if tot_tutto else 0
        warning.append(
            f"{tot_fuori:.0f} m3 ({pct:.1f}% del totale) ricadono in mesi precedenti a "
            f"{mese_min} (fuori dalla finestra di letture del/dei file caricati). "
            "Sono nel foglio 'Fuori_Periodo': non sono affidabili finche' non si caricano "
            "anche le estrazioni dei periodi precedenti."
        )

    return RisultatoElaborazione(
        volumi_distretto_mese=volumi_distretto_mese_b,
        volumi_distretto_trimestre=volumi_distretto_trimestre_b,
        volumi_fuori_periodo=volumi_fuori_periodo_b,
        volumi_distretto_mese_classe=volumi_distretto_mese_b_classe,
        segnalazioni=segnalazioni,
        utenze_scomparse=utenze_scomparse,
        volumi_comune_mese=volumi_comune_mese_b,
        volumi_comune_trimestre=volumi_comune_trimestre_b,
        utenze_per_distretto=utenze_per_distretto,
        utenze_per_distretto_classe=utenze_per_distretto_classe,
        statistiche_trimestrali=statistiche_trimestrali,
        statistiche_classe_uso=statistiche_classe_uso,
        coefficiente_punta=coefficiente_punta,
        anomalie_metodo_b=anomalie_metodo_b,
        riferimento_prodie=riferimento_prodie,
        cessate_con_stima_finale=cessate_con_stima_finale,
        riepilogo_file=pd.DataFrame(riepilogo_righe),
        warning=warning,
    )


def esporta_excel(risultato: RisultatoElaborazione, output_path: str | Path) -> Path:
    """Scrive il file Excel finale (solo Metodo B, vedi RisultatoElaborazione) con i fogli:
    - Riepilogo_File: un controllo rapido su cosa e' stato elaborato
    - Import_WMS: pronto per il caricamento in district_billed (solo mesi affidabili)
    - Riepilogo_Trimestrale: per trimestre solare + utenze attive + flag stime provvisorie
    - Fuori_Periodo: mesi parziali/incompleti, tenuti separati per trasparenza
    - Dettaglio_Classi: stesso dato di Import_WMS ma diviso anche per classe d'uso
    - Segnalazioni: utenze con distretto anomalo da verificare
    - Rif_ConsumoProDie: m3/giorno per ogni confronto tra letture del Metodo B
      (aggiunto 16/09/2026, vedi 2.11: riferimento a colpo d'occhio, non solo le anomalie)
    - Statistiche_Trimestrali, Statistiche_Classe_Uso, Coefficiente_Punta:
      dotazione idrica, variazioni % nel tempo, ripartizione per classe
      d'uso, coefficiente di punta stagionale (aggiunto 16/09/2026, vedi 2.14)
    """
    output_path = Path(output_path)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        risultato.riepilogo_file.to_excel(writer, sheet_name="Riepilogo_File", index=False)

        df_import = risultato.volumi_distretto_mese.copy()
        df_import["Mese"] = df_import["Mese"].astype(str)
        df_import.to_excel(writer, sheet_name="Import_WMS", index=False)

        risultato.volumi_distretto_trimestre.to_excel(writer, sheet_name="Riepilogo_Trimestrale", index=False)

        df_fuori = risultato.volumi_fuori_periodo.copy()
        df_fuori["Mese"] = df_fuori["Mese"].astype(str)
        df_fuori.to_excel(writer, sheet_name="Fuori_Periodo", index=False)

        df_classi = risultato.volumi_distretto_mese_classe.copy()
        df_classi["Mese"] = df_classi["Mese"].astype(str)
        df_classi.to_excel(writer, sheet_name="Dettaglio_Classi", index=False)

        risultato.segnalazioni.to_excel(writer, sheet_name="Segnalazioni", index=False)

        df_scomparse = risultato.utenze_scomparse.copy()
        if "Ultima Data Lettura" in df_scomparse.columns and not df_scomparse.empty:
            df_scomparse["Ultima Data Lettura"] = df_scomparse["Ultima Data Lettura"].astype(str)
        df_scomparse.to_excel(writer, sheet_name="Utenze_Scomparse", index=False)

        df_comune_mese = risultato.volumi_comune_mese.copy()
        df_comune_mese["Mese"] = df_comune_mese["Mese"].astype(str)
        df_comune_mese.to_excel(writer, sheet_name="Riepilogo_Comune", index=False)

        risultato.volumi_comune_trimestre.to_excel(writer, sheet_name="Riepilogo_Comune_Trimestre", index=False)

        risultato.utenze_per_distretto.to_excel(writer, sheet_name="Utenze_per_Distretto", index=False)
        risultato.utenze_per_distretto_classe.to_excel(writer, sheet_name="Utenze_Distretto_Classe", index=False)

        risultato.statistiche_trimestrali.to_excel(writer, sheet_name="Statistiche_Trimestrali", index=False)
        risultato.statistiche_classe_uso.to_excel(writer, sheet_name="Statistiche_Classe_Uso", index=False)
        risultato.coefficiente_punta.to_excel(writer, sheet_name="Coefficiente_Punta", index=False)

        df_anom_b = risultato.anomalie_metodo_b.copy()
        if "DATA_LETTURA" in df_anom_b.columns and not df_anom_b.empty:
            df_anom_b["DATA_LETTURA"] = df_anom_b["DATA_LETTURA"].astype(str)
        df_anom_b.to_excel(writer, sheet_name="Anomalie_MetodoB", index=False)

        df_rif = risultato.riferimento_prodie.copy()
        if "DATA_FINE" in df_rif.columns and not df_rif.empty:
            df_rif["DATA_FINE"] = df_rif["DATA_FINE"].astype(str)
        df_rif.to_excel(writer, sheet_name="Rif_ConsumoProDie", index=False)

        risultato.cessate_con_stima_finale.to_excel(writer, sheet_name="Cessate_Con_Stima_Finale", index=False)

    return output_path


if __name__ == "__main__":
    import sys
    files = sys.argv[1:]
    if not files:
        print("Uso: python motore_calcolo.py file1.xlsx [file2.xlsx ...]")
        sys.exit(1)

    percorso_archivio = "archivio/archivio_letture.csv"
    archivio_completo, stats = aggiorna_archivio(files, percorso_archivio)
    print(
        f"Archivio aggiornato ({percorso_archivio}): "
        f"{stats['righe_archivio_prima']} righe prima → {stats['righe_archivio_dopo']} righe dopo "
        f"({stats['righe_nuove_aggiunte_davvero']} nuove, {stats['righe_duplicate_scartate']} scartate perché già presenti)"
    )

    ris = elabora_dataframe(archivio_completo)
    out = esporta_excel(ris, "output/Volumi_Fatturati_per_Distretto.xlsx")
    print(f"File generato: {out}")
    for w in ris.warning:
        print(f"ATTENZIONE: {w}")
