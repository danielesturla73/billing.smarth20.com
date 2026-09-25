"""
Scarica da OpenStreetMap (Overpass API) il tracciato delle vie con nome dei
comuni in archivio e lo salva in project_docs/vie_osm.geojson (richiesto da
Daniele il 25/09/2026): e' la fonte indipendente da Neta per controllare le
coordinate delle prese e lo stradario via -> distretto.

Una tantum; si rilancia solo se serve (vie nuove, comuni nuovi):

    docker exec -w /app -e PYTHONPATH=/app billing python3 scripts/scarica_vie_osm.py [COMUNE ...]

Dati (c) OpenStreetMap contributors, licenza ODbL.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app import database, prese

PERCORSO_VIE_OSM = Path("project_docs/vie_osm.geojson")
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "billing-analisi-consumi/1.0 (stradario distretti)"


def _nome_istat(comune: str) -> str | None:
    """Nome ISTAT del comune (con accenti, es. Gambolò) dai confini ISTAT."""
    chiave = prese._nome_comune(comune)
    for n, etichetta, _, _ in prese._poligoni_comuni():
        if n == chiave:
            return etichetta.split(" (")[0]
    return None


def scarica_comune(comune: str) -> list[dict]:
    nome = _nome_istat(comune) or comune.title()
    query = (
        f'[out:json][timeout:120];area["name"="{nome}"]["admin_level"="8"]["boundary"="administrative"]->.a;'
        'way["highway"]["name"](area.a);out geom tags;'
    )
    richiesta = urllib.request.Request(
        OVERPASS, data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for tentativo in range(5):
        try:
            with urllib.request.urlopen(richiesta, timeout=180) as r:
                dati = json.load(r)
            break
        except urllib.error.HTTPError as exc:
            # 429/504: il servizio pubblico e' occupato, si riprova piu' tardi.
            if exc.code not in (429, 502, 503, 504) or tentativo == 4:
                raise
            time.sleep(30 * (tentativo + 1))
    return [
        {
            "type": "Feature",
            "properties": {"comune": comune, "nome": e["tags"]["name"], "osm_id": e["id"], "highway": e["tags"].get("highway", "")},
            "geometry": {"type": "LineString", "coordinates": [[round(p["lon"], 6), round(p["lat"], 6)] for p in e["geometry"]]},
        }
        for e in dati.get("elements", []) if e.get("type") == "way" and len(e.get("geometry", [])) >= 2
    ]


def main(comuni: list[str]) -> None:
    if not comuni:
        with database.connessione() as conn:
            comuni = [r[0] for r in conn.execute("SELECT DISTINCT LOCALITA FROM anagrafica_servizi ORDER BY 1")]
    esistenti = json.loads(PERCORSO_VIE_OSM.read_text(encoding="utf-8"))["features"] if PERCORSO_VIE_OSM.exists() else []
    rifatti = {c.strip().upper() for c in comuni}
    tenute = [f for f in esistenti if f["properties"]["comune"] not in rifatti]
    for comune in comuni:
        vie = scarica_comune(comune.strip().upper())
        print(f"{comune}: {len(vie)} tratti, {len({v['properties']['nome'] for v in vie})} vie", flush=True)
        tenute += vie
        # Salvato dopo ogni comune: se il servizio si ferma non si perde il fatto.
        PERCORSO_VIE_OSM.write_text(json.dumps({
            "type": "FeatureCollection",
            "fonte": "(c) OpenStreetMap contributors, ODbL - scaricato con Overpass API",
            "scaricato_il": time.strftime("%Y-%m-%d"),
            "features": tenute,
        }, ensure_ascii=False), encoding="utf-8")
        time.sleep(2)  # gentilezza verso il servizio pubblico


if __name__ == "__main__":
    main(sys.argv[1:])
