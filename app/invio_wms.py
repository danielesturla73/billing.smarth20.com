"""
Invio dei volumi a WMS SmartH2O "a un click" (district_billed), sulla rete
Docker interna (Daniele, 29/09/2026). Non e' uno scheduler: una persona
guarda l'anteprima e poi preme Invia (vedi riepilogo di progetto, 4.9).

Cosa si manda: Import_WMS di ogni comune scelto (volumi_distretto_mese,
con gli anni consolidati gia' sostituiti dalla copia fissa), TUTTI i mesi
ogni volta. WMS fa l'upsert su (mese, distretto), quindi rimandare non fa
danni e i mesi provvisori si correggono da soli quando arriva la lettura
reale (4.8). Solo i mesi utili al bilancio (Daniele, 29/09/2026, dopo aver
visto Belgioioso a luglio/agosto 2025 e giugno 2026 con volumi bassissimi):
si salta il trimestre di apertura dell'archivio (cold start, stessa regola
di statistiche e grafici, vedi motore_calcolo._mese_dopo_primo_trimestre) e
i mesi incompleti (volume sotto il 70% dei mesi prima, di solito l'ultimo:
le letture non sono ancora arrivate — stessa regola della pagina Volumi).
Le righe gia' in WMS da questa app per mesi che non si mandano piu' vengono
cancellate all'invio (solo fonte "Analisi Consumi", mai i caricamenti a mano).
Provvisorio = piu' del 2% del volume del mese viene da stime
non ancora chiuse da una lettura reale; mai per un anno consolidato.
Le conferme del tab Prese valgono su tutta la storia (vedi
motore_calcolo.applica_riassegnazioni), quindi al primo invio dopo una
conferma WMS riceve corretti anche i mesi passati, tranne gli anni
consolidati, che restano la copia fissa.

WMS non cancella mai niente: le righe che sono in WMS (da questa fonte) ma
non piu' nell'invio tornano nell'anteprima come "non piu' inviate".

Anteprima e invio devono mandare gli stessi numeri: l'anteprima calcola una
firma delle righe, l'invio la ricalcola e si ferma se e' cambiata (per
esempio e' arrivata un'estrazione nel frattempo).
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

import pandas as pd

from app import database, motore_calcolo, prese as prese_mod

TIMEOUT_S = 120
# Provvisorio solo se le stime non ancora chiuse pesano piu' del 2% del
# volume del mese (Daniele, 29/09/2026): con "anche 1 m3" era provvisoria
# meta' delle righe, spesso per pochi m3 su migliaia. La nota riporta
# comunque i m3 stimati anche sotto soglia.
SOGLIA_PROVVISORIO = 0.02


def assicura_tabella(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invii_wms (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            QUANDO TEXT NOT NULL, UTENTE TEXT, COMUNI TEXT, N_RIGHE INTEGER,
            CONTEGGI TEXT, FIRMA TEXT, RIGHE TEXT
        )""")
    conn.commit()


def _m3(v) -> str:
    return f"{v:,.0f}".replace(",", ".")


def _parti_comune(risultato, mesi_incompleti) -> tuple[list[dict], list[str]]:
    """Import_WMS di un comune, una voce per (mese, distretto), con i m3 da
    stime provvisorie e interpolati del mese (0 per gli anni consolidati),
    senza i mesi da non mandare. Restituisce (voci, mesi esclusi)."""
    v = risultato.volumi_distretto_mese
    if v is None or v.empty:
        return [], []
    mesi = sorted(v["Mese"].astype(str).str[:7].unique())
    primo_utile = str(motore_calcolo._mese_dopo_primo_trimestre(pd.Period(mesi[0], freq="M")))
    dopo_apertura = v[v["Mese"].astype(str).str[:7] >= primo_utile]
    esclusi = {m for m in mesi if m < primo_utile} | {m[:7] for m in mesi_incompleti(dopo_apertura)}
    v = v[~v["Mese"].astype(str).str[:7].isin(esclusi)]
    o = risultato.volumi_distretto_mese_origine
    origine = {}
    if o is not None and not o.empty:
        for mese, codice, provv, interp in zip(o["Mese"].astype(str), o["Codice Distretto"].astype(str),
                                               o["Provvisorio (m3)"], o["Interpolato (m3)"]):
            origine[(mese, codice)] = (float(provv or 0), float(interp or 0))
    parti = []
    for mese, codice, volume, nota in zip(v["Mese"].astype(str), v["Codice Distretto"].astype(str),
                                          v["Volume Fatturato (m3)"], v["Note"].fillna("").astype(str)):
        consolidato = "consolidato il" in nota
        provv, interp = (0.0, 0.0) if consolidato else origine.get((mese, codice), (0.0, 0.0))
        parti.append({"mese": mese[:7], "codice_distretto": codice.strip().upper(), "volume": float(volume),
                      "provv": provv, "interp": interp, "nota": nota})
    return parti, sorted(esclusi)


