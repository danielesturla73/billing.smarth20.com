"""
Coordinate da verificare del tab Prese: controlli, coordinate proposte con fonte e affidabilita',
Excel con legenda. Staccato da app/prese.py il 27/09/2026 (solo spostato, stesso codice).
"""
from __future__ import annotations

import io
import math
import threading

import numpy as np
import pandas as pd

from app import cache_disco, motore_calcolo, stradario, vie_osm
from app.prese_geo import (
    TOLLERANZA_BORDO_M,
    TOLLERANZA_CONFINE_COMUNE_M,
    _comune_della_posizione,
    _dentro_confini,
    _dentro_e_distanza,
    _distanza_dal_comune_m,
    _distretti_sicuri,
    _nome_comune,
    _poligoni,
    _poligoni_comuni,
)
from app.prese import (
    LAT_VALIDA,
    LON_VALIDA,
    _versione_dati,
    prese_comune,
)


# Coordinate da verificare (file separato per Neta): una presa a piu' di
# tanto dal distretto piu' vicino del suo comune e' "fuori comune".
DISTANZA_FUORI_COMUNE_M = 1000

# Le case sparse (NO DISTRETTO) stanno spesso in campagna a 1-2 km dai
# distretti (mediana 1,6 km a settembre 2026): per loro solo oltre 5 km.
DISTANZA_FUORI_COMUNE_NODMA_M = 5000

# Coordinata lontana dal resto della via: un civico a piu' di tanto dal
# punto mediano dei civici vicini per numero (fino a 3 sotto e 3 sopra)
# della stessa via ha la coordinata sbagliata. A Belgioioso Via Molino 24
# (8 prese con la stessa coordinata), 26 e 40 stavano a ~1 km dal resto
# della via, dentro DBLG03, e lo stradario ne faceva un'eccezione (Daniele,
# 25/09/2026: Via Molino e' tutta DBLG02).
DISTANZA_FUORI_VIA_M = 400

VICINI_PER_LATO = 3

# Sulle strade di campagna i civici vicini per numero sono lontani anche nella
# realta': la soglia cresce con la dispersione dei vicini (tante volte la
# loro distanza mediana dal loro centro).
FATTORE_DISPERSIONE_VIA = 3

# Coordinata "segnaposto": lo stesso punto (al metro) usato per prese di
# almeno tante vie diverse (a Voghera un punto per 16 prese di 14 vie).
MIN_VIE_COORDINATA_CONDIVISA = 3

# Distanza massima di una presa dal tracciato OSM della sua via (le case
# possono stare arretrate dalla strada): a Belgioioso mediana 14 m, 90% entro
# 69 m, oltre 150 m 141 prese, tutte coordinate sbagliate a campione.
DISTANZA_MAX_DA_VIA_OSM_M = 150

# Distanza massima di una presa dal suo civico ANNCSU (la presa puo' stare in
# cortile o sul retro): la fonte piu' precisa, prima di OSM e dei vicini.
DISTANZA_MAX_DA_CIVICO_M = 150

# Coordinata proposta a Neta: affidabilita' (Daniele, 26/09/2026: proporre
# solo stime affidabili). Controlli incrociati sulla proposta: nel comune
# giusto (ISTAT), vicina alla sua via in OSM, nel distretto delle altre fonti.
DISTANZA_PROPOSTA_DA_VIA_OSM_M = 60

# Interpolazione lungo la via OSM tra due civici vicini dallo stesso lato:
# prese di riferimento solo se entro tanti metri dal tracciato; affidabilita'
# media se i due civici distano (lungo la via) al massimo GAP_MEDIA, oltre
# bassa, oltre GAP_MAX non si interpola.
DISTANZA_RIFERIMENTO_DA_VIA_M = 30

GAP_INTERPOLAZIONE_MEDIA_M = 100

GAP_INTERPOLAZIONE_MAX_M = 300

