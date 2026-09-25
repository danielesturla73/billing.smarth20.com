"""
Stradario via -> distretto, per controllare le prese dall'indirizzo
(richiesto da Daniele il 25/09/2026).

Non esiste uno stradario ufficiale dei distretti: lo si ricava UNA TANTUM
per comune dalle prese stesse. Ogni presa ha indirizzo con civico e
coordinate; il distretto di una via e' quello in cui cade la maggior parte
delle sue prese (dentro i confini disegnati). Per le vie divise tra due
distretti si cerca il civico in cui cambia (tutti i civici insieme, oppure
pari e dispari separati), con un voto per civico; i palazzi con piu'
prese tutte in un altro distretto diventano eccezioni della via. Se non
c'e' un taglio netto la via resta "a cavallo" (distretto vuoto) e non si
controlla.

Il risultato va in project_docs/stradario_distretti.csv, rivedibile e
correggibile a mano come distretti_comuni.csv: una volta generato per un
comune non si rigenera da solo (le correzioni a mano andrebbero perse).
Il tab Prese lo usa per segnalare le prese la cui via appartiene a un altro
distretto: trova anche le prese con coordinate sbagliate, che il controllo
sulla posizione non puo' vedere.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

PERCORSO_STRADARIO = Path("project_docs/stradario_distretti.csv")
COLONNE = ["comune", "via", "civici", "civico_da", "civico_a", "distretto", "n_prese", "n_discordanti", "note"]

# Si ragiona per CIVICO (un voto per civico, il distretto dove cade la
# maggior parte delle sue prese), non per presa: un condominio con 18 prese
# pesava come 18 case e rendeva "a cavallo" vie compatte (Via Trieste 60,
# Via Molino 24 a Belgioioso; Daniele, 25/09/2026).
# Quota massima di civici "fuori posto" accettata per dire che una via (o un
# suo tratto) sta in un distretto: oltre, si prova a dividerla per civico.
QUOTA_DISCORDANTI_MAX = 0.10
# Un tratto di via con meno civici di cosi' non basta a dire dove cambia
# distretto (es. il solo civico 1 di Via Spinelli a Belgioioso).
MIN_CIVICI_TRATTO = 4
# Un civico con almeno tante prese, quasi tutte (QUOTA_ECCEZIONE) in un
# altro distretto, diventa un'eccezione della via (un palazzo al di la' del
# confine), da rivedere; un civico con una presa sola fuori posto resta
# invece un possibile errore e viene segnalato.
MIN_PRESE_ECCEZIONE = 2
QUOTA_ECCEZIONE = 0.8


def normalizza_indirizzo(indirizzo) -> tuple[str, int | None]:
    """'VIA F.LLI STRAMBIO, 124' -> ('VIA F.LLI STRAMBIO', 124). Il civico e'
    il primo numero dopo la virgola (124/A -> 124); None se manca."""
    testo = "" if indirizzo is None or (isinstance(indirizzo, float) and np.isnan(indirizzo)) else str(indirizzo)
    via, _, resto = testo.partition(",")
    via = re.sub(r"\s+", " ", via).strip().upper()
    numero = re.match(r"\s*(\d+)", resto)
    return via, int(numero.group(1)) if numero else None


def _maggioranza(distretti: list[str]) -> tuple[str, int]:
    """(distretto piu' frequente, quante prese NON sono in quello)."""
    valori, conteggi = np.unique(distretti, return_counts=True)
    i = int(conteggi.argmax())
    return str(valori[i]), int(len(distretti) - conteggi[i])


def _taglio(civici: list[int], distretti: list[str]) -> list[tuple[int | None, int | None, str]]:
    """Miglior divisione in al piu' due tratti consecutivi (un voto per
    civico). Restituisce [(da, a, distretto)]; da/a None = estremo aperto."""
    ordine = np.argsort(civici, kind="stable")
    c = [civici[i] for i in ordine]
    d = [distretti[i] for i in ordine]
    unico, err = _maggioranza(d)
    migliore = (err, [(None, None, unico)])
    for k in range(MIN_CIVICI_TRATTO, len(c) - MIN_CIVICI_TRATTO + 1):
        d1, e1 = _maggioranza(d[:k])
        d2, e2 = _maggioranza(d[k:])
        if d1 != d2 and e1 + e2 < migliore[0]:
            migliore = (e1 + e2, [(None, c[k - 1], d1), (c[k - 1] + 1, None, d2)])
    return migliore[1]


def _nel_tratto(civici: str, da, a, civico: int | None) -> bool:
    if civici == "tutti" and da is None and a is None:
        return True
    if civico is None:
        return False
    if civici == "pari" and civico % 2 or civici == "dispari" and not civico % 2:
        return False
    return (da is None or civico >= da) and (a is None or civico <= a)


def genera_stradario(prese_comune: pd.DataFrame, distretti_da_posizione: list[str], comune: str) -> pd.DataFrame:
    """Stradario del comune dalle prese (DataFrame di prese.prese_comune) e
    dal distretto in cui ciascuna cade ('' se fuori da ogni confine o senza
    coordinate valide: non contano)."""
    righe = []
    vie: dict[str, list[tuple[int | None, str]]] = {}
    for indirizzo, pos in zip(prese_comune["INDIRIZZO"], distretti_da_posizione):
        via, civico = normalizza_indirizzo(indirizzo)
        if via and pos:
            vie.setdefault(via, []).append((civico, pos))

    for via, prese in sorted(vie.items()):
        # Voti: uno per civico; le prese senza civico votano una per una.
        per_civico: dict[int, list[str]] = {}
        voti = []  # (civico, distretto, n_prese, solido)
        for civico, pos in prese:
            if civico is None:
                voti.append((None, pos, 1, False))
            else:
                per_civico.setdefault(civico, []).append(pos)
        for civico, dd in sorted(per_civico.items()):
            distretto, fuori = _maggioranza(dd)
            solido = len(dd) >= MIN_PRESE_ECCEZIONE and (len(dd) - fuori) / len(dd) >= QUOTA_ECCEZIONE
            voti.append((civico, distretto, len(dd), solido))

        # Modelli, dal piu' semplice: via intera, un taglio per civico,
        # pari e dispari separati (ciascuno intero o con un taglio).
        con_civico = [v for v in voti if v[0] is not None]
        modelli = [[("tutti", None, None, _maggioranza([v[1] for v in voti])[0])]]
        if len(con_civico) >= 2 * MIN_CIVICI_TRATTO:
            modelli.append([("tutti", da, a, d) for da, a, d in _taglio([v[0] for v in con_civico], [v[1] for v in con_civico])])
        pd_modello = []
        for nome, resto in (("pari", 0), ("dispari", 1)):
            parte = [v for v in con_civico if v[0] % 2 == resto]
            if parte:
                pd_modello += [(nome, da, a, d) for da, a, d in _taglio([v[0] for v in parte], [v[1] for v in parte])]
        if pd_modello:
            modelli.append(pd_modello)

        scelto = None
        for tratti in modelli:
            residui, eccezioni = [], []
            for v in voti:
                atteso = next((d for civ, da, a, d in tratti if _nel_tratto(civ, da, a, v[0])), "")
                if v[1] != atteso:
                    (eccezioni if v[3] else residui).append(v)
            # Almeno un civico fuori posto si tollera sempre (e' proprio
            # quello da segnalare), purche' la via ne abbia almeno 3.
            tollerati = max(1, int(QUOTA_DISCORDANTI_MAX * len(voti))) if len(voti) >= 3 else 0
            ok = len(residui) <= tollerati and len(eccezioni) <= max(2, int(0.2 * len(voti)))
            if ok and (scelto is None or len(residui) + len(eccezioni) < scelto[0]):
                scelto = (len(residui) + len(eccezioni), tratti, residui, eccezioni)

        if scelto is None:
            conteggi = pd.Series([p for _, p in prese]).value_counts()
            dettaglio = ", ".join(f"{k} {v}" for k, v in conteggi.items())
            righe.append((via, "tutti", None, None, "", len(prese), 0, f"a cavallo, non controllata ({dettaglio})"))
            continue
        _, tratti, residui, eccezioni = scelto
        divisa = len(tratti) > 1
        for civ, da, a, d in tratti:
            nel = [v for v in voti if _nel_tratto(civ, da, a, v[0]) and v not in eccezioni]
            n_fuori = sum(v[2] for v in residui if v in nel)
            righe.append((via, civ, da, a, d, sum(v[2] for v in nel), n_fuori, "via divisa tra distretti" if divisa else ""))
        for civico, d, n, _ in eccezioni:
            righe.append((via, "tutti", civico, civico, d, n, 0, f"eccezione: {n} prese al civico {civico}, da verificare"))

    df = pd.DataFrame(righe, columns=COLONNE[1:])
    df.insert(0, "comune", comune.strip().upper())
    return df


def carica(comune: str | None = None) -> pd.DataFrame:
    if not PERCORSO_STRADARIO.exists():
        return pd.DataFrame(columns=COLONNE)
    df = pd.read_csv(PERCORSO_STRADARIO, dtype=str, keep_default_na=False)
    for col in ("civico_da", "civico_a", "n_prese", "n_discordanti"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["via"] = df["via"].map(lambda v: normalizza_indirizzo(v)[0])
    if comune:
        df = df[df["comune"].str.strip().str.upper() == comune.strip().upper()]
    return df.reset_index(drop=True)


def salva(comune: str, nuovo: pd.DataFrame) -> None:
    """Sostituisce le righe del comune nel CSV, lasciando gli altri comuni."""
    tutto = carica()
    tutto = tutto[tutto["comune"].str.strip().str.upper() != comune.strip().upper()]
    tutto = pd.concat([d for d in (tutto, nuovo[COLONNE]) if not d.empty], ignore_index=True) if not (tutto.empty and nuovo.empty) else nuovo[COLONNE].copy()
    tutto = tutto.sort_values(["comune", "via", "civici", "civico_da"], na_position="first")
    for col in ("civico_da", "civico_a", "n_prese", "n_discordanti"):
        tutto[col] = tutto[col].map(lambda v: "" if pd.isna(v) else str(int(v)))
    PERCORSO_STRADARIO.parent.mkdir(parents=True, exist_ok=True)
    tutto.to_csv(PERCORSO_STRADARIO, index=False)


def distretti_da_via(stradario: pd.DataFrame, indirizzi) -> list[str]:
    """Per ogni indirizzo il distretto della via (o del suo tratto di civici,
    le eccezioni di un singolo civico prima di tutto); '' se la via non c'e',
    e' a cavallo, o il civico serve ma manca."""
    per_via: dict[str, list] = {}
    for r in stradario.itertuples(index=False):
        da = None if pd.isna(r.civico_da) else int(r.civico_da)
        a = None if pd.isna(r.civico_a) else int(r.civico_a)
        singolo = da is not None and da == a
        per_via.setdefault(r.via, []).append((not singolo, r.civici, da, a, r.distretto))
    for righe in per_via.values():
        righe.sort(key=lambda x: x[0])
    esito = []
    for indirizzo in indirizzi:
        via, civico = normalizza_indirizzo(indirizzo)
        esito.append(next((d for _, civ, da, a, d in per_via.get(via, []) if _nel_tratto(civ, da, a, civico)), ""))
    return esito
