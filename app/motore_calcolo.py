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
  ECCEZIONE: se il codice è nell'elenco ufficiale distretto→comune
  (project_docs/distretti_comuni.csv, mantenuto a mano da Daniele) come
  associabile al comune di questa estrazione — un distretto di un comune
  limitrofo che serve legittimamente anche punti di questo comune — è
  considerato valido, non un errore. Vedi classifica_distretto e
  carica_mappa_distretti_comuni.
"""

from __future__ import annotations

import itertools
import json
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# Elenco ufficiale distretto -> comune (con eventuali comuni limitrofi a
# cui il distretto e' legittimamente associabile), mantenuto a mano da
# Daniele — esportato/copiato da WMS SmartH2O quando serve (le due app
# restano deliberatamente separate, niente database condiviso: vedi
# classifica_distretto). Se un codice non compare qui, o compare con
# comune_ufficiale vuoto, si ricade sull'euristica del prefisso.
PERCORSO_MAPPA_DISTRETTI = Path("project_docs/distretti_comuni.csv")
# GeoJSON dei confini reali dei distretti (una Feature per distretto),
# usato dalla pagina Mappa al posto dei quadrati segnaposto quando esiste
# — vedi importa_confini_distretti.
PERCORSO_CONFINI_DISTRETTI = Path("project_docs/distretti_confini.geojson")

# Distretti soppressi, fusi in un altro (Daniele, 25/09/2026): in WMS
# SmartH2O non esistono piu' e non hanno un confine. Finche' Neta non
# corregge il CRM le estrazioni li riportano ancora: unisci_distretti_fusi
# li porta sul distretto nuovo prima di ogni calcolo, anche per i mesi
# passati, cosi' Import_WMS ha solo i codici attuali. L'archivio tiene il
# codice originale; il tab Prese li mostra come "Distretto soppresso" per il
# file da mandare a Neta.
DISTRETTI_FUSI = {
    "DVH02": "DVH05",
    "DVH03": "DVH05",
    "DCT04": "DCT13",
    "DCT07": "DCT13",  # DCT13 = DCT04 + DCT07 (Daniele, 25/09/2026)
}

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
    volumi_distretto_mese_origine: pd.DataFrame   # volume mensile per distretto, scomposto reale/provvisorio/interpolato (per i grafici)
    volumi_utenza_mese: pd.DataFrame              # volume mensile per singola utenza (per la classifica dei maggiori consumatori)
    utenze_corrette_da_nodma: pd.DataFrame        # utenze passate da NODMA/ND a un distretto vero, con il volume rimasto escluso per sempre
    stato_chiusura_mesi: pd.DataFrame             # per mese: lotto di fatturazione girato (Chiuso/Aperto/Non determinabile), vedi calcola_stato_chiusura_mesi
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


def carica_mappa_distretti_comuni(percorso: str | Path = PERCORSO_MAPPA_DISTRETTI) -> dict:
    """Legge PERCORSO_MAPPA_DISTRETTI: codice_distretto -> (comune_ufficiale,
    frozenset comuni_associabili). Un CSV mantenuto a mano da Daniele, non
    generato dall'app. Formato (intestazione richiesta):

        codice_distretto,comune_ufficiale,comuni_associabili
        DBLG01,BELGIOIOSO,
        DMR01,MORTARA,BELGIOIOSO

    comuni_associabili e' una lista separata da ';' di comuni limitrofi a
    cui QUEL distretto puo' legittimamente appartenere anche se compare in
    un'estrazione di un altro comune (es. una frazione alimentata dalla
    rete del comune vicino) — senza finire segnalato come anomalia. Una
    riga con comune_ufficiale vuoto (codice noto ma non ancora
    classificato) viene ignorata, non trattata come "nessun comune": si
    ricade sull'euristica del prefisso per quel codice, come se la riga
    non ci fosse.

    Se il file non esiste (o e' vuoto/non ancora creato), restituisce un
    dizionario vuoto — classifica_distretto ricade allora SEMPRE
    sull'euristica del prefisso, comportamento identico a prima
    dell'introduzione di questo file (richiesto da Daniele il 18/09/2026,
    vedi specifiche 6.1: l'elenco ufficiale comuni/distretti mancava).
    """
    percorso = Path(percorso)
    if not percorso.exists():
        return {}
    df = pd.read_csv(percorso, dtype=str, keep_default_na=False)
    mappa = {}
    for _, riga in df.iterrows():
        codice = riga.get("codice_distretto", "").strip().upper()
        comune_ufficiale = riga.get("comune_ufficiale", "").strip().upper()
        if not codice or not comune_ufficiale:
            continue
        associabili = frozenset(
            c.strip().upper() for c in riga.get("comuni_associabili", "").split(";") if c.strip()
        )
        mappa[codice] = (comune_ufficiale, associabili)
    return mappa


COLONNE_MAPPA_DISTRETTI = ["codice_distretto", "nome_distretto", "comune_ufficiale", "comuni_associabili"]


def carica_mappa_distretti_df(percorso: str | Path = PERCORSO_MAPPA_DISTRETTI) -> pd.DataFrame:
    """Come carica_mappa_distretti_comuni, ma restituisce il DataFrame
    grezzo (una riga per codice, comuni_associabili come stringa separata
    da ';', comune_ufficiale eventualmente vuoto) invece del dizionario
    gia' pronto per classifica_distretto — usato dalla pagina di gestione
    /pagine/distretti per mostrare e modificare il file riga per riga.
    """
    percorso = Path(percorso)
    if not percorso.exists():
        return pd.DataFrame(columns=COLONNE_MAPPA_DISTRETTI)
    df = pd.read_csv(percorso, dtype=str, keep_default_na=False)
    for col in COLONNE_MAPPA_DISTRETTI:
        if col not in df.columns:
            df[col] = ""
    return df[COLONNE_MAPPA_DISTRETTI].sort_values("codice_distretto").reset_index(drop=True)


def salva_mappa_distretti_df(df: pd.DataFrame, percorso: str | Path = PERCORSO_MAPPA_DISTRETTI) -> None:
    """Scrive il DataFrame (stesse 3 colonne di COLONNE_MAPPA_DISTRETTI) su
    disco, sovrascrivendo il file — usata da upsert/elimina/importa, mai
    direttamente dalle pagine web.
    """
    percorso = Path(percorso)
    percorso.parent.mkdir(parents=True, exist_ok=True)
    df[COLONNE_MAPPA_DISTRETTI].sort_values("codice_distretto").to_csv(percorso, index=False)


def upsert_distretto(
    codice_distretto: str, comune_ufficiale: str, comuni_associabili: str = "",
    nome_distretto: str = "", percorso: str | Path = PERCORSO_MAPPA_DISTRETTI,
) -> None:
    """Aggiunge o aggiorna (per codice_distretto) una riga dell'elenco —
    usata dal modulo "aggiungi/modifica" di /pagine/distretti. nome_distretto
    e' solo descrittivo (mai usato da classifica_distretto, che lavora sul
    codice): aiuta a leggere la tabella senza dover ricordare a memoria
    cosa e' ogni codice.
    """
    codice = codice_distretto.strip().upper()
    if not codice:
        raise ValueError("Codice distretto obbligatorio")
    df = carica_mappa_distretti_df(percorso)
    df = df[df["codice_distretto"].str.upper() != codice]
    nuova_riga = pd.DataFrame([{
        "codice_distretto": codice,
        "nome_distretto": nome_distretto.strip(),
        "comune_ufficiale": comune_ufficiale.strip().upper(),
        "comuni_associabili": comuni_associabili.strip().upper(),
    }])
    salva_mappa_distretti_df(pd.concat([df, nuova_riga], ignore_index=True), percorso)


def elimina_distretto(codice_distretto: str, percorso: str | Path = PERCORSO_MAPPA_DISTRETTI) -> None:
    """Rimuove una riga dall'elenco per codice_distretto (nessun errore se
    non esisteva gia')."""
    codice = codice_distretto.strip().upper()
    df = carica_mappa_distretti_df(percorso)
    salva_mappa_distretti_df(df[df["codice_distretto"].str.upper() != codice], percorso)


