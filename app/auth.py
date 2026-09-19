"""
Autenticazione, ruoli e registro delle azioni (deciso con Daniele il
19/09/2026).

- Ruoli a scalare: viewer < editor < admin. Viewer consulta; editor carica
  estrazioni, modifica distretti/mappa (e in futuro invia a WMS); admin
  gestisce gli utenti e vede il registro. Un account per persona, cosi' il
  registro dice CHI ha fatto cosa.
- Tutto imposto lato server (vedi il middleware in main.py): nascondere un
  pulsante nella pagina non protegge niente. Gli utenti WMS SmartH2O sono
  separati di proposito (le due app non condividono database).
- Le password non vengono mai salvate ne' si possono recuperare: solo un
  hash scrypt (libreria standard, nessuna dipendenza in piu'). Se si
  dimentica, si REIMPOSTA (un altro admin, oppure /recupero con SETUP_CODE).
- Utenti, sessioni e registro stanno nello stesso archivio/archivio.db delle
  letture (vedi database.py), in tabelle create qui con IF NOT EXISTS.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from app.database import DB_PATH

RUOLI = ("viewer", "editor", "admin")
RANGO = {r: i for i, r in enumerate(RUOLI, start=1)}
DURATA_SESSIONE_S = 12 * 3600
PASSWORD_MIN = 10
# Tentativi di login/recupero falliti tollerati per finestra (per username
# oppure per IP), poi si risponde 429 finche' la finestra non scade.
MAX_TENTATIVI = 5
FINESTRA_TENTATIVI_S = 10 * 60

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def inizializza() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS utenti (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                nome TEXT NOT NULL DEFAULT '',
                ruolo TEXT NOT NULL CHECK (ruolo IN ('viewer','editor','admin')),
                attivo INTEGER NOT NULL DEFAULT 1,
                password_hash TEXT NOT NULL,
                creato TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessioni (
                token_hash TEXT PRIMARY KEY,
                utente_id INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
                scade REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registro_azioni (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quando TEXT NOT NULL,
                utente TEXT NOT NULL,
                azione TEXT NOT NULL,
                dettagli TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_registro_quando ON registro_azioni (id DESC);
        """)
        conn.commit()


# ---- password ------------------------------------------------------------

def hash_password(password: str) -> str:
    sale = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=sale, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${sale.hex()}${h.hex()}"


def verifica_password(password: str, salvato: str) -> bool:
    try:
        _, n, r, p, sale, atteso = salvato.split("$")
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(sale), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(h.hex(), atteso)
    except (ValueError, TypeError):
        return False


# Hash di comodo per far durare uguale il login con uno username inesistente
# (altrimenti il tempo di risposta rivela quali username esistono).
_HASH_FINTO = hash_password(secrets.token_hex(8))


def controlla_password_nuova(password: str) -> str | None:
    if len(password) < PASSWORD_MIN:
        return f"La password deve avere almeno {PASSWORD_MIN} caratteri."
    return None


# ---- utenti ----------------------------------------------------------------

def _adesso_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def numero_utenti() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM utenti").fetchone()[0]


def elenco_utenti() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, username, nome, ruolo, attivo FROM utenti ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


def crea_utente(username: str, password: str, nome: str, ruolo: str) -> str | None:
    """Restituisce None se creato, altrimenti il messaggio d'errore."""
    username = username.strip()
    if not username or " " in username:
        return "Username obbligatorio, senza spazi."
    if ruolo not in RUOLI:
        return "Ruolo non valido."
    errore = controlla_password_nuova(password)
    if errore:
        return errore
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO utenti (username, nome, ruolo, password_hash, creato) VALUES (?,?,?,?,?)",
                (username, nome.strip(), ruolo, hash_password(password), _adesso_iso()),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return f"Lo username '{username}' esiste gia'."
    return None


