"""
Controllo accessi (middleware) e pagine di login, prima configurazione,
recupero admin, account e amministrazione utenti/registro.

Il middleware e' il punto unico di applicazione delle regole, DEFAULT
DENY: ogni rotta richiede login, e una rotta nuova e' protetta anche se ci
si dimentica di pensarci.
- GET/HEAD: qualsiasi utente loggato (viewer).
- POST/PUT/DELETE/PATCH: editor (upload, distretti, mappa, invio a WMS...).
- /admin/*: admin (utenti e registro), anche in lettura.
- Eccezioni senza login: /health (healthcheck Docker), /static, /login,
  /logout, /setup, /recupero. /account/password: qualsiasi utente loggato.
Definito con Daniele il 19/09/2026.
"""
from __future__ import annotations

import hmac
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth

COOKIE = "sessione"
PERCORSI_LIBERI = ("/health", "/static", "/login", "/logout", "/setup", "/recupero")
PERCORSI_ANCHE_VIEWER = ("/account/password",)
METODI_SICURI = ("GET", "HEAD", "OPTIONS")


def ip_client(request: Request) -> str:
    # Caddy (reverse proxy davanti) mette l'IP vero in X-Forwarded-For.
    inoltrato = request.headers.get("x-forwarded-for", "")
    if inoltrato:
        return inoltrato.split(",")[0].strip()
    return request.client.host if request.client else ""


def _percorso_libero(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PERCORSI_LIBERI)


def _destinazione_sicura(dest: str) -> str:
    """Evita open redirect: solo percorsi interni ("/..." ma non "//host")."""
    if dest.startswith("/") and not dest.startswith("//") and "\\" not in dest:
        return dest
    return "/"


def _origine_ammessa(request: Request) -> bool:
    """Difesa CSRF in piu' rispetto a SameSite=Lax: una richiesta che
    modifica dati e ha un'intestazione Origin deve arrivare dallo stesso
    host. Senza Origin (curl, script) passa: serve comunque il cookie."""
    origine = request.headers.get("origin")
    if not origine:
        return True
    return urlsplit(origine).netloc == request.headers.get("host", "")


def _nega(request: Request, codice: int, messaggio: str, templates: Jinja2Templates):
    if request.url.path.startswith("/api") or "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"errore": messaggio}, status_code=codice)
    return templates.TemplateResponse(
        request, "accesso_negato.html", {"messaggio": messaggio}, status_code=codice
    )