def prepara(risultati: list[tuple[str, object]], comuni_scelti: list[str], mesi_incompleti) -> dict:
    """{righe, distretti_ambito, comuni, firma, mesi_esclusi}. risultati =
    TUTTI i comuni; mesi_incompleti = la regola della pagina Volumi
    (main._mesi_incompleti), passata da fuori per non importare main.

    Un distretto puo' avere utenze in piu' comuni (es. DBRN01 in Broni e
    Stradella, 29/09/2026): per WMS il volume e' la somma. Per questo, per
    ogni distretto dei comuni scelti, si sommano sempre i volumi di tutti
    i comuni in cui compare — altrimenti inviando solo Broni il totale
    giusto in WMS verrebbe sovrascritto con la sola parte di Broni."""
    scelti = {c.strip().upper() for c in comuni_scelti}
    per_comune, mesi_esclusi = {}, {}
    for c, r in risultati:
        per_comune[c], esclusi = _parti_comune(r, mesi_incompleti)
        if c.strip().upper() in scelti and esclusi:
            mesi_esclusi[c] = esclusi
    ambito = set()
    for c, r in risultati:
        if c.strip().upper() not in scelti:
            continue
        for tabella in (r.volumi_distretto_mese, r.volumi_distretto_mese_calcolati):
            if tabella is not None and not tabella.empty:
                ambito |= set(tabella["Codice Distretto"].astype(str).str.strip().str.upper())
    somme: dict[tuple[str, str], dict] = {}
    for comune, parti in per_comune.items():
        for x in parti:
            if x["codice_distretto"] not in ambito:
                continue
            s = somme.setdefault((x["codice_distretto"], x["mese"]),
                                 {"volume": 0.0, "provv": 0.0, "interp": 0.0, "note": [], "comuni": []})
            s["volume"] += x["volume"]
            s["provv"] += x["provv"]
            s["interp"] += x["interp"]
            s["comuni"].append(comune)
            if x["nota"] and x["nota"] not in s["note"]:
                s["note"].append(x["nota"])
    righe = []
    for (codice, mese), s in sorted(somme.items()):
        parti = list(s["note"])
        if len(s["comuni"]) > 1:
            parti.append("somma di " + " + ".join(sorted(s["comuni"])))
        if s["provv"] >= 0.5:
            parti.append(f"stime provvisorie {_m3(s['provv'])} m³")
        if s["interp"] >= 0.5:
            parti.append(f"interpolato {_m3(s['interp'])} m³")
        righe.append({
            "mese": mese, "codice_distretto": codice, "volume": round(s["volume"], 2),
            "provvisorio": bool(s["provv"] >= 0.5 and s["provv"] > SOGLIA_PROVVISORIO * abs(s["volume"])),
            "note": "; ".join(parti) or None,
        })
    comuni = sorted({c for s in somme.values() for c in s["comuni"]} | (scelti & set(per_comune)))
    mesi_validi = {c: {x["mese"] for x in parti} for c, parti in per_comune.items()}
    indicatori = _indicatori(risultati, scelti, ambito, mesi_validi)
    firma = hashlib.sha256(json.dumps([righe, indicatori], sort_keys=True).encode()).hexdigest()[:16]
    return {"righe": righe, "indicatori": indicatori, "distretti_ambito": sorted(ambito), "comuni": comuni,
            "firma": firma, "mesi_esclusi": mesi_esclusi}