def crea_primo_admin(username: str, password: str, nome: str) -> str | None:
    """Come crea_utente, ma SOLO se non esiste nessun utente, in modo
    atomico: due richieste contemporanee a /setup non creano due admin."""
    username = username.strip()
    if not username or " " in username:
        return "Username obbligatorio, senza spazi."
    errore = controlla_password_nuova(password)
    if errore:
        return errore
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO utenti (username, nome, ruolo, password_hash, creato) "
            "SELECT ?,?,'admin',?,? WHERE NOT EXISTS (SELECT 1 FROM utenti)",
            (username, nome.strip(), hash_password(password), _adesso_iso()),
        )
        conn.commit()
        if cur.rowcount == 0:
            return "Esiste gia' un utente: la configurazione iniziale e' chiusa."
    return None


def _e_ultimo_admin_attivo(conn: sqlite3.Connection, utente_id: int) -> bool:
    riga = conn.execute("SELECT ruolo, attivo FROM utenti WHERE id=?", (utente_id,)).fetchone()
    if not riga or riga["ruolo"] != "admin" or not riga["attivo"]:
        return False
    altri = conn.execute(
        "SELECT COUNT(*) FROM utenti WHERE ruolo='admin' AND attivo=1 AND id<>?", (utente_id,)
    ).fetchone()[0]
    return altri == 0


def trova_utente(utente_id: int) -> dict | None:
    with _conn() as conn:
        r = conn.execute(
            "SELECT id, username, nome, ruolo, attivo FROM utenti WHERE id=?", (utente_id,)
        ).fetchone()
    return dict(r) if r else None


def imposta_password(utente_id: int, password: str) -> str | None:
    errore = controlla_password_nuova(password)
    if errore:
        return errore
    with _conn() as conn:
        conn.execute("UPDATE utenti SET password_hash=? WHERE id=?", (hash_password(password), utente_id))
        conn.execute("DELETE FROM sessioni WHERE utente_id=?", (utente_id,))
        conn.commit()
    return None


def imposta_ruolo(utente_id: int, ruolo: str) -> str | None:
    if ruolo not in RUOLI:
        return "Ruolo non valido."
    with _conn() as conn:
        if ruolo != "admin" and _e_ultimo_admin_attivo(conn, utente_id):
            return "Deve restare almeno un admin attivo."
        conn.execute("UPDATE utenti SET ruolo=? WHERE id=?", (ruolo, utente_id))
        # La sessione va riletta con il nuovo ruolo: la si chiude.
        conn.execute("DELETE FROM sessioni WHERE utente_id=?", (utente_id,))
        conn.commit()
    return None


def imposta_attivo(utente_id: int, attivo: bool) -> str | None:
    with _conn() as conn:
        if not attivo and _e_ultimo_admin_attivo(conn, utente_id):
            return "Deve restare almeno un admin attivo."
        conn.execute("UPDATE utenti SET attivo=? WHERE id=?", (1 if attivo else 0, utente_id))
        if not attivo:
            conn.execute("DELETE FROM sessioni WHERE utente_id=?", (utente_id,))
        conn.commit()
    return None


def elimina_utente(utente_id: int) -> str | None:
    with _conn() as conn:
        if _e_ultimo_admin_attivo(conn, utente_id):
            return "Deve restare almeno un admin attivo."
        conn.execute("DELETE FROM sessioni WHERE utente_id=?", (utente_id,))
        conn.execute("DELETE FROM utenti WHERE id=?", (utente_id,))
        conn.commit()
    return None


def reimposta_admin(username: str, password: str) -> str | None:
    """Recupero d'emergenza (/recupero): nuova password a un ADMIN esistente,
    che viene anche riattivato se era disattivato."""
    errore = controlla_password_nuova(password)
    if errore:
        return errore
    with _conn() as conn:
        r = conn.execute(
            "SELECT id FROM utenti WHERE username=? AND ruolo='admin'", (username.strip(),)
        ).fetchone()
        if not r:
            return "Nessun admin con questo username."
        conn.execute(
            "UPDATE utenti SET password_hash=?, attivo=1 WHERE id=?", (hash_password(password), r["id"])
        )
        conn.execute("DELETE FROM sessioni WHERE utente_id=?", (r["id"],))
        conn.commit()
    return None