def fuori_dalla_via(p: pd.DataFrame) -> dict[str, tuple[int, float, float]]:
    """{chiave presa: (distanza m, lat, lon del punto mediano dei civici
    vicini)} per le prese la cui coordinata sta lontana dal resto della sua
    via: oltre DISTANZA_FUORI_VIA_M, o oltre FATTORE_DISPERSIONE_VIA volte
    la dispersione dei civici vicini se e' maggiore. La distanza e' della
    singola presa (al civico 24 di Via Molino 8 prese sbagliate e 1 giusta).
    Il punto di ogni civico vicino e' la mediana delle sue prese."""
    righe = []
    for chiave, indirizzo, lat, lon, ok in zip(p["CHIAVE"], p["INDIRIZZO"], p["LAT"], p["LON"], p["COORD_VALIDE"]):
        via, civico = stradario.normalizza_indirizzo(indirizzo)
        if ok and via and civico is not None:
            righe.append((chiave, via, civico, float(lat), float(lon)))
    if not righe:
        return {}
    df = pd.DataFrame(righe, columns=["CHIAVE", "VIA", "CIVICO", "LAT", "LON"])
    kx = 111_320 * math.cos(math.radians(float(df["LAT"].median())))

    def metri(lat1, lon1, lat2, lon2):
        return np.hypot((np.asarray(lat1) - lat2) * 110_540, (np.asarray(lon1) - lon2) * kx)

    esito = {}
    df = df.sort_values(["VIA", "CIVICO"], kind="stable")
    punti = df.groupby(["VIA", "CIVICO"], sort=True)[["LAT", "LON"]].median()
    for via, g_punti in punti.groupby(level=0, sort=False):
        if len(g_punti) < 4:
            continue
        civici = g_punti.index.get_level_values(1).to_numpy()
        plat = g_punti["LAT"].to_numpy()
        plon = g_punti["LON"].to_numpy()
        prese_via = df[df["VIA"] == via]
        per_civico = {c: (grp["CHIAVE"].to_numpy(), grp["LAT"].to_numpy(), grp["LON"].to_numpy())
                      for c, grp in prese_via.groupby("CIVICO", sort=False)}
        n = len(civici)
        for k in range(n):
            idx = [x for x in range(max(0, k - VICINI_PER_LATO), min(n, k + 1 + VICINI_PER_LATO)) if x != k]
            if len(idx) < 3:
                continue
            c_lat = float(np.median(plat[idx]))
            c_lon = float(np.median(plon[idx]))
            dispersione = float(np.median(metri(plat[idx], plon[idx], c_lat, c_lon)))
            soglia = max(DISTANZA_FUORI_VIA_M, FATTORE_DISPERSIONE_VIA * dispersione)
            chiavi, la, lo = per_civico[civici[k]]
            for chiave, d in zip(chiavi, metri(la, lo, c_lat, c_lon)):
                if d > soglia:
                    esito[chiave] = (int(round(d)), round(c_lat, 6), round(c_lon, 6))
    return esito

def distanze_da_via_osm(p: pd.DataFrame, comune: str) -> tuple[dict[str, float], set[str]]:
    """({chiave presa: distanza m dal tracciato OSM della sua via}, vie Neta
    abbinate a OSM). Solo prese con coordinate valide e via abbinata; se il
    file OSM manca o il comune non c'e', ({}, set())."""
    osm = vie_osm.vie_comune(comune)
    if not osm:
        return {}, set()
    v = p[p["COORD_VALIDE"]]
    vie = pd.Series([stradario.normalizza_indirizzo(i)[0] for i in v["INDIRIZZO"]], index=v.index)
    abbinate = vie_osm.abbina(sorted(set(vie) - {""}), list(osm))
    esito = {}
    for via, nome in abbinate.items():
        m = (vie == via).to_numpy()
        d = vie_osm.distanza_m(v["LAT"].to_numpy(dtype=float)[m], v["LON"].to_numpy(dtype=float)[m], osm[nome])
        esito.update(zip(v["CHIAVE"].to_numpy()[m], d))
    return esito, set(abbinate)

