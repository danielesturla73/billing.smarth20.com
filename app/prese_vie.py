"""
Ricognizione dei nomi delle vie e delle frazioni (Daniele, 30/09/2026): le vie di Neta che non si
abbinano a OpenStreetMap o ad ANNCSU, o si abbinano in modo ambiguo, con i candidati vicini. Serve a
trovare in un colpo solo i problemi di nome (frazioni, localita', abbreviazioni, refusi, vie che
mancano in OSM) invece di scoprirli un DP alla volta. Solo lettura: non modifica niente.
"""
from __future__ import annotations

import difflib
import io

import pandas as pd

from app import anncsu, cache_disco, database, stradario, vie_osm
from app.prese import _RE_FRAZIONE, _versione_conferme, _versione_dati, assegnazioni, prese_comune

SOGLIA_SUGGERIMENTO = 0.6
MAX_SUGGERIMENTI = 3


def _suggerimenti(via: str, nomi: list[str]) -> list[str]:
    """I nomi piu' simili a una via non trovata (per parole e per lettere), per capire se e' un refuso."""
    _, pv = vie_osm.parole(via)
    base = " ".join(sorted(pv)) or via.upper()
    punteggi = []
    for n in nomi:
        _, po = vie_osm.parole(n)
        if not po:
            continue
        testo = " ".join(sorted(po))
        r = difflib.SequenceMatcher(None, base, testo).ratio()
        comuni = len(pv & po) / max(1, len(pv | po))
        punteggi.append((max(r, comuni), n))
    punteggi.sort(reverse=True)
    return [n for s, n in punteggi[:MAX_SUGGERIMENTI] if s >= SOGLIA_SUGGERIMENTO]


def _stato(via: str, nomi: list[str]) -> tuple[str, str]:
    """(stato, testo): 'ok' | 'ambigua' | 'nessuna', con il nome trovato, i pari merito o i suggerimenti."""
    if not nomi:
        return "assente", "(nessun dato per il comune)"
    stato, trovati = vie_osm.cerca_via(via, nomi)
    if stato == "ok":
        return "ok", trovati[0]
    if stato == "ambigua":
        return "ambigua", " | ".join(trovati)
    sugg = _suggerimenti(via, nomi)
    return "nessuna", ("simili: " + " | ".join(sugg)) if sugg else ""


COLONNE = ["COMUNE", "VIA", "TIPO", "PROBLEMA", "DP", "DP_APERTI", "DISTRETTI", "ESEMPIO",
           "STATO_OSM", "OSM", "STATO_ANNCSU", "ANNCSU"]


def _vie_comune(comune: str) -> pd.DataFrame:
    """Le righe del comune (una per via con un problema di nome), senza ordinare."""
    righe = []
    dati_anncsu = anncsu._dati()
    p = prese_comune(comune)
    if p.empty:
        return pd.DataFrame(columns=COLONNE)
    with database.connessione() as conn:
        a = assegnazioni(conn, comune)
    chiusi = set(a["DP"]) if not a.empty else set()
    vie = [stradario.normalizza_indirizzo(i)[0] for i in p["INDIRIZZO"]]
    p = p.assign(VIA=vie, APERTO=[(m != "") and (k not in chiusi) for m, k in zip(p["MOTIVO"], p["CHIAVE"])])
    osm = list(vie_osm.vie_comune(comune))
    an = sorted(set(dati_anncsu[dati_anncsu["COMUNE"] == comune.strip().upper()]["ODONIMO"]))
    for via, g in p.groupby("VIA"):
        if not via:
            continue
        tipo = "Frazione, localita' o cascina" if _RE_FRAZIONE.match(via) else "Via"
        s_osm, t_osm = _stato(via, osm)
        s_an, t_an = _stato(via, an)
        if tipo == "Via" and s_osm in ("ok", "assente") and s_an in ("ok", "assente"):
            continue  # nessun problema (o nessun dato con cui confrontare)
        if tipo != "Via" and s_an == "ok":
            continue  # una frazione non ha un tracciato OSM: conta ANNCSU
        if s_osm == "ambigua" or s_an == "ambigua":
            problema = "Nome ambiguo: due o piu' candidati a pari merito"
        elif s_osm == "nessuna" and s_an == "nessuna":
            problema = "Non trovata ne' in OSM ne' in ANNCSU"
        elif s_osm == "nessuna":
            problema = "Non trovata in OSM (c'e' in ANNCSU)" if s_an == "ok" else "Non trovata in OSM"
        elif s_an == "nessuna":
            problema = "Non trovata in ANNCSU (c'e' in OSM)" if s_osm == "ok" else "Non trovata in ANNCSU"
        else:
            problema = "Da controllare"
        righe.append({
            "COMUNE": comune, "VIA": via, "TIPO": tipo, "PROBLEMA": problema,
            "DP": len(g), "DP_APERTI": int(g["APERTO"].sum()),
            "DISTRETTI": ", ".join(f"{d} {n}" for d, n in g["DISTRETTO_PRINCIPALE"].value_counts().items()),
            "ESEMPIO": g["INDIRIZZO"].iloc[0],
            "STATO_OSM": s_osm, "OSM": t_osm, "STATO_ANNCSU": s_an, "ANNCSU": t_an,
        })
    return pd.DataFrame(righe, columns=COLONNE)


