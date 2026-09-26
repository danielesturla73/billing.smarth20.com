"""
Risultati pesanti salvati su disco (archivio/cache/), per non ricalcolare
tutto a ogni riavvio del container (Daniele, 26/09/2026: dopo un deploy l'app
restava lenta 1-2 minuti mentre ricalcolava i 22 comuni).

Ogni risultato si salva con una chiave che descrive tutto cio' da cui
dipende (dati e file di riferimento) piu' una firma del CODICE dell'app: se
cambia anche solo una di queste cose, il file su disco non vale piu' e si
ricalcola. Cosi' un risultato salvato non puo' mai essere piu' vecchio dei
dati o delle regole di calcolo. I file si possono cancellare in qualunque
momento: si rifanno da soli.
"""
from __future__ import annotations

import hashlib
import os
import pickle
from pathlib import Path

CARTELLA = Path("archivio") / "cache"
_FIRMA_CODICE: str | None = None


def firma_codice() -> str:
    """Firma di tutti i file .py dell'app: un deploy con codice diverso
    invalida tutti i risultati salvati."""
    global _FIRMA_CODICE
    if _FIRMA_CODICE is None:
        h = hashlib.sha256()
        for p in sorted(Path(__file__).parent.glob("*.py")):
            h.update(p.name.encode())
            h.update(p.read_bytes())
        _FIRMA_CODICE = h.hexdigest()[:16]
    return _FIRMA_CODICE


def _nome_file(nome: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in nome)


def _percorso(nome: str, chiave: tuple) -> Path:
    impronta = hashlib.sha256(repr((firma_codice(), nome, chiave)).encode()).hexdigest()[:24]
    return CARTELLA / f"{_nome_file(nome)}__{impronta}.pkl"


def carica(nome: str, chiave: tuple):
    """L'oggetto salvato per (nome, chiave), o None se non c'e' o non vale."""
    percorso = _percorso(nome, chiave)
    try:
        with open(percorso, "rb") as f:
            chiave_salvata, oggetto = pickle.load(f)
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError, ValueError):
        return None
    return oggetto if chiave_salvata == (firma_codice(), nome, chiave) else None


def salva(nome: str, chiave: tuple, oggetto) -> None:
    """Scrittura atomica (file temporaneo + rinomina); gli errori di disco
    non devono mai far fallire il calcolo."""
    try:
        CARTELLA.mkdir(parents=True, exist_ok=True)
        percorso = _percorso(nome, chiave)
        temporaneo = percorso.with_suffix(f".tmp{os.getpid()}")
        with open(temporaneo, "wb") as f:
            pickle.dump(((firma_codice(), nome, chiave), oggetto), f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temporaneo, percorso)
    except OSError:
        return
    pulisci(nome, tieni=percorso)


def pulisci(nome: str, tieni: Path | None = None) -> None:
    """Toglie i file vecchi di un tipo (quelli di chiavi superate)."""
    for p in CARTELLA.glob(f"{_nome_file(nome)}__{'?' * 24}.pkl"):
        if p != tieni:
            try:
                p.unlink()
            except OSError:
                pass