def coordinate_sbagliate_per_via(p: pd.DataFrame, comune: str) -> dict[str, tuple]:
    """{chiave: (motivo, distanza, lat proposta, lon proposta, fonte della
    proposta)} per le prese con la coordinata lontana dal suo indirizzo.
    Fonti in ordine: il civico ANNCSU (oltre DISTANZA_MAX_DA_CIVICO_M; se la
    presa e' vicina al suo civico e' a posto), il tracciato OSM della via
    (oltre DISTANZA_MAX_DA_VIA_OSM_M), i civici vicini della stessa via
    (fuori_dalla_via). Proposta: il civico ANNCSU, altrimenti il punto
    mediano dei civici vicini."""
    d_osm, abbinate = distanze_da_via_osm(p, comune)
    vicini = fuori_dalla_via(p)
    esito = {}
    kx = 111_320 * math.cos(math.radians(45.1))
    for chiave, indirizzo, ok, la, lo, cla, clo in zip(p["CHIAVE"], p["INDIRIZZO"], p["COORD_VALIDE"], p["LAT"], p["LON"],
                                                      p.get("CIV_LAT", pd.Series(np.nan, index=p.index)),
                                                      p.get("CIV_LON", pd.Series(np.nan, index=p.index))):
        if not ok:
            continue
        via = stradario.normalizza_indirizzo(indirizzo)[0]
        if pd.notna(cla):
            d = math.hypot((la - cla) * 110_540, (lo - clo) * kx)
            if d > DISTANZA_MAX_DA_CIVICO_M:
                esito[chiave] = ("Lontana dal suo civico (ANNCSU)", int(round(d)), round(cla, 6), round(clo, 6), "civico ANNCSU")
            continue
        prop = vicini.get(chiave, (None, None, None))[1:]
        fonte = "stima dai civici vicini" if prop[0] is not None else ""
        if via in abbinate:
            d = d_osm.get(chiave)
            if d is not None and d > DISTANZA_MAX_DA_VIA_OSM_M:
                esito[chiave] = ("Lontana dalla sua via (OpenStreetMap)", int(round(d)), *prop, fonte)
        elif chiave in vicini:
            esito[chiave] = ("Lontana dal resto della via", vicini[chiave][0], *prop, fonte)
    return esito

def interpolazione_lungo_via(p: pd.DataFrame, comune: str, chiavi: set[str]) -> dict[str, tuple[float, float, int]]:
    """{chiave: (lat, lon, distanza tra i due civici di riferimento)} per
    le prese in `chiavi` con via abbinata a OSM: il civico si colloca sul
    tracciato della via tra il civico piu' vicino sotto e quello sopra dallo
    stesso lato (pari/dispari), in proporzione al numero. Riferimenti: prese
    della stessa via con coordinata entro DISTANZA_RIFERIMENTO_DA_VIA_M dal
    tracciato, non segnaposto e non da correggere. Serve nei comuni senza i
    civici ANNCSU posizionati."""
    osm = vie_osm.vie_comune(comune)
    if not osm or not chiavi:
        return {}
    v = p[p["COORD_VALIDE"]]
    nv_tutte = [stradario.normalizza_indirizzo(i) for i in p["INDIRIZZO"]]
    vie_obiettivo = {nv_tutte[i][0] for i, k in enumerate(p["CHIAVE"]) if k in chiavi and nv_tutte[i][1] is not None}
    abbinate = vie_osm.abbina(sorted({x for x, _ in nv_tutte} - {""}), list(osm))
    escluse = chiavi | set(coordinate_condivise(p))
    esito = {}
    per_via_rif: dict[str, list] = {}
    for k, ind, la, lo in zip(v["CHIAVE"], v["INDIRIZZO"], v["LAT"], v["LON"]):
        vv, n = stradario.normalizza_indirizzo(ind)
        if vv in vie_obiettivo and n is not None and k not in escluse:
            per_via_rif.setdefault(vv, []).append((n, float(la), float(lo)))
    per_via_obiettivo: dict[str, list] = {}
    for i, k in enumerate(p["CHIAVE"]):
        vv, n = nv_tutte[i]
        if k in chiavi and n is not None:
            per_via_obiettivo.setdefault(vv, []).append((k, n))
    for via in vie_obiettivo & set(abbinate):
        tratti = osm[abbinate[via]]
        # Riferimenti: (civico, tratto, posizione lungo il tratto)
        rif: dict[int, list[tuple[int, float]]] = {}
        for n, la, lo in per_via_rif.get(via, []):
            pr = vie_osm.proietta(la, lo, tratti)
            if pr and pr[2] <= DISTANZA_RIFERIMENTO_DA_VIA_M:
                rif.setdefault(n, []).append((pr[0], pr[1]))
        punti = {n: (l[0][0], float(np.median([x for _, x in l]))) for n, l in rif.items()
                 if len({tr for tr, _ in l}) == 1}
        for k, n in per_via_obiettivo.get(via, []):
            sotto = [c for c in punti if c < n and c % 2 == n % 2]
            sopra = [c for c in punti if c > n and c % 2 == n % 2]
            if not sotto or not sopra:
                continue
            c1, c2 = max(sotto), min(sopra)
            (t1, s1), (t2, s2) = punti[c1], punti[c2]
            gap = abs(s2 - s1)
            if t1 != t2 or gap > GAP_INTERPOLAZIONE_MAX_M:
                continue
            la, lo = vie_osm.punto_lungo(tratti[t1], s1 + (s2 - s1) * (n - c1) / (c2 - c1))
            esito[k] = (round(la, 6), round(lo, 6), int(round(gap)))
    return esito