# Classi d'uso di Neta (prefisso di PRODOTTO_CODICE) in tre gruppi per WMS
# (Daniele, 29/09/2026: l'industriale da solo pesa il 3%, conta di piu'
# domestico / non domestico / pubblico, soprattutto per il consumo notturno
# lecito). I pozzi a parte: acqua di pozzo privato, non della rete.
GRUPPI_CLASSE = {
    "DOM_RES": "domestico", "DOM_NO_RES": "domestico", "CONDOMINIO": "domestico",
    "ART_COMM": "non_domestico", "INDUSTRIALE": "non_domestico", "AGR_ZOOT": "non_domestico",
    "PUBBLICO_DIS": "pubblico", "PUBBLICO_NO_DIS": "pubblico",
}
NOMI_MENSILI = ["fatturato_domestico_mc", "fatturato_non_domestico_mc", "fatturato_pubblico_mc",
                "fatturato_pozzi_mc", "fatturato_altro_mc", "fatturato_fuori_distretto_mc"]


def _gruppo_classe(classe: str) -> str:
    codice = str(classe).split("-")[0].strip().upper()
    if codice.startswith("POZ"):
        return "pozzi"
    return GRUPPI_CLASSE.get(codice, "altro")


def _dp_comune(comune: str) -> tuple[dict[str, dict], str]:
    """{distretto: {dp, utenze}} delle utenze attive del comune (ultima
    estrazione, distretto confermato nel tab Prese se c'e'), e la data
    dell'ultima estrazione. Punti di erogazione = DP distinti (DP = delivery
    point, un contatore: NON la presa/allaccio, che in Neta non ha un codice —
    Daniele, 29/09/2026; il servizio da solo se il DP
    manca o e' il segnaposto), come nel tab Prese; attive = stato non
    cessato (CFAT, CNFA). Stessa classificazione dei volumi
    (motore_calcolo.classifica_distretto, per comune): i DP NO DISTRETTO
    vanno sotto la chiave "NODMA", quelli con distretto mancante o di un
    altro comune non associabile sotto "ND" (Daniele, 29/09/2026: anche in
    WMS i DP non distrettualizzati stanno per comune, zona NODMA)."""
    with database.connessione() as conn:
        s = pd.read_sql("SELECT CODICE_SERVIZIO, DP, DISTRETTO, STATO_SERVIZIO, DATA_ESTRAZIONE, LOCALITA FROM anagrafica_servizi "
                        "WHERE LOCALITA = ?", conn, params=(comune.strip().upper(),))
        # Contatori FIGLIO (sotto-contatori a valle di un PADRE, legame
        # dell'ultima lettura): non sono punti di erogazione dalla rete e nei
        # volumi sono gia' esclusi; restano tra le utenze (Daniele, 29/09/2026).
        legami = pd.read_sql(
            "SELECT CAST(CODICE_SERVIZIO AS TEXT) AS C, LEGAMI_FORNITURA AS L FROM letture WHERE LOCALITA = ? "
            "ORDER BY DATA_LETTURA", conn, params=(comune.strip().upper(),)) if database.tabella_esiste(conn) else pd.DataFrame(columns=["C", "L"])
    ultimi = legami.drop_duplicates("C", keep="last")
    figli = set(ultimi.loc[ultimi["L"] == motore_calcolo.LEGAME_FIGLIO, "C"].str.replace(r"\.0$", "", regex=True))
    if s.empty:
        return {}, ""
    riass = prese_mod.riassegnazioni_calcolo(comune)
    distretto = s["CODICE_SERVIZIO"].map(riass).fillna(s["DISTRETTO"]).fillna("").astype(str).str.strip().str.upper()
    s["D"] = distretto.map(lambda d: motore_calcolo.DISTRETTI_FUSI.get(d, d))
    s = s[~s["STATO_SERVIZIO"].fillna("").astype(str).str.upper().str.startswith("C")].copy()
    if s.empty:
        return {}, ""
    categoria = motore_calcolo.classifica_distretto(s.assign(DISTRETTO=s["D"]))["CATEGORIA_DISTRETTO"]
    s["D"] = s["D"].where(categoria == "valido", categoria.map({"case_sparse": "NODMA", "anomalia": "ND"}))
    s["CHIAVE"] = [f"S{c}" if (not dp or dp == prese_mod.DP_SEGNAPOSTO) else dp
                   for c, dp in zip(s["CODICE_SERVIZIO"], s["DP"].fillna(""))]
    s["CHIAVE_DP"] = s["CHIAVE"].where(~s["CODICE_SERVIZIO"].isin(figli))  # NaN = figlio, non conta come DP
    g = s.groupby("D").agg(dp=("CHIAVE_DP", "nunique"), utenze=("CODICE_SERVIZIO", "count"))
    data = str(pd.to_datetime(s["DATA_ESTRAZIONE"]).max().date()) if not s.empty else ""
    return {d: {"dp": int(r.dp), "utenze": int(r.utenze)} for d, r in g.iterrows()}, data