def vie_da_verificare(comuni: list[str]) -> pd.DataFrame:
    """Una riga per via di Neta con un problema di nome (vedi _vie_comune), ordinate per DP ancora da confermare.
    Il risultato di ogni comune si salva su disco (cancellabile) e vale finche' non cambiano i dati, le conferme
    del comune, le vie OSM o gli odonimi ANNCSU o il codice: il calcolo di tutti i comuni costa circa un minuto."""
    parti = []
    versione = _versione_dati()
    for comune in comuni:
        chiave = (comune, versione, _versione_conferme(comune),
                  *(f.stat().st_mtime_ns if f.exists() else 0 for f in (vie_osm.PERCORSO_VIE_OSM, anncsu.PERCORSO_ANNCSU)))
        df = cache_disco.carica(f"vie_{comune}", chiave)
        if df is None:
            df = _vie_comune(comune)
            cache_disco.salva(f"vie_{comune}", chiave, df)
        parti.append(df)
    df = pd.concat(parti, ignore_index=True) if parti else pd.DataFrame(columns=COLONNE)
    if not df.empty:
        df = df.sort_values(["DP_APERTI", "DP"], ascending=False).reset_index(drop=True)
    return df


LEGENDA = [
    ("Cosa contiene", "Le vie (e le frazioni, localita', cascine) di Neta che non si abbinano bene a OpenStreetMap o ad ANNCSU: senza "
                      "abbinamento l'app perde una fonte per capire il distretto. Una riga per via, ordinate per DP ancora da confermare."),
    ("Nome ambiguo", "Due o piu' nomi in OSM o in ANNCSU sono a pari merito con la via di Neta: l'app non sceglie. Nella colonna a "
                     "fianco ci sono i candidati."),
    ("Non trovata", "Nessun nome di OSM o ANNCSU contiene le parole della via di Neta. 'simili' elenca i nomi piu' vicini (un refuso "
                    "o un nome scritto diversamente); se e' vuoto la via probabilmente manca in OSM o in ANNCSU."),
    ("Frazione, localita' o cascina", "Indirizzi che iniziano con FRAZIONE, LOCALITA o CASCINA (C.NA): non hanno un tracciato in OSM, "
                                      "si abbinano solo ad ANNCSU."),
    ("DP aperti", "Punti di erogazione della via con un motivo di segnalazione e ancora da confermare."),
    ("Come si risolve", "Refuso o nome diverso: si puo' correggere in Neta, oppure aggiungere una regola di abbinamento. Via che manca "
                        "in OSM: si puo' aggiungere in OpenStreetMap e riscaricare le vie. Via che manca in ANNCSU: dipende dal Comune."),
    ("Fonti", "ANNCSU - Agenzia delle Entrate e ISTAT (CC-BY 4.0); (c) OpenStreetMap contributors (ODbL)."),
]

COLONNE_EXCEL = {
    "COMUNE": "Comune", "VIA": "Via (Neta)", "TIPO": "Tipo", "PROBLEMA": "Problema", "DP": "DP", "DP_APERTI": "DP aperti",
    "DISTRETTI": "Distretti in Neta", "ESEMPIO": "Esempio di indirizzo", "STATO_OSM": "OSM", "OSM": "OSM: nome o simili",
    "STATO_ANNCSU": "ANNCSU", "ANNCSU": "ANNCSU: nome o simili",
}


def esporta_vie_excel(comuni: list[str]) -> bytes:
    df = vie_da_verificare(comuni)
    riepilogo = (df.groupby(["COMUNE", "PROBLEMA"]).agg(vie=("VIA", "size"), dp=("DP", "sum"), dp_aperti=("DP_APERTI", "sum"))
                 .reset_index().rename(columns={"COMUNE": "Comune", "PROBLEMA": "Problema", "vie": "Vie", "dp": "DP", "dp_aperti": "DP aperti"})
                 .sort_values(["Comune", "DP aperti"], ascending=[True, False])) if not df.empty else pd.DataFrame(columns=["Comune", "Problema", "Vie", "DP", "DP aperti"])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        riepilogo.to_excel(writer, index=False, sheet_name="Riepilogo")
        det = df[list(COLONNE_EXCEL)].rename(columns=COLONNE_EXCEL)
        det.to_excel(writer, index=False, sheet_name="Vie da verificare")
        pd.DataFrame(LEGENDA, columns=["Voce", "Spiegazione"]).to_excel(writer, index=False, sheet_name="Legenda")
        for nome, tabella in (("Riepilogo", riepilogo), ("Vie da verificare", det)):
            foglio = writer.sheets[nome]
            for i, col in enumerate(tabella.columns, start=1):
                larghezza = min(60, max(10, len(col) + 2, *(len(str(v)) + 2 for v in tabella[col].head(300))))
                foglio.column_dimensions[foglio.cell(row=1, column=i).column_letter].width = larghezza
            foglio.freeze_panes = "A2"
            foglio.auto_filter.ref = foglio.dimensions
        fl = writer.sheets["Legenda"]
        fl.column_dimensions["A"].width = 32
        fl.column_dimensions["B"].width = 130
    return buffer.getvalue()