def _affidabilita_proposte(p: pd.DataFrame, comune: str, righe: np.ndarray, lat_c: pd.Series, lon_c: pd.Series,
                           fonte_c: pd.Series, base: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Controlli incrociati sulle coordinate proposte: nel comune giusto
    (confini ISTAT), entro DISTANZA_PROPOSTA_DA_VIA_OSM_M dalla sua via in
    OSM, nel distretto delle altre fonti dell'indirizzo (stradario, via OSM).
    Ogni controllo fallito abbassa di un livello (alta -> media -> bassa).
    Restituisce (affidabilita', nota con fonte ed esito dei controlli).
    Tutto a blocchi: una proposta alla volta erano ~2 minuti per i 22 comuni."""
    livelli = ["bassa", "media", "alta"]
    aff = pd.Series("", index=p.index)
    nota = pd.Series("", index=p.index)
    if not len(righe):
        return aff, nota
    la = lat_c.to_numpy(dtype=float)[righe]
    lo = lon_c.to_numpy(dtype=float)[righe]
    controlli = [[] for _ in righe]
    falliti = np.zeros(len(righe), dtype=int)

    if _poligoni_comuni():
        dove = _comune_della_posizione(la, lo)
        for k, d in enumerate(dove):
            ok = _nome_comune(d.split(" (")[0]) == _nome_comune(comune)
            controlli[k].append(f"comune {'✓' if ok else '✗ (' + (d or 'fuori') + ')'}")
            falliti[k] += not ok

    osm = vie_osm.vie_comune(comune)
    if osm:
        vie = [stradario.normalizza_indirizzo(p["INDIRIZZO"].iloc[i])[0] for i in righe]
        abbinate = vie_osm.abbina(sorted({stradario.normalizza_indirizzo(x)[0] for x in p["INDIRIZZO"]} - {""}), list(osm))
        for via in set(vie) & set(abbinate):
            ks = np.array([k for k, v in enumerate(vie) if v == via])
            dist = vie_osm.distanza_m(la[ks], lo[ks], osm[abbinate[via]])
            for k, d in zip(ks, dist):
                ok = d <= DISTANZA_PROPOSTA_DA_VIA_OSM_M
                controlli[k].append(f"via OSM {'✓' if ok else '✗'} ({round(d)} m)")
                falliti[k] += not ok

    d_punto = _distretti_sicuri(la, lo)
    for k, i in enumerate(righe):
        altre = {x for x in (p["D_STRADARIO"].iloc[i], p["D_OSM"].iloc[i]) if x}
        if altre:
            ok = not d_punto[k] or d_punto[k] in altre
            controlli[k].append(f"distretto {'✓' if ok else '✗ (' + d_punto[k] + ')'}")
            falliti[k] += not ok
        aff.iloc[i] = livelli[max(0, livelli.index(base.iloc[i]) - falliti[k])]
        nota.iloc[i] = f"{fonte_c.iloc[i]}; controlli: {', '.join(controlli[k]) or 'nessuno disponibile'}"
    return aff, nota

def coordinate_condivise(p: pd.DataFrame) -> dict[str, int]:
    """{chiave presa: numero di vie} per le prese la cui coordinata (al
    metro) e' usata anche da prese di altre vie: una coordinata segnaposto,
    non la posizione vera (vedi MIN_VIE_COORDINATA_CONDIVISA)."""
    v = p[p["COORD_VALIDE"]]
    if v.empty:
        return {}
    vie = [stradario.normalizza_indirizzo(i)[0] for i in v["INDIRIZZO"]]
    punto = list(zip(v["LAT"].round(5), v["LON"].round(5)))
    n_vie = pd.Series(vie).groupby(pd.Series(punto)).nunique()
    return {k: int(n_vie[pt]) for k, pt in zip(v["CHIAVE"], punto) if n_vie[pt] >= MIN_VIE_COORDINATA_CONDIVISA}

def distretti_del_comune(comune: str) -> set[str]:
    """Codici dei distretti del comune (ufficiali o associabili)."""
    comune = comune.strip().upper()
    elenco = motore_calcolo.carica_mappa_distretti_df()
    return {
        r.codice_distretto.upper() for r in elenco.itertuples(index=False)
        if r.comune_ufficiale.strip().upper() == comune
        or comune in [c.strip().upper() for c in str(r.comuni_associabili).split(";")]
    }

def _scala(valore: float, cifre_intere: int) -> float:
    """8742620 -> 8.742620 (cifre_intere=1), 4531012 -> 45.31012 (2): la
    virgola persa nell'estrazione."""
    intere = len(str(int(abs(valore))))
    return valore / 10 ** (intere - cifre_intere)

def _coordinata_non_valida(lat, lon, comune: str) -> tuple[str, tuple[float, float] | None]:
    """Descrive una coordinata fuori dall'area valida e, se e' un errore di
    formato riconoscibile (virgola persa, latitudine e longitudine
    invertite), propone quella corretta dicendo dove cadrebbe (Daniele,
    25/09/2026: a settembre 2026 824 mancanti, 9 senza virgola, 1 invertita,
    le altre in altre regioni)."""
    if pd.isna(lat) or pd.isna(lon):
        return "Coordinate mancanti", None
    lat, lon = float(lat), float(lon)
    if lat == 0 and lon == 0:
        return "Coordinate a 0,0", None
    candidati = []
    if abs(lat) > 1000 or abs(lon) > 1000:
        candidati.append(("Virgola mancante", (_scala(lat, 2) if abs(lat) > 1000 else lat, _scala(lon, 1) if abs(lon) > 1000 else lon)))
    candidati.append(("Latitudine e longitudine invertite", (lon, lat)))
    for motivo, (la, lo) in candidati:
        if LAT_VALIDA[0] <= la <= LAT_VALIDA[1] and LON_VALIDA[0] <= lo <= LON_VALIDA[1]:
            dove = _comune_della_posizione(np.array([la]), np.array([lo]))[0]
            if _nome_comune(dove.split(" (")[0]) == _nome_comune(comune):
                return f"{motivo}: corretta cadrebbe nel comune", (round(la, 6), round(lo, 6))
            return f"{motivo}: corretta cadrebbe in {dove or 'un comune lontano'}", (round(la, 6), round(lo, 6))
    return f"Coordinate fuori provincia ({lat:.4f}, {lon:.4f})", None

_CACHE_COORDINATE: dict = {"versione": None, "dati": {}}

_LOCK_COORDINATE = threading.Lock()

def coordinate_da_verificare(comune: str) -> pd.DataFrame:
    """Come _coordinate_da_verificare, in memoria finche' i dati non cambiano
    (circa un minuto per i 22 comuni: si calcola in background dopo ogni
    caricamento, poi Excel e invii sono immediati)."""
    versione = _versione_dati()
    with _LOCK_COORDINATE:
        if _CACHE_COORDINATE["versione"] != versione:
            _CACHE_COORDINATE.update(versione=versione, dati={})
        if comune in _CACHE_COORDINATE["dati"]:
            return _CACHE_COORDINATE["dati"][comune].copy()
    risultato = cache_disco.carica(f"coordinate_{comune}", (comune, versione))
    if risultato is None:
        risultato = _coordinate_da_verificare(comune)
        cache_disco.salva(f"coordinate_{comune}", (comune, versione), risultato)
    with _LOCK_COORDINATE:
        if _CACHE_COORDINATE["versione"] == versione:
            _CACHE_COORDINATE["dati"][comune] = risultato
    return risultato.copy()

def _coordinate_da_verificare(comune: str) -> pd.DataFrame:
    """Prese del comune con coordinate da far verificare a Neta (Daniele,
    25/09/2026), ricalcolate a ogni estrazione indipendentemente dalle
    conferme del distretto: una presa resta qui finche' Neta non corregge
    la coordinata. PROBLEMA: mancanti o fuori provincia; dentro un distretto
    di un altro comune; a piu' di DISTANZA_FUORI_COMUNE_M dai distretti del
    comune; coordinata segnaposto (stesso punto per piu' vie); lontana dal
    resto della sua via (vedi fuori_dalla_via, con la coordinata mediana dei
    civici vicini come proposta). Se ci sono i confini
    ISTAT del comune, "fuori comune" viene da quelli e sostituisce le due
    regole sui distretti (dentro un distretto di altro comune, lontana dai
    distretti del comune). La regola "lontana dal distretto della sua
    via" e' stata tolta: sulle strade lunghe (Via Emilia, Via Piacenza,
    frazioni) segnalava le case sparse lungo la stessa via (~970 falsi
    allarmi con lo stradario di tutti i comuni)."""
    p = prese_comune(comune)
    if p.empty:
        return p
    problema = pd.Series("", index=p.index)
    distanza = pd.Series(np.nan, index=p.index)
    lat_c = pd.Series(np.nan, index=p.index)
    lon_c = pd.Series(np.nan, index=p.index)
    fonte_c = pd.Series("", index=p.index)
    for i in np.nonzero(~p["COORD_VALIDE"].to_numpy())[0]:
        testo, corretta = _coordinata_non_valida(p["LAT"].iloc[i], p["LON"].iloc[i], comune)
        problema.iloc[i] = testo
        if corretta:
            lat_c.iloc[i], lon_c.iloc[i] = corretta
            fonte_c.iloc[i] = "correzione del formato"

    # Con i confini ISTAT: fuori dal territorio comunale (oltre la
    # tolleranza dei confini generalizzati) al posto della distanza dai
    # distretti, che segnalava anche case sparse legittime in campagna.
    v_tutti = np.nonzero(p["COORD_VALIDE"].to_numpy())[0]
    istat = None
    if len(v_tutti):
        lat_v = p["LAT"].to_numpy(dtype=float)[v_tutti]
        lon_v = p["LON"].to_numpy(dtype=float)[v_tutti]
        dist_comune = _distanza_dal_comune_m(lat_v, lon_v, comune)
        if dist_comune is not None:
            istat = True
            dove = _comune_della_posizione(lat_v, lon_v)
            for k, i in enumerate(v_tutti):
                if _nome_comune(dove[k].split(" (")[0]) != _nome_comune(comune) and dist_comune[k] > TOLLERANZA_CONFINE_COMUNE_M:
                    problema.iloc[i] = f"Fuori dal comune: cade in {dove[k] or 'un comune lontano'}"
                    distanza.iloc[i] = round(dist_comune[k])

    condivise = coordinate_condivise(p)
    fuori = coordinate_sbagliate_per_via(p, comune)
    for i, chiave in enumerate(p["CHIAVE"]):
        if problema.iloc[i]:
            continue
        if chiave in condivise:
            problema.iloc[i] = f"Coordinata segnaposto: stesso punto per prese di {condivise[chiave]} vie diverse"
        elif chiave in fuori:
            motivo, d, la, lo, fonte = fuori[chiave]
            problema.iloc[i] = motivo
            distanza.iloc[i] = d
            if la is not None:
                lat_c.iloc[i], lon_c.iloc[i], fonte_c.iloc[i] = la, lo, fonte

    # Coordinata in un distretto diverso da quello della sua via, oltre la
    # tolleranza dal confine: con "prima l'indirizzo" il distretto non si
    # segnala, ma la coordinata va corretta.
    dv_col = p["DISTRETTO_VIA"].to_numpy()
    j = np.nonzero((problema == "").to_numpy() & p["COORD_VALIDE"].to_numpy() & (dv_col != ""))[0]
    if len(j):
        la = p["LAT"].to_numpy(dtype=float)[j]
        lo = p["LON"].to_numpy(dtype=float)[j]
        dove = np.array(_dentro_confini(la, lo), dtype=object)
        for dv in set(dv_col[j]):
            k = np.nonzero((dv_col[j] == dv) & (dove != "") & (dove != dv))[0]
            if not len(k):
                continue
            dentro, dist = _dentro_e_distanza(la[k], lo[k], dv)
            for kk, d_in, d in zip(k, dentro, dist):
                if not d_in and d > TOLLERANZA_BORDO_M:
                    problema.iloc[j[kk]] = f"Cade in {dove[kk]}, ma l'indirizzo e' {dv} ({p['FONTE_INDIRIZZO'].iloc[j[kk]]})"
                    distanza.iloc[j[kk]] = round(d)

    propri = distretti_del_comune(comune) & {c for c, _, _ in _poligoni()}
    elenco = motore_calcolo.carica_mappa_distretti_df().set_index("codice_distretto")["comune_ufficiale"]
    v = np.nonzero(p["COORD_VALIDE"].to_numpy())[0]
    if len(v) and propri:
        lat = p["LAT"].to_numpy(dtype=float)[v]
        lon = p["LON"].to_numpy(dtype=float)[v]
        pos = np.array(_dentro_confini(lat, lon), dtype=object)
        minima = np.full(len(v), np.inf)
        for codice in propri:
            dentro, dist = _dentro_e_distanza(lat, lon, codice)
            minima = np.minimum(minima, np.where(dentro, 0, dist))
        for k, i in enumerate(v):
            if istat:
                break  # fuori comune gia' deciso dai confini ISTAT
            if pos[k] and pos[k] not in propri:
                problema.iloc[i] = f"Dentro un distretto di un altro comune ({pos[k]}, {elenco.get(pos[k], '') or '?'})"
                distanza.iloc[i] = round(minima[k])
            elif minima[k] > (DISTANZA_FUORI_COMUNE_NODMA_M if p["MOTIVO"].iloc[i] == "NODMA" else DISTANZA_FUORI_COMUNE_M):
                problema.iloc[i] = "Lontana dai distretti del comune"
                distanza.iloc[i] = round(minima[k])

    # Indirizzo che ANNCSU non conosce: la via c'e' ma il civico no (civico
    # sbagliato in Neta, o non ancora registrato dal Comune).
    for i, esiste in enumerate(p["CIVICO_ESISTE"]):
        if not problema.iloc[i] and esiste is False:
            problema.iloc[i] = "Civico non presente in ANNCSU (indirizzo da verificare)"

    # Coordinata proposta, dalla fonte piu' affidabile (Daniele, 25-26/09/2026:
    # ogni proposta con la sua fonte, e solo se la stima e' affidabile):
    # 1. civico ANNCSU: alta (metodi 1-4, rilievo o cartografia), media
    #    (metodo 5, dal Portale per i Comuni, senza accuratezza dichiarata);
    # 2. correzione del formato (virgola persa, lat/lon invertite): alta;
    # 3. interpolazione lungo la via OSM tra due civici: media se i civici
    #    distano al massimo GAP_INTERPOLAZIONE_MEDIA_M, altrimenti bassa;
    # 4. stima dai civici vicini: bassa.
    # Poi i controlli incrociati abbassano il livello; le proposte "bassa"
    # non si danno a Neta: "da rilevare sul posto".
    base = pd.Series("", index=p.index)
    civ = p["CIV_LAT"].notna() & (problema != "")
    lat_c[civ] = p.loc[civ, "CIV_LAT"].round(6)
    lon_c[civ] = p.loc[civ, "CIV_LON"].round(6)
    fonte_c[civ] = [
        "civico ANNCSU (" + {"1": "rilievo sul campo, < 5 m", "2": "rilievo sul campo, >= 5 m", "3": "cartografia, < 5 m",
                             "4": "cartografia, >= 5 m", "5": "Portale per i Comuni"}.get(str(m), "metodo non indicato") + ")"
        for m in p.loc[civ, "CIV_METODO"]
    ]
    base[civ] = ["alta" if str(m) in ("1", "2", "3", "4") else "media" for m in p.loc[civ, "CIV_METODO"]]
    base[(fonte_c == "correzione del formato") & ~civ] = "alta"
    da_stimare = {k for k, pr, la in zip(p["CHIAVE"], problema, lat_c) if pr and pd.isna(la)} | \
                 {k for k, pr, f in zip(p["CHIAVE"], problema, fonte_c) if pr and f == "stima dai civici vicini"}
    interp = interpolazione_lungo_via(p, comune, da_stimare)
    for i, k in enumerate(p["CHIAVE"]):
        if k in interp and not civ.iloc[i]:
            la, lo, gap = interp[k]
            lat_c.iloc[i], lon_c.iloc[i] = la, lo
            fonte_c.iloc[i] = f"interpolazione lungo la via (OSM), civici di riferimento a {gap} m"
            base.iloc[i] = "media" if gap <= GAP_INTERPOLAZIONE_MEDIA_M else "bassa"
    base[(fonte_c == "stima dai civici vicini") & (base == "")] = "bassa"

    righe = np.nonzero((problema != "").to_numpy() & lat_c.notna().to_numpy())[0]
    affidabilita, nota = _affidabilita_proposte(p, comune, righe, lat_c, lon_c, fonte_c, base)
    bassa = affidabilita == "bassa"
    lat_c[bassa] = np.nan
    lon_c[bassa] = np.nan
    nota[bassa] = "stima non affidabile, da rilevare sul posto — " + nota[bassa]
    nota[(problema != "") & (affidabilita == "")] = "nessuna stima possibile, da rilevare sul posto"

    p = p.assign(PROBLEMA=problema, DISTANZA_M=distanza, LAT_CORRETTA=lat_c, LON_CORRETTA=lon_c,
                 FONTE_COORDINATA=fonte_c, AFFIDABILITA=affidabilita, NOTA_PROPOSTA=nota)
    return p[p["PROBLEMA"] != ""].reset_index(drop=True)

def esporta_coordinate_excel(comuni: list[str]) -> bytes:
    """File per Neta con le prese da ricontrollare sul posto / in mappa."""
    parti = [c.assign(COMUNE=comune) for comune in comuni if not (c := coordinate_da_verificare(comune)).empty]
    colonne = {
        "COMUNE": "Comune", "DP": "Presa (DP)", "INDIRIZZO": "Indirizzo", "CAP": "CAP",
        "SERVIZI": "Codici servizio", "N_SERVIZI": "N. servizi", "DISTRETTO": "Distretto attuale",
        "PROBLEMA": "Problema", "DISTANZA_M": "Distanza (m)", "LAT": "Latitudine", "LON": "Longitudine",
        "LAT_CORRETTA": "Latitudine corretta (proposta)", "LON_CORRETTA": "Longitudine corretta (proposta)",
        "AFFIDABILITA": "Affidabilita' della proposta", "NOTA_PROPOSTA": "Fonte e controlli della proposta",
    }
    if parti:
        df = pd.concat(parti, ignore_index=True)
        df["DP"] = np.where(df["DP"] == "", "(servizio senza presa)", df["DP"])
        df = df[list(colonne)].rename(columns=colonne)
    else:
        df = pd.DataFrame(columns=list(colonne.values()))
    return _excel(df, "Coordinate", LEGENDA_COORDINATE)

LEGENDA_COORDINATE = [
    ("Cosa contiene", "Prese con la coordinata da verificare. Una riga per presa, i codici servizio nella stessa cella. "
                      "Ricalcolato a ogni estrazione: una presa resta finche' la coordinata non viene corretta."),
    ("Problema", "Coordinate mancanti, a 0,0 o fuori provincia; virgola mancante o latitudine/longitudine invertite; "
                 "fuori dal comune (confini ISTAT); coordinata segnaposto (stesso punto per prese di 3 o piu' vie); "
                 "lontana dal suo civico ANNCSU (oltre 150 m); lontana dalla sua via in OpenStreetMap (oltre 150 m) "
                 "o dal resto della via; in un altro distretto rispetto all'indirizzo; civico non presente in ANNCSU."),
    ("Distanza (m)", "Quanto la coordinata attuale dista dal riferimento del problema (civico, via, confine del comune)."),
    ("Latitudine / Longitudine", "La coordinata attuale, quella da correggere."),
    ("Latitudine / Longitudine corretta (proposta)", "La coordinata da inserire, solo se la stima e' affidabile (alta o media)."),
    ("Affidabilita' - alta", "Civico ANNCSU posizionato dal Comune (rilievo o cartografia) oppure correzione del formato, "
                             "e tutti i controlli incrociati superati."),
    ("Affidabilita' - media", "Civico ANNCSU inserito dal Portale per i Comuni, oppure interpolazione lungo la via tra due "
                              "civici vicini (entro 100 m), oppure una fonte 'alta' con un controllo non superato."),
    ("Affidabilita' - bassa", "Stima non affidabile: nessuna coordinata proposta, da rilevare sul posto."),
    ("Fonte e controlli della proposta", "Da dove viene la proposta e l'esito dei controlli: comune giusto (ISTAT), "
                                         "vicina alla sua via in OpenStreetMap (entro 60 m), nel distretto indicato "
                                         "dalle altre fonti. ✓ superato, ✗ non superato."),
    ("Fonti", "ANNCSU - Agenzia delle Entrate e ISTAT (CC-BY 4.0); confini ISTAT; (c) OpenStreetMap contributors (ODbL)."),
]

def _excel(df: pd.DataFrame, foglio_nome: str, legenda: list[tuple[str, str]] | None = None) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=foglio_nome)
        foglio = writer.sheets[foglio_nome]
        for i, col in enumerate(df.columns, start=1):
            larghezza = min(60, max(10, len(col) + 2, *(len(str(v)) + 2 for v in df[col].head(500))))
            foglio.column_dimensions[foglio.cell(row=1, column=i).column_letter].width = larghezza
        foglio.freeze_panes = "A2"
        foglio.auto_filter.ref = foglio.dimensions
        if legenda:
            pd.DataFrame(legenda, columns=["Voce", "Spiegazione"]).to_excel(writer, index=False, sheet_name="Legenda")
            fl = writer.sheets["Legenda"]
            fl.column_dimensions["A"].width = 42
            fl.column_dimensions["B"].width = 120
    return buffer.getvalue()