def _indicatori(risultati, scelti: set[str], ambito: set[str], mesi_validi: dict[str, set[str]]) -> list[dict]:
    """Indicatori per WMS (tabella district_indicatori): punti di erogazione
    (DP) e utenze attive per distretto — mai come "prese": l'UARL di WMS vuole
    gli allacci fisici, che sono molti meno e che Neta non ha —, fatturato mensile per
    gruppo di classe d'uso, e per comune il volume fuori distretto (NODMA =
    case sparse, ND = distretto anomalo o mancante). Stessi mesi dell'invio;
    i distretti in piu' comuni sommati su tutti i comuni."""
    uscita = []
    conteggi: dict[str, dict] = {}
    data_prese = ""
    classi: dict[tuple[str, str, str], float] = {}
    for comune, r in risultati:
        validi = mesi_validi.get(comune, set())
        per_distretto, data = _dp_comune(comune)
        data_prese = max(data_prese, data)
        if comune.strip().upper() in scelti and data:
            for zona in ("NODMA", "ND"):
                v = per_distretto.get(zona, {"dp": 0, "utenze": 0})
                uscita.append({"comune": comune, "codice_zona": zona, "data": data,
                               "nome": f"punti_erogazione_{zona.lower()}", "valore": v["dp"], "unita": "n"})
        for d, v in per_distretto.items():
            if d in ambito:
                acc = conteggi.setdefault(d, {"dp": 0, "utenze": 0})
                acc["dp"] += v["dp"]
                acc["utenze"] += v["utenze"]
        c = r.volumi_distretto_mese_classe
        if c is not None and not c.empty:
            for mese, codice, classe, vol in zip(c["Mese"].astype(str).str[:7], c["Codice Distretto"].astype(str),
                                                 c["Classe d'uso"], c["Volume (m3)"]):
                codice = codice.strip().upper()
                if codice in ambito and mese in validi:
                    k = (codice, mese, _gruppo_classe(classe))
                    classi[k] = classi.get(k, 0.0) + float(vol or 0)
        if comune.strip().upper() in scelti and r.volumi_comune_mese is not None and not r.volumi_comune_mese.empty:
            vc = r.volumi_comune_mese
            for mese, sparse, anomalo in zip(vc["Mese"].astype(str).str[:7], vc["Volume Case Sparse (m3)"],
                                             vc["Volume Distretto Anomalo/Mancante (m3)"]):
                if mese not in validi:
                    continue
                for zona, vol in (("NODMA", sparse), ("ND", anomalo)):
                    uscita.append({"comune": comune, "codice_zona": zona, "data": f"{mese}-01",
                                   "nome": "fatturato_fuori_distretto_mc", "valore": round(float(vol or 0), 2), "unita": "m3"})
    if data_prese:
        for d, v in sorted(conteggi.items()):
            uscita.append({"comune": None, "codice_zona": d, "data": data_prese, "nome": "punti_erogazione",
                           "valore": v["dp"], "unita": "n"})
            uscita.append({"comune": None, "codice_zona": d, "data": data_prese, "nome": "utenze_attive",
                           "valore": v["utenze"], "unita": "n"})
    for (codice, mese, gruppo), vol in sorted(classi.items()):
        uscita.append({"comune": None, "codice_zona": codice, "data": f"{mese}-01", "nome": f"fatturato_{gruppo}_mc",
                       "valore": round(vol, 2), "unita": "m3"})
    return uscita


