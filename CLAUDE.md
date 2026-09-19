# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Analisi Consumi da Fatturazione" (formerly "Fatturazione Utenze") — a water-consumption analysis service for Daniele. The purpose is NOT billing: it is the water balance and leak reduction. It reads quarterly meter-reading extracts from the Neta H2O CRM (one Excel file per comune/municipality) and computes the billed/consumed volume per water district (`distretto`) per month, which eventually feeds the `district_billed` table of a separate, already-live app called **WMS SmartH2O**. WMS holds the inflow (immesso, daily/monthly) and night minimum flows, and does the top-down comparison inflow vs. billed: a monthly trend, consolidated once a year. Estimated (provisional) months are acceptable for the trend, as long as they are flagged and later upserted (see below). Names like `district_billed`, `Volume Fatturato (m3)` and `Import_WMS` stay unchanged (WMS format).

The two apps are deliberately separate (see `project_docs/riepilogo-progetto-wms-smarth2o.md`, §4.7): no shared database, no shared container. The web app (FastAPI + Jinja2 pages, SQLite archive, upload, per-comune results cache, login with viewer/editor/admin roles and an audit log) sits on top of the calculation engine; the main open work is the one-click push to WMS (see below and `README_DOCKER.md`).

Read `project_docs/specifiche-applicativo-fatturazione-utenze.md` and `project_docs/riepilogo-progetto-wms-smarth2o.md` before making calculation-logic or architecture decisions — they record decisions already made with Daniele (including ones that reversed earlier approaches). Don't re-derive or re-litigate something already settled there.

## Commands

There is no test suite, linter, or formatter configured in this repo — don't assume one and don't add one unprompted.

```bash
# Install deps locally
pip install -r requirements.txt

# Run the service locally (reads/writes ./archivio, ./input, ./output relative to cwd)
uvicorn app.main:app --reload --port 8000

# Build and run via Docker (production path, on the VPS at /opt/billing)
docker compose up -d --build
curl http://localhost:8010/health

# Quick manual exercise of the calculation engine (no web layer involved)
python3 -c "
from app.motore_calcolo import elabora_file, esporta_excel
r = elabora_file(['input/BELGIOIOSO.xlsx', 'input/BELGIOIOSO_2.xlsx'])
esporta_excel(r, 'output/test.xlsx')
"
```

`archivio/`, `input/`, and `output/` are gitignored and dockerignored (real customer data) but are bind-mounted as Docker volumes in `docker-compose.yml` so they persist across container rebuilds. `input/*.xlsx` are raw Neta H2O extracts (not committed); `archivio/*.csv` is the persistent historical store built incrementally by `aggiorna_archivio()`.

## Architecture

**`app/main.py`** — FastAPI app, currently just `/health` and `/`. It imports `app.motore_calcolo` purely so a broken engine fails container startup instead of failing silently later. Everything described in `README_DOCKER.md`'s "Cosa NON c'è ancora" (upload endpoint, real pages, the WMS "one-click" push, auth, a real database) is unbuilt — this file is the intended entry point for all of it.

**`app/motore_calcolo.py`** — the entire calculation engine, self-contained pandas code, no web dependencies. This is where almost all the domain logic lives. Structure to understand before touching it:

1. **Ingestion** (`carica_estrazione`, `classifica_distretto`) — loads one Neta H2O Excel extract, validates its columns against `COLONNE_ATTESE`, and classifies every row's `DISTRETTO` value as `valido` / `case_sparse` (expected, non-districted points — heuristically anything starting with `"NO"`) / `anomalia` (likely data-entry error, surfaced in the `Segnalazioni` sheet, never silently dropped from the historical record — just excluded from district totals). The comune-detection prefix heuristic (`_prefisso_dominante`) exists because there's no official comune↔distretto reference list yet (see spec doc §6.1); it's meant to be replaced once Daniele provides one.

2. **Historical archive** (`carica_archivio`, `aggiorna_archivio`) — every new extract is appended to a CSV on disk, deduplicated on `CHIAVE_ARCHIVIO` = (`CODICE_SERVIZIO`, `DATA_LETTURA`, `TIPO_LETTURA`). This is what lets each new quarterly extract just merge into the existing history instead of requiring a full reload. There's one archive per comune in practice (`archivio_letture.csv`, `archivio_letture_mortara.csv`).