_ALIAS_COLONNE_IMPORT = {
    "codice_distretto": {"codice_distretto", "codice distretto", "distretto", "cod_distretto", "codice"},
    "nome_distretto": {"nome_distretto", "nome distretto", "denominazione distretto", "descrizione distretto", "nome"},
    "comune_ufficiale": {"comune_ufficiale", "comune ufficiale", "comune", "comune_principale", "comune principale"},
    "comuni_associabili": {
        "comuni_associabili", "comuni associabili", "associabili",
        "comuni_limitrofi", "comuni limitrofi", "comuni_associati", "comuni associati",
        "altro comune associabile", "altro_comune_associabile", "comune associabile", "comune_associabile",
    },
}


def importa_mappa_distretti(
    percorso_file: str | Path, percorso_destinazione: str | Path = PERCORSO_MAPPA_DISTRETTI
) -> dict:
    """Importa un file CSV o Excel che Daniele gia' ha (es. esportato da
    WMS SmartH2O) e lo fonde (upsert per codice_distretto — le righe nuove
    aggiornano quelle esistenti, non sovrascrivono l'intero elenco) con
    quello gia' presente. Riconosce le intestazioni anche con nomi simili
    al nostro formato (maiuscole/minuscole, spazi, "Comune" invece di
    "comune_ufficiale", ecc. — vedi _ALIAS_COLONNE_IMPORT); la colonna
    comuni_associabili e' opzionale, le altre due no. Se non riesce a
    riconoscere codice_distretto/comune_ufficiale solleva ValueError con
    l'elenco delle colonne trovate nel file, cosi' si capisce subito cosa
    aggiustare (richiesto da Daniele il 18/09/2026, non si conosceva in
    anticipo il formato esatto del file che avrebbe caricato).
    """
    percorso_file = Path(percorso_file)
    if percorso_file.suffix.lower() in (".xlsx", ".xls"):
        grezzo = pd.read_excel(percorso_file, dtype=str)
    else:
        # sep=None + engine="python" fa riconoscere da solo il separatore:
        # un export Excel in locale italiano usa quasi sempre ';', non ','
        # (la virgola e' il separatore decimale in italiano) — scoperto sul
        # primo file reale caricato da Daniele il 18/09/2026.
        grezzo = pd.read_csv(percorso_file, dtype=str, keep_default_na=False, sep=None, engine="python")
    grezzo.columns = [str(c).strip() for c in grezzo.columns]
    grezzo = grezzo.fillna("")

    colonne_minuscole = {c.lower().strip(): c for c in grezzo.columns}
    mappa_colonne = {}
    for standard, varianti in _ALIAS_COLONNE_IMPORT.items():
        trovata = next((colonne_minuscole[v] for v in varianti if v in colonne_minuscole), None)
        if trovata:
            mappa_colonne[standard] = trovata

    mancanti = [c for c in ("codice_distretto", "comune_ufficiale") if c not in mappa_colonne]
    if mancanti:
        raise ValueError(
            f"Non riesco a riconoscere le colonne {mancanti} nel file '{percorso_file.name}'. "
            f"Colonne trovate: {', '.join(grezzo.columns) or '(nessuna)'}"
        )

    pulito = pd.DataFrame({
        "codice_distretto": grezzo[mappa_colonne["codice_distretto"]].astype(str).str.strip().str.upper(),
        "nome_distretto": (
            grezzo[mappa_colonne["nome_distretto"]].astype(str).str.strip()
            if "nome_distretto" in mappa_colonne else ""
        ),
        "comune_ufficiale": grezzo[mappa_colonne["comune_ufficiale"]].astype(str).str.strip().str.upper(),
        "comuni_associabili": (
            grezzo[mappa_colonne["comuni_associabili"]].astype(str).str.strip().str.upper()
            if "comuni_associabili" in mappa_colonne else ""
        ),
    })
    pulito = pulito[pulito["codice_distretto"] != ""]
    if pulito.empty:
        raise ValueError(f"Nessuna riga con un codice distretto valido nel file '{percorso_file.name}'")

    esistente = carica_mappa_distretti_df(percorso_destinazione)
    combinato = pd.concat([esistente, pulito], ignore_index=True).drop_duplicates(
        subset="codice_distretto", keep="last"
    )
    salva_mappa_distretti_df(combinato, percorso_destinazione)

    return {"righe_importate": len(pulito), "righe_totali": len(combinato)}


_ALIAS_PROPRIETA_CODICE_GEOJSON = {
    "codice_distretto", "codice distretto", "codice", "distretto", "cod_distretto", "distretto_codice",
    # Nomi tipici di un export GIS/ArcGIS (es. "DMA.json" caricato da
    # Daniele il 18/09/2026: OBJECTID, GisId, GisDescription, GisCode...).
    "giscode", "gis_code", "gis code", "gis_codice",
}


def importa_confini_distretti(
    percorso_file: str | Path, percorso_destinazione: str | Path = PERCORSO_CONFINI_DISTRETTI
) -> dict:
    """Importa un GeoJSON (FeatureCollection, una Feature per distretto,
    poligono o multipoligono) con i confini reali — usato dalla pagina
    Mappa al posto dei quadrati segnaposto, non appena presente su disco.

    Riconosce da solo quale proprieta' di ogni feature contiene il codice
    distretto (stessi alias di _ALIAS_PROPRIETA_CODICE_GEOJSON, es.
    'codice', 'DISTRETTO'...) e la copia dentro properties.codice_distretto
    su OGNI feature, cosi' il template della mappa cerca sempre lo stesso
    nome di campo indipendentemente da come si chiamava nel file originale
    (sovrascrive l'intero file, a differenza dell'upsert del CSV: i confini
    non si "fondono" riga per riga, un nuovo file e' sempre la versione
    completa e definitiva). Se non riesce a riconoscerla solleva ValueError
    con le proprieta' trovate nella prima feature, per capire come
    sistemare (richiesto da Daniele il 18/09/2026).
    """
    percorso_file = Path(percorso_file)
    dati = json.loads(percorso_file.read_text(encoding="utf-8"))
    if dati.get("type") != "FeatureCollection" or not dati.get("features"):
        raise ValueError(
            f"Il file '{percorso_file.name}' non è un GeoJSON FeatureCollection con almeno una feature"
        )

    prima_proprieta = dati["features"][0].get("properties") or {}
    proprieta_minuscole = {str(k).lower().strip(): k for k in prima_proprieta}
    proprieta_codice = next(
        (proprieta_minuscole[a] for a in _ALIAS_PROPRIETA_CODICE_GEOJSON if a in proprieta_minuscole), None
    )
    if not proprieta_codice:
        raise ValueError(
            "Non riesco a riconoscere quale proprietà contiene il codice distretto nel file "
            f"'{percorso_file.name}'. Proprietà trovate nella prima feature: "
            f"{', '.join(prima_proprieta.keys()) or '(nessuna)'}"
        )

    for feature in dati["features"]:
        codice = str((feature.get("properties") or {}).get(proprieta_codice, "")).strip().upper()
        feature.setdefault("properties", {})["codice_distretto"] = codice

    percorso_destinazione = Path(percorso_destinazione)
    percorso_destinazione.parent.mkdir(parents=True, exist_ok=True)
    percorso_destinazione.write_text(json.dumps(dati, ensure_ascii=False), encoding="utf-8")

    return {"n_feature": len(dati["features"]), "proprieta_usata": proprieta_codice}