def configurato() -> bool:
    return bool(os.environ.get("WMS_API_URL") and os.environ.get("INTERNAL_API_TOKEN"))


def chiama_wms(dati: dict, prova: bool, utente: str) -> dict:
    """POST /api/v1/billed/import su WMS. ValueError con un messaggio
    leggibile se WMS non risponde o rifiuta."""
    if not configurato():
        raise ValueError("WMS_API_URL o INTERNAL_API_TOKEN mancanti nel .env del container billing.")
    url = os.environ["WMS_API_URL"].rstrip("/") + "/api/v1/billed/import"
    corpo = json.dumps({"prova": prova, "utente": utente, "distretti_ambito": dati["distretti_ambito"],
                        "cancella_non_inviate": True, "righe": dati["righe"],
                        "indicatori": dati.get("indicatori", [])}).encode()
    richiesta = urllib.request.Request(url, data=corpo, method="POST", headers={
        "Content-Type": "application/json", "X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]})
    try:
        with urllib.request.urlopen(richiesta, timeout=TIMEOUT_S) as risposta:
            return json.loads(risposta.read())
    except urllib.error.HTTPError as exc:
        try:
            dettaglio = json.loads(exc.read()).get("detail", "")
        except Exception:
            dettaglio = ""
        raise ValueError(f"WMS ha risposto {exc.code}: {dettaglio or exc.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"WMS non raggiungibile ({url}): {getattr(exc, 'reason', exc)}")


def registra_invio(dati: dict, esito: dict, utente: str) -> int:
    with database.connessione() as conn:
        assicura_tabella(conn)
        cur = conn.execute(
            "INSERT INTO invii_wms (QUANDO, UTENTE, COMUNI, N_RIGHE, CONTEGGI, FIRMA, RIGHE) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), utente, ", ".join(dati["comuni"]), len(dati["righe"]),
             json.dumps(esito.get("conteggi", {})), dati["firma"], json.dumps(dati["righe"])),
        )
        conn.commit()
        return cur.lastrowid


def elenco_invii(limite: int = 30) -> list[dict]:
    with database.connessione() as conn:
        assicura_tabella(conn)
        righe = conn.execute(
            "SELECT ID, QUANDO, UTENTE, COMUNI, N_RIGHE, CONTEGGI FROM invii_wms ORDER BY ID DESC LIMIT ?", (limite,)
        ).fetchall()
    return [{"id": i, "quando": q[:16], "utente": u, "comuni": c, "n_righe": n, "conteggi": json.loads(k or "{}")}
            for i, q, u, c, n, k in righe]


def riassunto_anteprima(esito: dict) -> dict:
    """Per la pagina: le righe che cambiano (non le invariate), gli scarti
    raggruppati per distretto, le non piu' inviate."""
    righe = pd.DataFrame(esito.get("righe", []))
    if righe.empty:
        return {"cambiano": [], "scarti": [], "non_piu": esito.get("non_piu_inviate", [])}
    cambiano = righe[righe["azione"].isin(["creato", "aggiornato"])].copy()
    cambiano["differenza"] = cambiano["volume"] - cambiano["volume_prima"].fillna(0)
    scartate = righe[righe["azione"] == "scartato"]
    scarti = (scartate.groupby(["codice_distretto", "motivo"], as_index=False)
              .agg(mesi=("mese", "count"), volume=("volume", "sum")).to_dict("records")) if not scartate.empty else []
    return {
        "cambiano": cambiano.astype(object).where(cambiano.notna(), None).to_dict("records"),
        "scarti": scarti,
        "non_piu": esito.get("non_piu_inviate", []),
    }