3. **Metodo B — the only billing calculation method** (`calcola_periodi_metodo_b` and everything downstream of it). This is the load-bearing piece of domain logic in the whole codebase:
   - Each period between two real readings (dates only, not `GG_LETT_PREC`) is prorated across calendar months in proportion to overlapping days (a reading's consumption is not "this month's consumption"). `GG_LETT_PREC`/`CONSUMO` are used only for the trailing estimates not yet closed by a real reading (all of them counted, as provisional values), where `GG_LETT_PREC` is the days since the previous reading (Daniele, 19/09/2026).
   - Billed volume is the **physical difference between consecutive real readings** (`TIPI_LETTURA_REALE`), not the `CONSUMO`/`GG_LETT_PREC` figures Neta H2O declares. Estimated readings (`STIMATA` and similar) in between two real readings are discarded/absorbed, never summed — a real reading reconciles (conguaglia) whatever was estimated before it, it doesn't add to it.
   - There used to be a "Metodo A" (sum of declared `CONSUMO`) kept for comparison. **It has been fully removed from the code** per Daniele's explicit instruction, because it systematically double-counted already-reconciled estimates. Do not reintroduce it, even as a diagnostic/comparison column — if you need to reference why, see spec doc §2.4/§2.10 and project summary §4.4.
   - Meter swaps (`TIPI_INIZIO_CONTATORE`) always break the reading chain — a difference is never computed across two different physical meters.
   - Sentinel values (`LETTURA_SENTINELLA_MIN = 999_999`) and implausible consumption rates (`MC_GIORNO_SANITA_MASSIMA = 500` m³/day) are excluded as data-quality anomalies, not billed — see `Anomalie_MetodoB`.
   - If a district changes on a point mid-stream, the *whole* period between two real readings is attributed to the district on the closing (more recent) reading — a known, accepted simplification (spec doc §6.1), not a bug.

4. **Aggregation & reporting** (`aggrega*`, `calcola_riepilogo_trimestrale`, `calcola_statistiche_*`, `calcola_coefficiente_punta`, `costruisci_segnalazioni`, `trova_utenze_scomparse`, `trova_cessate_con_stima_finale`) — turn the prorated per-reading periods into per-district-per-month/quarter volumes, usage-class breakdowns, active-customer counts, and various data-quality reports (disappeared customers, closed contracts whose last reading was an estimate instead of a required real one, etc.).

5. **Entry points**: `elabora_file(paths)` for a quick one-off run over specific files; `elabora_dataframe(df)` for the normal path (archive → `elabora_dataframe`), returning a single `RisultatoElaborazione` dataclass that holds every computed table. `esporta_excel(risultato, path)` writes it all out as a multi-sheet Excel workbook — this remains available as a secondary/optional export once the web frontend exists (project summary §6.0), it's not being removed.

**Only ever one number per district/month/quarter** (Metodo B) flows anywhere in this codebase — don't build any feature that reintroduces a second method or a Metodo A/B comparison.

**`Import_WMS`** (i.e. `RisultatoElaborazione.volumi_distretto_mese`) is the one output that matters for the WMS SmartH2O integration: one row per (month, district code, billed m³), using the district's Neta H2O *code* (e.g. `DBLG03`), not any internal numeric ID — WMS SmartH2O does that translation on its side.

## Access control

Separate login from WMS (`app/auth.py`, `app/accessi.py`; details in `README_DOCKER.md`, section "Accessi"). Roles viewer < editor < admin, enforced server-side by a default-deny middleware — any new route needs login, and any non-GET needs editor, without extra code; `/admin/*` needs admin. When adding an action that changes data, log it with `auth.registra(utente, azione, dettagli, ip)` (audit trail requested by Daniele: who did what). Never ask for, print, or store passwords or `SETUP_CODE` in chat/git; the `.env` stays out of git. Keep port 8010 bound to 127.0.0.1.

## Integration with WMS SmartH2O (not yet built)

- The two containers share a Docker bridge network, `rete-interna-idrico` (external, created once on the VPS, not by this repo's compose file). WMS SmartH2O is reachable at `http://wms-smarth20:80` (port 80 internally, *not* 8080 — that's only the host-published port for Caddy).
- Calls between the two services will be authenticated with a shared secret, `INTERNAL_API_TOKEN` (see `.env.example`); the target URL is `WMS_API_URL`.
- The push to `district_billed` is meant to stay a manual "one-click" action (a button, human-confirmed) rather than a scheduler, until the calculation logic is fully confirmed by Neta H2O (see project summary §4.9) — don't build this as an automatic cron/scheduler unless that decision changes.
- Provisional values matter: a quarter can be billed with an estimate when a real reading is still missing (flagged via `flag_mesi_provvisori`/"Contiene Stime Provvisorie"), and gets silently corrected on the next recalculation once the real reading arrives. Any future loading endpoint into `district_billed` needs to support **upsert**, not insert-only, because of this.

## Conventions

- All domain code, comments, and docstrings are in Italian, matching the Neta H2O source column names (`DISTRETTO`, `CODICE_SERVIZIO`, `CONSUMO`, etc.) and the business domain — keep new code consistent with this rather than switching to English identifiers.
- Comments in `motore_calcolo.py` frequently cite *why* a rule exists (a specific data anomaly found, a specific conversation/date with Daniele). When changing that logic, preserve or update that provenance rather than deleting it — it's the only record of decisions that aren't written in `project_docs/`.
- `app/motore_calcolo.py` is the authoritative copy. Don't create or resurrect a root-level duplicate (`README_DOCKER.md` documents that an earlier root copy existed during initial development and was superseded — it's gone now).