def classifica_distretto(df: pd.DataFrame, mappa_distretti: dict | None = None) -> pd.DataFrame:
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
    convenzioni diverse (es. "NODMA") — qualunque codice che inizia per
    "NO" viene trattato come case_sparse, non anomalia.

    Precisazioni di Daniele (19/09/2026): "*" e' lo stesso di campo vuoto
    (distretto mancante). Un codice di un altro comune su un'utenza di
    questa estrazione (es. DBRN01 in Belgioioso) NON sposta l'utenza in quel
    comune: il comune prevale e il codice DMA e' sbagliato, perche'
    l'associazione massiva utenza->distretto e' stata fatta con
    un'operazione GIS sulla posizione dell'ultima lettura del letturista,
    a volte errata (dato inviato da altrove, lettura dichiarata sul posto
    ma non fatta).

    Per i codici che NON iniziano per "NO", la classificazione usa PRIMA
    l'elenco ufficiale (vedi carica_mappa_distretti_comuni): un codice
    riconosciuto e' 'valido' se il suo comune ufficiale e' quello di questa
    estrazione (comune_dominante(df)) O se questo comune e' tra i suoi
    comuni_associabili (es. una frazione alimentata dal comune vicino) —
    altrimenti e' 'anomalia' (codice di un altro comune, non associato: un
    errore vero, quasi sempre). Un codice ASSENTE dall'elenco (o presente
    con comune_ufficiale vuoto) ricade sull'euristica del prefisso
    dominante del file, come prima di avere l'elenco (richiesto da Daniele
    il 18/09/2026: distinguere errore vero da comune limitrofo legittimo
    non era possibile solo col prefisso).
    """
    df = df.copy()
    prefisso = _prefisso_dominante(df["DISTRETTO"])
    if mappa_distretti is None:
        mappa_distretti = carica_mappa_distretti_comuni()
    comune_corrente = comune_dominante(df).strip().upper()

    def categoria(val: str) -> str:
        val_pulito = str(val).strip().upper()
        if val_pulito.startswith("NO") and val_pulito not in ("", "NAN"):
            return "case_sparse"
        if val_pulito in mappa_distretti:
            comune_ufficiale, associabili = mappa_distretti[val_pulito]
            if comune_ufficiale == comune_corrente or comune_corrente in associabili:
                return "valido"
            return "anomalia"
        if prefisso and val.startswith(prefisso):
            return "valido"
        return "anomalia"

    df["CATEGORIA_DISTRETTO"] = df["DISTRETTO"].apply(categoria)

    def motivo(row):
        if row["CATEGORIA_DISTRETTO"] != "anomalia":
            return ""
        distretto = str(row["DISTRETTO"]).strip().upper()
        if distretto in ("*", "NAN", ""):
            return "Codice distretto mancante o non valido"
        if distretto in mappa_distretti:
            comune_ufficiale, _ = mappa_distretti[distretto]
            return (
                f"Codice DMA '{row['DISTRETTO']}' appartiene a {comune_ufficiale}, ma l'utenza e' "
                f"nel comune di {comune_corrente}: il comune prevale, il codice DMA e' quasi certamente "
                "errato (associazione GIS sulla posizione dell'ultima lettura). Se invece e' una "
                "frazione alimentata dal comune vicino, aggiungerlo come associabile in "
                "project_docs/distretti_comuni.csv"
            )
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

# LEGAMI_FORNITURA: quasi sempre "INDIFFERENTE" (contatore indipendente),
# ma puo' essere "PADRE" (misura il consumo TOTALE di un condominio) o
# "FIGLIO" (sotto-contatore interno, il suo consumo e' gia' incluso in
# quello del padre — confermato da Daniele il 18/09/2026). Un FIGLIO non va
# mai fatturato separatamente: verrebbe contato due volte nel totale del
# distretto, una nel padre e una nel figlio. Vedi elabora_dataframe, dove
# le righe FIGLIO sono escluse prima di calcola_periodi_metodo_b (restano
# comunque nell'archivio, solo escluse dal Metodo B).
LEGAME_FIGLIO = "FIGLIO"

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
    2. Dentro ogni segmento, la prima lettura NON sentinella (di qualunque
       tipo, vedi LETTURA_SENTINELLA_MIN — le sentinella in testa vengono
       scartate e finiscono tra le anomalie) è il punto di partenza (BASE):
       non genera consumo da sola, serve solo come riferimento — non
       sappiamo cosa è successo prima di lei.
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
       REALI consecutive risulta NEGATIVA, la LETTURA in se' non e' presa
       per buona (un contatore dell'acqua non torna mai indietro fuori da
       un cambio) — ma il periodo NON si azzera piu': si STIMA con il
       ritmo medio storico (m3/giorno) calcolato sugli altri periodi validi
       (differenza reale-reale) della STESSA utenza, applicato ai giorni
       del periodo. Se l'utenza non ha nessun periodo storico valido da cui
       ricavare un ritmo (es. il reset e' nel primissimo segmento noto), il
       periodo resta escluso (0) come prima. In entrambi i casi finisce
       comunque nel secondo DataFrame restituito (anomalie), da verificare
       a mano — stimarlo non lo rende meno sospetto, solo meno probabile
       che sia zero. L'ancora si sposta comunque alla nuova lettura, per
       non propagare l'errore in avanti (richiesto da Daniele il
       18/09/2026, caso di verifica: utenza 53886970 — un reset non
       marcato non significa che l'utenza abbia consumato zero in quei
       giorni).
       Se invece l'ancora precedente era la BASE del segmento ed era una
       STIMA (non una reale), una differenza negativa NON è un'anomalia:
       è la prima reale che corregge (conguaglia) una stima per eccesso,
       cosa normalissima (vedi 2.4/2.10) — non finisce tra le anomalie da
       verificare con Neta H2O, solo nel riferimento, senza generare
       consumo per quel primo intervallo (richiesto da Daniele il
       18/09/2026, caso di verifica: utenza 75768720).

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
        # Periodi "differenza letture reali" (OK) di QUESTA utenza, tenuti
        # separati dal periodi globale finche' non sono stati chiusi tutti
        # i segmenti: servono a calcolare il ritmo medio storico
        # dell'utenza (vedi sotto, pendenti_reset), quindi non possono
        # mescolarsi con le stime provvisorie ne' con quelli di altre
        # utenze.
        periodi_utenza: list = []
        # Periodi con reset di contatore non marcato (differenza negativa
        # tra due ancore REALI): non si escludono piu' a zero, si stimano
        # con il ritmo medio storico dell'utenza — ma quel ritmo si conosce
        # solo DOPO aver chiuso tutti i segmenti dell'utenza, quindi restano
        # "pendenti" fino ad allora (richiesto da Daniele il 18/09/2026,
        # caso di verifica: utenza 53886970 — un reset non marcato non
        # significa che l'utenza abbia consumato zero in quei giorni).
        pendenti_reset: list = []

        def chiudi_segmento(segmento):
            if not segmento:
                return
            # Scarta eventuali letture sentinella (LETTURA >=
            # LETTURA_SENTINELLA_MIN) in TESTA al segmento prima di
            # scegliere la base: altrimenti un valore spazzatura (es.
            # 9999989, lo stesso placeholder "lettura non disponibile" di
            # Neta H2O visto altrove come 999999/9999999) diventava
            # l'ancora di partenza senza mai essere controllato — il
            # controllo sentinella esiste gia' per le letture DENTRO il
            # segmento, ma la base ne era esente per design (vedi punto 2
            # sotto). La prima reale successiva, confrontata con
            # quell'ancora spazzatura, risultava "diminuita, probabile
            # reset contatore non marcato": non era un reset, era la base
            # stessa non valida (richiesto da Daniele il 18/09/2026, caso
            # di verifica: utenza 53787788).
            indice_base = 0
            while indice_base < len(segmento) and segmento[indice_base].LETTURA >= LETTURA_SENTINELLA_MIN:
                scartata = segmento[indice_base]
                anomalie.append({
                    "CODICE_SERVIZIO": scartata.CODICE_SERVIZIO, "DATA_LETTURA": scartata.DATA_LETTURA,
                    "LETTURA": scartata.LETTURA, "TIPO_LETTURA": scartata.TIPO_LETTURA,
                    "MOTIVO": "Lettura sentinella (valore non valido, ignorata)",
                    "DISTRETTO": scartata.DISTRETTO, "FILE_ORIGINE": scartata.FILE_ORIGINE,
                })
                indice_base += 1
            if indice_base >= len(segmento):
                return  # tutto il segmento era sentinella: nessun punto di partenza utilizzabile
            segmento = segmento[indice_base:]

            base = segmento[0]
            ancora_lettura, ancora_data = base.LETTURA, base.DATA_LETTURA
            indice_ultima_ancora = 0  # indice in segmento dell'ultima ancora reale (0 = base)
            # Se la base del segmento e' una stima (non sappiamo cosa e'
            # successo prima di lei), una prima reale piu' bassa la sta
            # semplicemente correggendo (conguaglio) — non e' un reset di
            # contatore: la protezione sotto (delta<0 -> "probabile reset
            # non marcato") va applicata solo quando ANCHE l'ancora
            # precedente era gia' una lettura reale (vedi 2.10: scoperta e
            # validata sulle diminuzioni tra due reali consecutive, non
            # stima->reale). Richiesto da Daniele il 18/09/2026 dopo aver
            # verificato il caso 75768720 (stima 1369 il 31/08, reale 1365
            # il 10/11: normalissimo, la stima puo' essere per eccesso).
            ancora_e_reale = base.TIPO_LETTURA in TIPI_LETTURA_REALE

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
                    if giorni > 0 and delta < 0 and ancora_e_reale:
                        # Non si esclude piu' a zero: si accantona, e si
                        # risolve DOPO aver chiuso tutti i segmenti
                        # dell'utenza (vedi pendenti_reset piu' sopra),
                        # cosi' si puo' stimare con il ritmo medio storico
                        # dell'utenza invece di attribuire zero consumo.
                        pendenti_reset.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_LETTURA": riga.DATA_LETTURA,
                            "LETTURA": riga.LETTURA, "TIPO_LETTURA": riga.TIPO_LETTURA,
                            "ANCORA_LETTURA": ancora_lettura, "GIORNI": giorni, "DELTA": delta,
                            "LOCALITA": riga.LOCALITA, "DISTRETTO": riga.DISTRETTO,
                            "CATEGORIA_DISTRETTO": riga.CATEGORIA_DISTRETTO,
                            "PRODOTTO_CODICE": riga.PRODOTTO_CODICE, "FILE_ORIGINE": riga.FILE_ORIGINE,
                        })
                    elif giorni > 0 and delta < 0:
                        # Ancora precedente non reale (stima iniziale del
                        # segmento): la reale la corregge, non e' un'anomalia
                        # da verificare con Neta H2O — solo tracciata nel
                        # foglio di riferimento per trasparenza, senza
                        # generare consumo per questo primo intervallo (non
                        # sappiamo cosa sia successo davvero prima della
                        # prima lettura reale).
                        riferimento.append({
                            "CODICE_SERVIZIO": riga.CODICE_SERVIZIO, "DATA_FINE": riga.DATA_LETTURA,
                            "GIORNI": giorni, "VOLUME_M3": delta, "M3_GIORNO": round(ritmo, 2),
                            "LOCALITA": riga.LOCALITA, "DISTRETTO": riga.DISTRETTO, "FILE_ORIGINE": riga.FILE_ORIGINE,
                            "ESITO": "Escluso: la reale corregge una stima iniziale (nessun consumo attribuito, non e' un'anomalia)",
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
                        periodi_utenza.append({
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
                    ancora_e_reale = True
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

        # Tutti i segmenti dell'utenza sono chiusi: periodi_utenza contiene
        # ora tutte le differenze reale-reale valide (le stime provvisorie
        # in coda sono gia' finite direttamente in periodi, non contano per
        # il ritmo: non sono una misura fisica). Da qui il ritmo medio
        # storico, usato per stimare i periodi con reset non marcato invece
        # di escluderli a zero.
        giorni_ok = sum(p["GIORNI"] for p in periodi_utenza)
        volume_ok = sum(p["VOLUME_M3"] for p in periodi_utenza)
        ritmo_medio_utenza = (volume_ok / giorni_ok) if giorni_ok > 0 else None

        for pend in pendenti_reset:
            if ritmo_medio_utenza is not None:
                volume_stimato = round(ritmo_medio_utenza * pend["GIORNI"], 2)
                periodi_utenza.append({
                    "CODICE_SERVIZIO": pend["CODICE_SERVIZIO"], "DATA_FINE": pend["DATA_LETTURA"],
                    "GIORNI": pend["GIORNI"], "VOLUME_M3": volume_stimato,
                    "LOCALITA": pend["LOCALITA"], "DISTRETTO": pend["DISTRETTO"],
                    "CATEGORIA_DISTRETTO": pend["CATEGORIA_DISTRETTO"],
                    "PRODOTTO_CODICE": pend["PRODOTTO_CODICE"], "FILE_ORIGINE": pend["FILE_ORIGINE"],
                    "ORIGINE": "stima (reset contatore non marcato, interpolata dal ritmo storico dell'utenza)",
                })
                motivo = (
                    f"Lettura diminuita rispetto alla precedente ({pend['ANCORA_LETTURA']}->{pend['LETTURA']}), "
                    f"probabile reset contatore non marcato — periodo STIMATO con il ritmo medio storico "
                    f"dell'utenza ({ritmo_medio_utenza:.2f} m3/giorno: {volume_stimato} m3), da verificare"
                )
                esito = f"Stimato con il ritmo medio dell'utenza ({ritmo_medio_utenza:.2f} m3/giorno, reset contatore non marcato)"
                volume_riferimento = volume_stimato
            else:
                # Nessun periodo valido pregresso per questa utenza (es. il
                # reset e' nel primissimo segmento noto): nessun ritmo da
                # cui stimare, resta escluso come prima della modifica.
                motivo = (
                    f"Lettura diminuita rispetto alla precedente ({pend['ANCORA_LETTURA']}->{pend['LETTURA']}), "
                    f"probabile reset contatore non marcato — nessun periodo storico valido per stimarlo, escluso"
                )
                esito = "Escluso: lettura diminuita (probabile reset contatore non marcato, nessun ritmo storico disponibile per stimare)"
                volume_riferimento = pend["DELTA"]
            anomalie.append({
                "CODICE_SERVIZIO": pend["CODICE_SERVIZIO"], "DATA_LETTURA": pend["DATA_LETTURA"],
                "LETTURA": pend["LETTURA"], "TIPO_LETTURA": pend["TIPO_LETTURA"],
                "MOTIVO": motivo,
                "DISTRETTO": pend["DISTRETTO"], "FILE_ORIGINE": pend["FILE_ORIGINE"],
            })
            riferimento.append({
                "CODICE_SERVIZIO": pend["CODICE_SERVIZIO"], "DATA_FINE": pend["DATA_LETTURA"],
                "GIORNI": pend["GIORNI"], "VOLUME_M3": volume_riferimento,
                "M3_GIORNO": round(volume_riferimento / pend["GIORNI"], 2) if pend["GIORNI"] > 0 else 0,
                "LOCALITA": pend["LOCALITA"], "DISTRETTO": pend["DISTRETTO"], "FILE_ORIGINE": pend["FILE_ORIGINE"],
                "ESITO": esito,
            })

        periodi.extend(periodi_utenza)

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


def trova_utenze_corrette_da_nodma(df_prorata_b: pd.DataFrame) -> pd.DataFrame:
    """Individua le utenze la cui classificazione di distretto e' cambiata
    nella storia dell'archivio da non distrettualizzata/anomala (NODMA/ND,
    vedi classifica_distretto) a un distretto vero — tipicamente perche'
    Neta H2O corregge un errore di anagrafica dopo essere stata avvisata,
    cosa che spesso richiede mesi (richiesto da Daniele il 18/09/2026).

    NON e' un'anomalia nel calcolo: il periodo che si CHIUDE con la prima
    lettura reale del distretto corretto viene gia' attribuito (proratato
    sui mesi giusti) al nuovo distretto in automatico, grazie al ricalcolo
    completo ad ogni caricamento — vedi calcola_periodi_metodo_b, "il
    distretto della lettura di chiusura vince". Il punto da segnalare e'
    un altro: i periodi CHIUSI PRIMA della correzione (quando l'utenza era
    ancora NODMA/ND) restano esclusi da qualunque distretto per sempre,
    ANCHE DOPO la correzione — la deduplica dell'archivio (vedi
    database.aggiorna_letture: a parita' di chiave vince la riga gia'
    presente) non li aggiornerebbe nemmeno ricaricando uno storico corretto.
    """
    colonne = [
        "Codice Servizio", "Distretto Attuale", "Volume Escluso Permanentemente (m3)",
        "Mesi Interessati", "N. Mesi",
    ]
    if df_prorata_b.empty:
        return pd.DataFrame(columns=colonne)

    per_utenza_categorie = df_prorata_b.groupby("CODICE_SERVIZIO")["CATEGORIA_DISTRETTO"].apply(set)
    interessate = per_utenza_categorie[
        per_utenza_categorie.apply(lambda s: "valido" in s and bool(s & {"case_sparse", "anomalia"}))
    ].index
    if len(interessate) == 0:
        return pd.DataFrame(columns=colonne)

    righe = []
    for codice, gruppo in df_prorata_b[df_prorata_b["CODICE_SERVIZIO"].isin(interessate)].groupby("CODICE_SERVIZIO"):
        valido = gruppo[gruppo["CATEGORIA_DISTRETTO"] == "valido"]
        non_valido = gruppo[gruppo["CATEGORIA_DISTRETTO"] != "valido"]
        if valido.empty or non_valido.empty:
            continue
        # Solo i mesi non validi PRIMA del primo mese valido: sono quelli
        # rimasti chiusi mentre l'utenza era ancora NODMA/ND, quindi
        # permanentemente esclusi da un distretto. Un eventuale mese non
        # valido dopo il primo valido (dato ballerino) non e' il caso
        # descritto da Daniele, resta fuori da questa segnalazione.
        primo_mese_valido = valido["MESE"].min()
        persi = non_valido[non_valido["MESE"] < primo_mese_valido]
        if persi.empty:
            continue
        righe.append({
            "Codice Servizio": codice,
            "Distretto Attuale": valido.sort_values("MESE").iloc[-1]["DISTRETTO"],
            "Volume Escluso Permanentemente (m3)": round(persi["VOLUME_MESE_M3"].sum(), 2),
            "Mesi Interessati": ", ".join(str(m) for m in sorted(persi["MESE"].unique())),
            "N. Mesi": persi["MESE"].nunique(),
        })

    if not righe:
        return pd.DataFrame(columns=colonne)
    return pd.DataFrame(righe)[colonne].sort_values("Volume Escluso Permanentemente (m3)", ascending=False)


def _bucket_origine(origine: str) -> str:
    """Raggruppa il campo ORIGINE di un periodo del Metodo B (vedi
    calcola_periodi_metodo_b) in una delle 3 categorie usate ovunque nelle
    statistiche/grafici: 'Reale (m3)' (differenza fisica tra letture
    reali), 'Provvisorio (m3)' (stima in attesa di conferma, si
    autocorregge da sola) o 'Interpolato (m3)' (stima da reset di
    contatore non marcato, non si autocorregge da sola).
    """
    if origine == "differenza letture reali":
        return "Reale (m3)"
    if origine.startswith("stima provvisoria"):
        return "Provvisorio (m3)"
    return "Interpolato (m3)"


def mese_max_statistiche(df_prorata_b: pd.DataFrame, soglia_pct_reale: float = 85.0) -> pd.Period | None:
    """L'ultimo mese (andando a ritroso dal piu' recente) con una quota di
    volume REALE sopra soglia_pct_reale — il segnale che il lotto di
    letture per quel periodo si e' gia' chiuso. Si ferma al primo mese che
    supera la soglia: i mesi precedenti a quello restano dentro anche se
    singolarmente piu' bassi (rumore normale nella serie), solo la CODA
    finale ancora aperta viene esclusa.

    Usato per tagliare la coda in statistiche/grafici (MAI Import_WMS):
    un lotto trimestrale di letture non ancora arrivato per l'ultimo
    periodo fa apparire quei mesi con un volume basso e poco affidabile
    (richiesto da Daniele il 18/09/2026, caso di verifica: Belgioioso,
    marzo/aprile 2026 scesi al 80,8%/23,8% di reale, contro l'86-99% dei
    mesi precedenti). NON e' lo stesso problema del "cold start" iniziale
    (vedi _mese_dopo_primo_trimestre): li' il volume e' basso perche'
    manca ancora storia PRIMA, qui perche' manca la chiusura DOPO — due
    meccanismi distinti e non sovrapponibili: verificato che la % Reale
    dei primissimi mesi di un archivio nuovo e' gia' alta (98-99%), il
    cold start non abbassa l'affidabilita', solo il totale.

    Restituisce None se non c'e' nessun mese sopra soglia — il chiamante
    allora non applica nessun taglio, coerente con "non si inventa un
    confronto quando manca il dato".
    """
    valide = df_prorata_b[df_prorata_b["CATEGORIA_DISTRETTO"] == "valido"]
    if valide.empty:
        return None
    valide = valide.copy()
    valide["_bucket"] = valide["ORIGINE"].apply(_bucket_origine)
    per_mese = valide.groupby(["MESE", "_bucket"])["VOLUME_MESE_M3"].sum().unstack("_bucket", fill_value=0.0)
    for col in ("Reale (m3)", "Provvisorio (m3)", "Interpolato (m3)"):
        if col not in per_mese.columns:
            per_mese[col] = 0.0
    totale = per_mese.sum(axis=1)
    pct_reale = (per_mese["Reale (m3)"] / totale * 100).where(totale > 0)

    for mese in sorted(pct_reale.index, reverse=True):
        if pd.notna(pct_reale[mese]) and pct_reale[mese] >= soglia_pct_reale:
            return mese
    return None


def aggrega_origine_mensile(
    df_prorata_b: pd.DataFrame, mese_min: pd.Period | None, mese_max: pd.Period | None
) -> pd.DataFrame:
    """Come volumi_distretto_mese, ma scompone il volume di ogni (Mese,
    Distretto) per ORIGINE (vedi calcola_periodi_metodo_b) in 3 colonne:
    Reale (differenza fisica tra letture reali), Provvisorio (stima ancora
    in attesa di una reale che la confermi, si autocorregge da sola) e
    Interpolato (stima da reset di contatore non marcato, NON si
    autocorregge da sola — resta cosi' finche' non si verifica il dato con
    Neta H2O). Pensata per il grafico mensile impilato della pagina
    pagina Volumi (richiesto da Daniele il 18/09/2026): la % di stimato
    non e' un indicatore unico perche' le due categorie hanno un percorso
    di risoluzione diverso (vedi conversazione), quindi restano separate
    fin da qui invece di essere sommate in un solo "% stimato".
    """
    colonne = ["Mese", "Codice Distretto", "Reale (m3)", "Provvisorio (m3)", "Interpolato (m3)"]
    if df_prorata_b.empty:
        return pd.DataFrame(columns=colonne)

    valide = df_prorata_b
    if mese_min is not None and mese_max is not None:
        valide = valide[(valide["MESE"] >= mese_min) & (valide["MESE"] <= mese_max)]
    if valide.empty:
        return pd.DataFrame(columns=colonne)

    # Include ANCHE case sparse (NODMA) e distretto anomalo/mancante (ND) —
    # vedi _distretto_per_statistiche: a differenza di Import_WMS, qui sono
    # utili a fini statistici (richiesto da Daniele il 18/09/2026).
    valide = valide.copy()
    valide["DISTRETTO"] = [
        _distretto_per_statistiche(cat, dist)
        for cat, dist in zip(valide["CATEGORIA_DISTRETTO"], valide["DISTRETTO"])
    ]
    valide["_bucket"] = valide["ORIGINE"].apply(_bucket_origine)

    pivot = (
        valide.groupby(["MESE", "DISTRETTO", "_bucket"])["VOLUME_MESE_M3"]
        .sum()
        .unstack("_bucket", fill_value=0.0)
        .reset_index()
        .rename(columns={"MESE": "Mese", "DISTRETTO": "Codice Distretto"})
    )
    for col in ("Reale (m3)", "Provvisorio (m3)", "Interpolato (m3)"):
        if col not in pivot.columns:
            pivot[col] = 0.0
        pivot[col] = pivot[col].round(2)
    return pivot[colonne].sort_values(["Mese", "Codice Distretto"])


def aggrega_utenza_mese(
    df_prorata_b: pd.DataFrame, mese_min: pd.Period | None, mese_max: pd.Period | None
) -> pd.DataFrame:
    """Volume mensile per SINGOLA utenza (solo mesi affidabili, solo
    distretti validi — stessa finestra e stesso filtro degli altri
    risultati). Non finisce mai nel foglio Excel/nel JSON dei totali per
    distretto: serve solo come base per la classifica dei maggiori
    consumatori mostrata in pagina Volumi (richiesto da Daniele il
    18/09/2026), calcolata a richiesta dal layer web per un mese o un anno
    a scelta invece di essere precalcolata qui per ogni possibile periodo.
    """
    colonne = ["Codice Servizio", "Codice Distretto", "Classe d'uso", "Mese", "Volume (m3)"]
    if df_prorata_b.empty:
        return pd.DataFrame(columns=colonne)

    valide = df_prorata_b[df_prorata_b["CATEGORIA_DISTRETTO"] == "valido"]
    if mese_min is not None and mese_max is not None:
        valide = valide[(valide["MESE"] >= mese_min) & (valide["MESE"] <= mese_max)]
    if valide.empty:
        return pd.DataFrame(columns=colonne)

    agg = (
        valide.groupby(["CODICE_SERVIZIO", "DISTRETTO", "PRODOTTO_CODICE", "MESE"], as_index=False)["VOLUME_MESE_M3"]
        .sum()
        .rename(columns={
            "CODICE_SERVIZIO": "Codice Servizio", "DISTRETTO": "Codice Distretto",
            "PRODOTTO_CODICE": "Classe d'uso", "MESE": "Mese", "VOLUME_MESE_M3": "Volume (m3)",
        })
    )
    agg["Volume (m3)"] = agg["Volume (m3)"].round(2)
    return agg[colonne]


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


def _distretto_per_statistiche(categoria_distretto: str, distretto) -> str:
    """Etichetta un punto per le viste STATISTICHE (mai per Import_WMS, che
    deve restare con i soli codici distretto veri riconosciuti da WMS
    SmartH2O — vedi aggrega()): 'NODMA' per i punti non distrettualizzati
    (case sparse: cascine isolate, frazioni alimentate da altri acquedotti,
    ecc. — vedi classifica_distretto), 'ND' per quelli con un distretto
    anomalo/mancante, altrimenti il codice distretto vero. Richiesto da
    Daniele il 18/09/2026: prima questi punti sparivano silenziosamente
    dalle statistiche per classe d'uso e dal grafico mensile, invece di
    comparire come una loro categoria a se' — utili a fini statistici anche
    se non fatturabili a nessun distretto specifico.
    """
    if categoria_distretto == "case_sparse":
        return "NODMA"
    if categoria_distretto == "anomalia":
        return "ND"
    return distretto


def aggrega_classe_uso_statistiche(df_prorata: pd.DataFrame) -> pd.DataFrame:
    """Come la seconda tabella restituita da aggrega() (volume per
    distretto+mese+classe d'uso), ma SENZA scartare case sparse (NODMA) e
    distretto anomalo/mancante (ND) — vedi _distretto_per_statistiche. Usata
    solo per le statistiche per classe d'uso (calcola_statistiche_classe_uso),
    mai per Import_WMS.
    """
    colonne = ["Mese", "Codice Distretto", "Classe d'uso", "Volume (m3)"]
    if df_prorata.empty:
        return pd.DataFrame(columns=colonne)

    df = df_prorata.copy()
    df["DISTRETTO"] = [
        _distretto_per_statistiche(cat, dist)
        for cat, dist in zip(df["CATEGORIA_DISTRETTO"], df["DISTRETTO"])
    ]
    risultato = (
        df.groupby(["MESE", "DISTRETTO", "PRODOTTO_CODICE"], as_index=False)["VOLUME_MESE_M3"]
        .sum()
        .rename(columns={
            "MESE": "Mese", "DISTRETTO": "Codice Distretto",
            "PRODOTTO_CODICE": "Classe d'uso", "VOLUME_MESE_M3": "Volume (m3)",
        })
    )
    risultato["Volume (m3)"] = risultato["Volume (m3)"].round(2)
    return risultato[colonne].sort_values(["Mese", "Codice Distretto", "Classe d'uso"])


def conteggio_utenze_per_distretto_classe_statistiche(df_tutti: pd.DataFrame) -> pd.DataFrame:
    """Come conteggio_utenze_per_distretto_classe, ma SENZA scartare case
    sparse (NODMA) e distretto anomalo/mancante (ND) — denominatore
    coerente con aggrega_classe_uso_statistiche per "Utenze Attive Oggi" e
    "Consumo Medio" nelle statistiche per classe d'uso.
    """
    anagrafica = _ultima_anagrafica_utenze(df_tutti)
    if anagrafica.empty:
        return pd.DataFrame(columns=["Codice Distretto", "Classe d'uso", "Totale Utenze"])

    anagrafica = anagrafica.copy()
    anagrafica["DISTRETTO"] = [
        _distretto_per_statistiche(cat, dist)
        for cat, dist in zip(anagrafica["CATEGORIA_DISTRETTO"], anagrafica["DISTRETTO"])
    ]
    risultato = _pivot_stato(anagrafica, ["DISTRETTO", "PRODOTTO_CODICE"])
    return risultato.rename(
        columns={"DISTRETTO": "Codice Distretto", "PRODOTTO_CODICE": "Classe d'uso"}
    ).sort_values(["Codice Distretto", "Classe d'uso"])


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


def _mese_dopo_primo_trimestre(mese_min: pd.Period) -> pd.Period:
    """Il primo mese DOPO il trimestre solare in cui cade mese_min — usato
    per escludere il trimestre di apertura dell'archivio ("cold start", vedi
    calcola_statistiche_trimestrali) anche dalle statistiche e dai grafici
    mensili, non solo da quelli trimestrali (richiesto da Daniele il
    18/09/2026: scartarlo per statistiche/grafici, ma tenerlo sempre in
    archivio e nei volumi "ufficiali" come Import_WMS).
    """
    return (mese_min.asfreq("Q") + 1).asfreq("M", how="start")


def calcola_statistiche_trimestrali(volumi_distretto_trimestre: pd.DataFrame) -> pd.DataFrame:
    """Per ogni (Trimestre, Distretto) del Riepilogo_Trimestrale: dotazione
    idrica media (m3/utenza/giorno, usando "Utenze Attive" gia' calcolato),
    variazione % rispetto al trimestre precedente dello stesso distretto, e
    variazione % rispetto allo stesso trimestre dell'anno precedente.

    Le variazioni restano vuote (None) quando manca il dato di confronto
    (es. un trimestre precedente non ancora caricato) — non si inventa mai
    un confronto con un buco in mezzo.

    Il PRIMO trimestre di ogni distretto (quello in cui l'archivio "parte":
    la maggior parte delle utenze ha li' la sua primissima lettura nota, che
    per il Metodo B non genera consumo da sola — "effetto cold start")
    viene escluso del tutto da questa tabella, su richiesta esplicita di
    Daniele il 18/09/2026: la sua dotazione idrica sarebbe artificialmente
    bassa, e la variazione % del trimestre SUCCESSIVO (che lo userebbe come
    base di confronto) sarebbe artificialmente gonfiata — visto su
    Belgioioso, dove il primo trimestre (2025-T3) fa risultare il
    successivo a +162% di "crescita" che non e' mai successa davvero. I
    volumi di quel trimestre restano intatti ovunque altrove
    (Riepilogo_Trimestrale, Import_WMS...): solo questa tabella di trend lo
    scarta.
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
    df["_e_primo_trimestre"] = df["_ord"] == df.groupby("Codice Distretto")["_ord"].transform("min")
    primo_trimestre_per_distretto = {
        riga["Codice Distretto"]: (riga["_anno"], riga["_num_trim"])
        for _, riga in df[df["_e_primo_trimestre"]].iterrows()
    }

    prec = df.groupby("Codice Distretto").shift(1)
    prec_e_cold_start = df.groupby("Codice Distretto")["_e_primo_trimestre"].shift(1)

    var_prec = []
    for v_att, v_prec, ord_att, ord_prec, precedente_e_cold_start in zip(
        df["Volume Fatturato (m3)"], prec["Volume Fatturato (m3)"], df["_ord"], prec["_ord"], prec_e_cold_start
    ):
        if (
            pd.isna(v_prec) or pd.isna(ord_prec) or ord_prec != ord_att - 1 or v_prec == 0
            or precedente_e_cold_start
        ):
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
        e_cold_start_riferimento = primo_trimestre_per_distretto.get(distretto) == (anno - 1, num_trim)
        if chiave in riferimento.index and not e_cold_start_riferimento:
            v_prec = riferimento.loc[chiave]
            if isinstance(v_prec, pd.Series):
                v_prec = v_prec.iloc[0]
            var_anno.append(round((vol - v_prec) / v_prec * 100, 1) if v_prec else None)
        else:
            var_anno.append(None)
    df["Variazione % vs Stesso Trimestre Anno Precedente"] = var_anno

    df = df[~df["_e_primo_trimestre"]]

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

    finestra = volumi_distretto_mese_classe
    if mese_min is not None and mese_max is not None:
        finestra = finestra[(finestra["Mese"] >= mese_min) & (finestra["Mese"] <= mese_max)]
    if finestra.empty:
        return pd.DataFrame(columns=colonne)

    tot = (
        finestra.groupby(["Codice Distretto", "Classe d'uso"], as_index=False)["Volume (m3)"]
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
    di STATI_CHIUSURA_ATTESI) la cui ULTIMA lettura nota (di consumo) NON è
    una lettura reale (vedi TIPI_LETTURA_REALE piu' sopra, sezione Metodo B).

    PERCHE' E' UN'ANOMALIA (confermato da Daniele): quando un'utenza cessa
    e' obbligatorio effettuare una lettura reale di chiusura. Se l'ultima
    lettura nota e' invece una stima, il contratto potrebbe essere stato
    chiuso senza la lettura di chiusura dovuta — da verificare a mano.

    Le righe INIZIALE ESCLUSO/INCLUSO (TIPI_INIZIO_CONTATORE) sono escluse
    PRIMA di cercare "l'ultima": sono marcatori di apertura di un nuovo
    contatore (valore sempre 0, non una lettura di consumo), e Neta H2O ne
    logga una anche quando un contratto si chiude lo stesso giorno di un
    cambio contatore — subito dopo la reale di chiusura (RIMOZIONE PER
    CAMBIO) che la precede nello stesso giorno (vedi _PRIORITA_STESSA_DATA).
    Senza questo filtro, quella riga fittizia risultava "l'ultima lettura"
    e faceva scattare l'anomalia anche quando la reale di chiusura c'era
    eccome (richiesto da Daniele il 18/09/2026, caso di verifica: utenza
    53787283).
    """
    colonne_output = [
        "Codice Servizio", "Indirizzo", "Distretto", "Stato Servizio",
        "Ultima Lettura (tipo)", "Ultima Data Lettura", "File Origine",
    ]
    if df_tutti.empty:
        return pd.DataFrame(columns=colonne_output)

    df_no_apertura = df_tutti[~df_tutti["TIPO_LETTURA"].isin(TIPI_INIZIO_CONTATORE)]
    if df_no_apertura.empty:
        return pd.DataFrame(columns=colonne_output)

    ultima = (
        _ordina_priorita_stessa_data(df_no_apertura)
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


# Stato di chiusura fatturazione per mese (richiesto da Daniele il
# 19/09/2026): l'ufficio fatturazione, a ogni giro, emette una STIMATA per
# (quasi) tutte le utenze con DATA_LETTURA = ultimo giorno del mese
# fatturato (es. fatturato ad aprile -> stima al 30/04; a maggio -> 30/05).
# Un picco di STIMATE su un fine mese e' quindi il segnale che quel lotto e'
# stato girato. Nei dati reali: Belgioioso 31/08 (2163), 30/11 (2286),
# 28/02 (1426); Mortara 30/09, 31/12, 31/03, 30/06 (1400-4000 l'una), contro
# poche unita' (4-34) sugli altri fine mese. Regola scelta da Daniele
# ("picco relativo"): soglia in percentuale sul picco del comune stesso, non
# un numero assoluto, cosi' vale per comuni grandi e piccoli.
SOGLIA_LOTTO_PCT_PICCO = 30.0
# Con meno di due lotti nello storico il "picco tipico" coincide con l'unico
# lotto visto e non e' affidabile: lo stato e' "non determinabile".
MIN_LOTTI_PER_DETERMINARE = 2


def calcola_stato_chiusura_mesi(
    df_tutti: pd.DataFrame, soglia_pct_picco: float = SOGLIA_LOTTO_PCT_PICCO
) -> pd.DataFrame:
    """Per ogni mese tra la prima e l'ultima DATA_LETTURA dell'archivio (di
    UN comune: il picco e' relativo al comune) dice se il lotto di
    fatturazione di quel mese risulta girato.

    Stato: "Chiuso" (STIMATE a fine mese >= soglia_pct_picco % del picco),
    "Senza lotto" (sotto soglia: mese non fatturato in un giro, o giro non ancora girato), "Non determinabile" (meno di
    MIN_LOTTI_PER_DETERMINARE lotti nello storico). Colonne aggiuntive:
    quante STIMATE a fine mese, e "Letture Reali Tardive" = letture reali
    con DATA_LETTURA nel mese arrivate in un file successivo a quello che
    contiene il lotto di stime (conguaglio di stime gia' emesse). E' solo
    informativo: non cambia il calcolo Metodo B.
    """
    colonne = ["Mese", "Stime a Fine Mese", "Picco Comune", "% del Picco", "Stato",
               "File del Lotto", "Letture Reali Tardive"]
    if df_tutti.empty:
        return pd.DataFrame(columns=colonne)

    df = df_tutti[["DATA_LETTURA", "TIPO_LETTURA", "FILE_ORIGINE"]].dropna(subset=["DATA_LETTURA"]).copy()
    if df.empty:
        return pd.DataFrame(columns=colonne)
    df["_mese"] = df["DATA_LETTURA"].dt.to_period("M")
    fine_mese = df["DATA_LETTURA"].dt.normalize() == df["_mese"].dt.end_time.dt.normalize()
    stime = df[fine_mese & (df["TIPO_LETTURA"] == "LETTURA STIMATA")]
    conteggio = stime.groupby("_mese").size()

    mesi = pd.period_range(df["_mese"].min(), df["_mese"].max(), freq="M")
    conteggio = conteggio.reindex(mesi, fill_value=0)
    picco = int(conteggio.max())
    e_lotto = (conteggio >= picco * soglia_pct_picco / 100) & (conteggio > 0)
    determinabile = int(e_lotto.sum()) >= MIN_LOTTI_PER_DETERMINARE

    # Ordine cronologico dei file, come in trova_utenze_scomparse: per
    # data di lettura piu' vecchia.
    ordine_file = df.groupby("FILE_ORIGINE")["DATA_LETTURA"].min().rank(method="first")
    reali = df[df["TIPO_LETTURA"].isin(TIPI_LETTURA_REALE)]

    righe = []
    for mese in mesi:
        n = int(conteggio[mese])
        if not determinabile:
            stato = "Non determinabile"
        else:
            stato = "Chiuso" if e_lotto[mese] else "Senza lotto"
        file_lotto = ""
        tardive = 0
        if stato == "Chiuso":
            file_lotto = min(stime.loc[stime["_mese"] == mese, "FILE_ORIGINE"], key=lambda f: ordine_file[f])
            reali_mese = reali[reali["_mese"] == mese]
            tardive = int((reali_mese["FILE_ORIGINE"].map(ordine_file) > ordine_file[file_lotto]).sum())
        righe.append({
            "Mese": mese, "Stime a Fine Mese": n, "Picco Comune": picco,
            "% del Picco": round(n / picco * 100, 1) if picco else 0.0,
            "Stato": stato, "File del Lotto": file_lotto, "Letture Reali Tardive": tardive,
        })
    return pd.DataFrame(righe, columns=colonne)


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


def unisci_distretti_fusi(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Sostituisce i codici dei distretti soppressi (DISTRETTI_FUSI) con
    quello del distretto in cui sono stati fusi. Restituisce il DataFrame e
    quante righe sono state spostate per codice vecchio."""
    codici = df["DISTRETTO"].astype(str).str.strip().str.upper()
    fusi = codici.isin(DISTRETTI_FUSI)
    if not fusi.any():
        return df, {}
    df = df.copy()
    df.loc[fusi, "DISTRETTO"] = codici[fusi].map(DISTRETTI_FUSI)
    return df, codici[fusi].value_counts().to_dict()


def elabora_dataframe(df_grezzo: pd.DataFrame) -> RisultatoElaborazione:
    """Come elabora_file, ma parte da un DataFrame già caricato (es.
    l'archivio storico) invece che da percorsi di file su disco.
    """
    warning: list[str] = []
    riepilogo_righe = []
    grezzi = []
    distretti_visti_per_file = []  # indice allineato a riepilogo_righe/grezzi

    df_grezzo, spostate = unisci_distretti_fusi(df_grezzo)
    if spostate:
        warning.append(
            "Distretti soppressi uniti al distretto nuovo: "
            + ", ".join(f"{vecchio} -> {DISTRETTI_FUSI[vecchio]} ({n} righe)" for vecchio, n in sorted(spostate.items()))
            + ". Neta non ha ancora aggiornato il CRM: vedi il tab Prese per il file da mandare."
        )

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
    # Finestra per statistiche/grafici: parte DOPO il trimestre di "cold
    # start" (vedi _mese_dopo_primo_trimestre) — i volumi "ufficiali"
    # (Import_WMS, Riepilogo_Trimestrale) restano su mese_min/mese_max
    # senza questo taglio, cambia solo cio' che finisce nelle statistiche
    # e nei grafici.
    mese_min_statistiche = _mese_dopo_primo_trimestre(mese_min)

    segnalazioni = costruisci_segnalazioni(df_tutti)

    # Contatori FIGLIO (LEGAMI_FORNITURA — vedi LEGAME_FIGLIO piu' sopra):
    # esclusi dal Metodo B, MAI dall'archivio. Il padre misura gia' il
    # consumo totale del condominio (confermato da Daniele il 18/09/2026):
    # calcolare anche la differenza dei figli conterebbe il loro consumo
    # due volte nel totale del distretto.
    figli = df_tutti[df_tutti["LEGAMI_FORNITURA"] == LEGAME_FIGLIO]
    if not figli.empty:
        n_figli = figli["CODICE_SERVIZIO"].nunique()
        n_padri = df_tutti.loc[df_tutti["LEGAMI_FORNITURA"] == "PADRE", "CODICE_SERVIZIO"].nunique()
        warning.append(
            f"{n_figli} utenze sono contatori FIGLIO (sotto-contatori di {n_padri} "
            "contatori PADRE): il loro consumo è già incluso nella lettura del padre, "
            "quindi sono escluse dal calcolo dei volumi per distretto (Metodo B) per non "
            "contarlo due volte. Restano visibili nell'archivio letture, solo escluse dal "
            "calcolo — vedi LEGAMI_FORNITURA nel file Neta H2O."
        )
    df_tutti_billing = df_tutti[df_tutti["LEGAMI_FORNITURA"] != LEGAME_FIGLIO]

    # --- METODO B (differenza di letture, le reali vincono sulle
    # stimate) --- UNICO metodo usato dal 16/09/2026: confermato da
    # Daniele che e' questa la logica corretta di fatturazione (le
    # letture reali conguagliano le stime, non si sommano ad esse — vedi
    # 2.4/2.10). E' questo il volume che alimenta district_billed (foglio
    # Import_WMS).
    periodi_b, anomalie_metodo_b, riferimento_prodie = calcola_periodi_metodo_b(df_tutti_billing)
    if not anomalie_metodo_b.empty:
        warning.append(
            f"Metodo B: {len(anomalie_metodo_b)} letture escluse dal calcolo perché "
            "sentinella (valore non valido) o perché la lettura risultava diminuita senza "
            "un cambio contatore marcato nel file. Dettaglio nel foglio 'Anomalie_MetodoB' "
            "— probabile problema di qualità dati da chiedere a Neta H2O."
        )
    df_prorata_b = prorata_mensile_metodo_b(periodi_b)
    # Taglio di coda per statistiche/grafici (mai Import_WMS), simmetrico a
    # mese_min_statistiche ma per il motivo opposto: non "manca storia
    # prima" ma "manca la chiusura dopo" — vedi mese_max_statistiche.
    ultimo_mese_affidabile = mese_max_statistiche(df_prorata_b)
    mese_max_statistiche_valore = (
        min(mese_max, ultimo_mese_affidabile) if ultimo_mese_affidabile is not None else mese_max
    )
    if ultimo_mese_affidabile is not None and ultimo_mese_affidabile < mese_max:
        warning.append(
            f"Statistiche e grafici si fermano a {ultimo_mese_affidabile}: i mesi successivi (fino a "
            f"{mese_max}) non hanno ancora una quota di volume reale sufficiente — il lotto di letture "
            "trimestrale per quel periodo non è ancora arrivato. Import_WMS li include comunque, "
            "verranno confermati/corretti al prossimo caricamento."
        )
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
    # NODMA (case sparse) e ND (distretto anomalo/mancante) inclusi qui —
    # mai in volumi_distretto_mese_b_classe/utenze_per_distretto_classe,
    # che restano con i soli distretti veri per Import_WMS/Excel
    # "Dettaglio_Classi". Vedi _distretto_per_statistiche.
    statistiche_classe_uso = calcola_statistiche_classe_uso(
        aggrega_classe_uso_statistiche(df_prorata_b),
        conteggio_utenze_per_distretto_classe_statistiche(df_tutti),
        mese_min_statistiche, mese_max_statistiche_valore,
    )
    coefficiente_punta = calcola_coefficiente_punta(
        volumi_distretto_mese_b[
            (volumi_distretto_mese_b["Mese"] >= mese_min_statistiche)
            & (volumi_distretto_mese_b["Mese"] <= mese_max_statistiche_valore)
        ]
    )
    volumi_distretto_mese_origine = aggrega_origine_mensile(
        df_prorata_b, mese_min_statistiche, mese_max_statistiche_valore
    )
    volumi_utenza_mese = aggrega_utenza_mese(df_prorata_b, mese_min_statistiche, mese_max_statistiche_valore)

    # Utenze passate da NODMA/ND a un distretto vero nella storia
    # dell'archivio: vedi trova_utenze_corrette_da_nodma per il perche' e'
    # solo una segnalazione, non una correzione automatica del calcolo.
    utenze_corrette_da_nodma = trova_utenze_corrette_da_nodma(df_prorata_b)
    if not utenze_corrette_da_nodma.empty:
        tot_escluso = utenze_corrette_da_nodma["Volume Escluso Permanentemente (m3)"].sum()
        warning.append(
            f"{len(utenze_corrette_da_nodma)} utenze sono passate da non distrettualizzate/anomale "
            f"(NODMA/ND) a un distretto vero nella storia dell'archivio: {tot_escluso:,.0f} m3 restano "
            "esclusi per sempre da qualunque distretto (i periodi chiusi PRIMA della correzione). "
            "Dettaglio nel foglio 'Utenze_Corrette_Da_NODMA'."
        )

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
        volumi_distretto_mese_origine=volumi_distretto_mese_origine,
        volumi_utenza_mese=volumi_utenza_mese,
        utenze_corrette_da_nodma=utenze_corrette_da_nodma,
        stato_chiusura_mesi=calcola_stato_chiusura_mesi(df_tutti),
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

        df_nodma = risultato.utenze_corrette_da_nodma.copy()
        if "Mesi Interessati" in df_nodma.columns:
            df_nodma["Mesi Interessati"] = df_nodma["Mesi Interessati"].astype(str)
        df_nodma.to_excel(writer, sheet_name="Utenze_Corrette_Da_NODMA", index=False)

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