def registra_accessi(app: FastAPI, templates: Jinja2Templates) -> None:
    auth.inizializza()

    @app.middleware("http")
    async def controllo_accessi(request: Request, call_next):
        request.state.utente = None
        path = request.url.path

        if request.method not in METODI_SICURI and not _origine_ammessa(request):
            return _nega(request, 403, "Richiesta da un'origine non ammessa.", templates)

        utente = auth.utente_da_token(request.cookies.get(COOKIE))
        request.state.utente = utente
        if _percorso_libero(path):
            return await call_next(request)

        if utente is None:
            if request.method in METODI_SICURI and "text/html" in request.headers.get("accept", ""):
                if auth.numero_utenti() == 0:
                    return RedirectResponse("/setup", status_code=303)
                dest = path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(dest)}", status_code=303)
            return JSONResponse({"errore": "Accesso non autenticato."}, status_code=401)

        if path.startswith("/admin"):
            minimo = "admin"
        elif request.method in METODI_SICURI or path in PERCORSI_ANCHE_VIEWER:
            minimo = "viewer"
        else:
            minimo = "editor"
        if not auth.ha_ruolo(utente, minimo):
            return _nega(request, 403, f"Questa azione richiede il ruolo {minimo}.", templates)

        return await call_next(request)

    def pagina(request: Request, nome: str, **contesto):
        return templates.TemplateResponse(request, nome, contesto)

    def imposta_cookie(request: Request, risposta, token: str):
        risposta.set_cookie(
            COOKIE, token, max_age=auth.DURATA_SESSIONE_S, httponly=True, samesite="lax",
            secure=request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https",
        )
        return risposta

    # ---- login / logout ----------------------------------------------------

    @app.get("/login")
    def login_pagina(request: Request, next: str = "/"):
        if request.state.utente:
            return RedirectResponse(_destinazione_sicura(next), status_code=303)
        if auth.numero_utenti() == 0:
            return RedirectResponse("/setup", status_code=303)
        return pagina(request, "login.html", next=_destinazione_sicura(next), errore=None)

    @app.post("/login")
    def login_invio(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
        ip = ip_client(request)
        nome = username.strip()
        if auth.troppi_tentativi("login_fallito", nome, ip):
            return templates.TemplateResponse(
                request, "login.html",
                {"next": _destinazione_sicura(next), "errore": "Troppi tentativi falliti: riprova tra qualche minuto."},
                status_code=429,
            )
        utente = auth.autentica(nome, password)
        if utente is None:
            auth.registra(nome, "login_fallito", "", ip)
            return templates.TemplateResponse(
                request, "login.html",
                {"next": _destinazione_sicura(next), "errore": "Username o password non corretti."},
                status_code=401,
            )
        auth.registra(utente["username"], "login", "", ip)
        risposta = RedirectResponse(_destinazione_sicura(next), status_code=303)
        return imposta_cookie(request, risposta, auth.crea_sessione(utente["id"]))

    @app.post("/logout")
    def logout(request: Request):
        utente = request.state.utente
        if utente:
            auth.registra(utente["username"], "logout", "", ip_client(request))
        auth.chiudi_sessione(request.cookies.get(COOKIE))
        risposta = RedirectResponse("/login", status_code=303)
        risposta.delete_cookie(COOKIE)
        return risposta

    # ---- prima configurazione e recupero (protetti da SETUP_CODE) ------------

    def _codice_ok(inserito: str) -> bool:
        atteso = auth.codice_setup()
        return bool(atteso) and hmac.compare_digest(inserito.encode(), atteso.encode())

    @app.get("/setup")
    def setup_pagina(request: Request):
        if auth.numero_utenti() > 0:
            return RedirectResponse("/login", status_code=303)
        return pagina(request, "setup.html", errore=None, codice_configurato=bool(auth.codice_setup()))

    @app.post("/setup")
    def setup_invio(
        request: Request, codice: str = Form(...), username: str = Form(...), nome: str = Form(""),
        password: str = Form(...), conferma: str = Form(...),
    ):
        ip = ip_client(request)

        def errore(msg: str, status: int = 400):
            return templates.TemplateResponse(
                request, "setup.html", {"errore": msg, "codice_configurato": bool(auth.codice_setup())},
                status_code=status,
            )

        if auth.numero_utenti() > 0:
            return RedirectResponse("/login", status_code=303)
        if auth.troppi_tentativi("setup_fallito", "", ip):
            return errore("Troppi tentativi falliti: riprova tra qualche minuto.", 429)
        if not _codice_ok(codice):
            auth.registra("", "setup_fallito", "codice errato o non configurato", ip)
            return errore("Codice di setup non valido.", 403)
        if password != conferma:
            return errore("Le due password non coincidono.")
        esito = auth.crea_primo_admin(username, password, nome)
        if esito:
            return errore(esito)
        utente = auth.autentica(username, password)
        auth.registra(utente["username"], "setup_admin", "primo amministratore creato", ip)
        return imposta_cookie(request, RedirectResponse("/", status_code=303), auth.crea_sessione(utente["id"]))

    @app.get("/recupero")
    def recupero_pagina(request: Request):
        if not auth.codice_setup() or auth.numero_utenti() == 0:
            return HTMLResponse("Not Found", status_code=404)
        return pagina(request, "recupero.html", errore=None)

    @app.post("/recupero")
    def recupero_invio(
        request: Request, codice: str = Form(...), username: str = Form(...),
        password: str = Form(...), conferma: str = Form(...),
    ):
        if not auth.codice_setup() or auth.numero_utenti() == 0:
            return HTMLResponse("Not Found", status_code=404)
        ip = ip_client(request)

        def errore(msg: str, status: int = 400):
            return templates.TemplateResponse(request, "recupero.html", {"errore": msg}, status_code=status)

        if auth.troppi_tentativi("recupero_fallito", "", ip):
            return errore("Troppi tentativi falliti: riprova tra qualche minuto.", 429)
        if not _codice_ok(codice):
            auth.registra("", "recupero_fallito", "codice errato", ip)
            return errore("Codice di setup non valido.", 403)
        if password != conferma:
            return errore("Le due password non coincidono.")
        esito = auth.reimposta_admin(username, password)
        if esito:
            return errore(esito)
        auth.registra(username.strip(), "recupero_admin", "password admin reimpostata con SETUP_CODE", ip)
        return RedirectResponse("/login", status_code=303)

    # ---- account personale -----------------------------------------------------

    @app.get("/account")
    def account_pagina(request: Request, msg: str = "", errore: str = ""):
        return pagina(request, "account.html", msg=msg, errore=errore)

    @app.post("/account/password")
    def account_password(
        request: Request, attuale: str = Form(...), nuova: str = Form(...), conferma: str = Form(...),
    ):
        utente = request.state.utente
        if auth.autentica(utente["username"], attuale) is None:
            return RedirectResponse("/account?errore=" + quote("La password attuale non è corretta."), status_code=303)
        if nuova != conferma:
            return RedirectResponse("/account?errore=" + quote("Le due password nuove non coincidono."), status_code=303)
        esito = auth.imposta_password(utente["id"], nuova)
        if esito:
            return RedirectResponse("/account?errore=" + quote(esito), status_code=303)
        auth.registra(utente["username"], "password_cambiata", "", ip_client(request))
        # imposta_password chiude tutte le sessioni dell'utente, compresa questa.
        risposta = RedirectResponse("/login", status_code=303)
        risposta.delete_cookie(COOKIE)
        return risposta

    # ---- amministrazione (solo admin, imposto dal middleware) ---------------------

    def _torna_utenti(msg: str = "", errore: str = ""):
        query = "&".join(
            p for p in (f"msg={quote(msg)}" if msg else "", f"errore={quote(errore)}" if errore else "") if p
        )
        return RedirectResponse("/admin/utenti" + (f"?{query}" if query else ""), status_code=303)

    @app.get("/admin/utenti")
    def admin_utenti(request: Request, msg: str = "", errore: str = ""):
        return pagina(
            request, "amministrazione.html", pagina_attiva="admin", sezione="utenti",
            utenti=auth.elenco_utenti(), ruoli=auth.RUOLI, msg=msg, errore=errore,
        )

    @app.post("/admin/utenti/crea")
    def admin_crea(
        request: Request, username: str = Form(...), password: str = Form(...),
        nome: str = Form(""), ruolo: str = Form("viewer"),
    ):
        esito = auth.crea_utente(username, password, nome, ruolo)
        if esito:
            return _torna_utenti(errore=esito)
        auth.registra(request.state.utente["username"], "utente_creato", f"{username.strip()} ({ruolo})", ip_client(request))
        return _torna_utenti(msg=f"Utente {username.strip()} creato.")

    def _bersaglio(utente_id: int):
        return auth.trova_utente(utente_id)

    @app.post("/admin/utenti/{utente_id}/ruolo")
    def admin_ruolo(request: Request, utente_id: int, ruolo: str = Form(...)):
        u = _bersaglio(utente_id)
        if not u:
            return _torna_utenti(errore="Utente inesistente.")
        esito = auth.imposta_ruolo(utente_id, ruolo)
        if esito:
            return _torna_utenti(errore=esito)
        auth.registra(request.state.utente["username"], "ruolo_cambiato", f"{u['username']}: {u['ruolo']} -> {ruolo}", ip_client(request))
        return _torna_utenti(msg=f"Ruolo di {u['username']} aggiornato.")

    @app.post("/admin/utenti/{utente_id}/password")
    def admin_password(request: Request, utente_id: int, password: str = Form(...)):
        u = _bersaglio(utente_id)
        if not u:
            return _torna_utenti(errore="Utente inesistente.")
        esito = auth.imposta_password(utente_id, password)
        if esito:
            return _torna_utenti(errore=esito)
        auth.registra(request.state.utente["username"], "password_reimpostata", u["username"], ip_client(request))
        return _torna_utenti(msg=f"Password di {u['username']} aggiornata.")

    @app.post("/admin/utenti/{utente_id}/attivo")
    def admin_attivo(request: Request, utente_id: int, attivo: int = Form(...)):
        u = _bersaglio(utente_id)
        if not u:
            return _torna_utenti(errore="Utente inesistente.")
        if u["id"] == request.state.utente["id"]:
            return _torna_utenti(errore="Non puoi disattivare il tuo stesso account.")
        esito = auth.imposta_attivo(utente_id, bool(attivo))
        if esito:
            return _torna_utenti(errore=esito)
        auth.registra(request.state.utente["username"], "utente_riattivato" if attivo else "utente_disattivato", u["username"], ip_client(request))
        return _torna_utenti(msg=f"{u['username']} {'riattivato' if attivo else 'disattivato'}.")

    @app.post("/admin/utenti/{utente_id}/elimina")
    def admin_elimina(request: Request, utente_id: int):
        u = _bersaglio(utente_id)
        if not u:
            return _torna_utenti(errore="Utente inesistente.")
        if u["id"] == request.state.utente["id"]:
            return _torna_utenti(errore="Non puoi eliminare il tuo stesso account.")
        esito = auth.elimina_utente(utente_id)
        if esito:
            return _torna_utenti(errore=esito)
        auth.registra(request.state.utente["username"], "utente_eliminato", u["username"], ip_client(request))
        return _torna_utenti(msg=f"{u['username']} eliminato.")

    @app.get("/admin/registro")
    def admin_registro(request: Request, utente: str = "", azione: str = ""):
        return pagina(
            request, "registro.html", pagina_attiva="admin", sezione="registro",
            righe=auth.leggi_registro(utente, azione), utenti=auth.elenco_utenti(),
            azioni=auth.azioni_distinte(), filtro_utente=utente, filtro_azione=azione,
        )