# ---- login e sessioni --------------------------------------------------------

def autentica(username: str, password: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute(
            "SELECT id, username, nome, ruolo, attivo, password_hash FROM utenti WHERE username=?",
            (username.strip(),),
        ).fetchone()
    if r is None:
        verifica_password(password, _HASH_FINTO)
        return None
    if not verifica_password(password, r["password_hash"]) or not r["attivo"]:
        return None
    return {k: r[k] for k in ("id", "username", "nome", "ruolo", "attivo")}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def crea_sessione(utente_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with _conn() as conn:
        conn.execute("DELETE FROM sessioni WHERE scade < ?", (time.time(),))
        conn.execute(
            "INSERT INTO sessioni (token_hash, utente_id, scade) VALUES (?,?,?)",
            (_hash_token(token), utente_id, time.time() + DURATA_SESSIONE_S),
        )
        conn.commit()
    return token


def utente_da_token(token: str | None) -> dict | None:
    if not token:
        return None
    with _conn() as conn:
        r = conn.execute(
            "SELECT u.id, u.username, u.nome, u.ruolo, u.attivo FROM sessioni s "
            "JOIN utenti u ON u.id = s.utente_id "
            "WHERE s.token_hash=? AND s.scade>? AND u.attivo=1",
            (_hash_token(token), time.time()),
        ).fetchone()
    return dict(r) if r else None


def chiudi_sessione(token: str | None) -> None:
    if not token:
        return
    with _conn() as conn:
        conn.execute("DELETE FROM sessioni WHERE token_hash=?", (_hash_token(token),))
        conn.commit()


def ha_ruolo(utente: dict | None, minimo: str) -> bool:
    return bool(utente) and RANGO[utente["ruolo"]] >= RANGO[minimo]


# ---- registro delle azioni ------------------------------------------------------

def registra(utente: str, azione: str, dettagli: str = "", ip: str = "") -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO registro_azioni (quando, utente, azione, dettagli, ip) VALUES (?,?,?,?,?)",
            (_adesso_iso(), utente, azione, dettagli, ip),
        )
        conn.commit()


def troppi_tentativi(azione_fallita: str, utente: str, ip: str) -> bool:
    """True se in FINESTRA_TENTATIVI_S ci sono gia' MAX_TENTATIVI errori per
    questo username o per questo IP (l'azione fallita e' scritta nel
    registro da chi chiama)."""
    soglia = datetime.fromtimestamp(time.time() - FINESTRA_TENTATIVI_S, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with _conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM registro_azioni WHERE azione=? AND quando>=? AND (utente=? OR (ip<>'' AND ip=?))",
            (azione_fallita, soglia, utente, ip),
        ).fetchone()[0]
    return n >= MAX_TENTATIVI


def leggi_registro(utente: str = "", azione: str = "", limite: int = 300) -> list[dict]:
    query = "SELECT quando, utente, azione, dettagli, ip FROM registro_azioni WHERE 1=1"
    parametri: list = []
    if utente:
        query += " AND utente=?"
        parametri.append(utente)
    if azione:
        query += " AND azione=?"
        parametri.append(azione)
    query += " ORDER BY id DESC LIMIT ?"
    parametri.append(limite)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(query, parametri).fetchall()]


def azioni_distinte() -> list[str]:
    with _conn() as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT azione FROM registro_azioni ORDER BY azione")]


def codice_setup() -> str | None:
    """Il SETUP_CODE del .env, letto a ogni richiesta dall'ambiente del
    container (cambia solo ricreando il container). Assente = la
    configurazione iniziale e il recupero admin sono spenti."""
    return os.environ.get("SETUP_CODE") or None
