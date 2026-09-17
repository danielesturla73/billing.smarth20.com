"""
Migrazione one-off: importa i CSV storici per comune (archivio_letture.csv,
archivio_letture_mortara.csv, ecc.) nel nuovo archivio SQLite unico
(archivio/archivio.db). Da eseguire UNA VOLTA sola prima di far passare
l'applicativo alla lettura da database (vedi app/database.py).

Uso: python -m scripts.migra_csv_a_sqlite (dalla cartella /app dentro il
container, o dalla radice del progetto in locale).

Non tocca/cancella i CSV originali: restano sul disco come riferimento e
backup, semplicemente l'app smette di leggerli dopo questa migrazione.
Se un comune risulta gia' presente nel database (rilanciando lo script
per errore), le sue righe vengono sostituite con quelle ricalcolate dal
CSV, non duplicate.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app import database, motore_calcolo

ARCHIVIO_DIR = Path("archivio")


def migra() -> None:
    csv_trovati = sorted(ARCHIVIO_DIR.glob("*.csv"))
    if not csv_trovati:
        print(f"Nessun CSV trovato in {ARCHIVIO_DIR}/, niente da migrare.")
        return

    with database.connessione() as conn:
        for percorso_csv in csv_trovati:
            df = motore_calcolo.carica_archivio(percorso_csv)
            if df.empty:
                print(f"{percorso_csv.name}: vuoto, saltato.")
                continue

            comune = motore_calcolo.comune_dominante(df) or "SCONOSCIUTO"
            df = df.sort_values("DATA_LETTURA").drop_duplicates(subset=database.CHIAVE_LETTURE, keep="first")

            if database.tabella_esiste(conn):
                conn.execute("DELETE FROM letture WHERE LOCALITA = ?", (comune,))
            da_scrivere = df.copy()
            for col in database.COLONNE_DATA:
                if col in da_scrivere.columns:
                    da_scrivere[col] = pd.to_datetime(da_scrivere[col], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
            da_scrivere.to_sql("letture", conn, if_exists="append", index=False)
            conn.commit()
            database.assicura_indici(conn)
            print(f"{percorso_csv.name} -> comune '{comune}': {len(df)} righe migrate.")

    print(f"\nMigrazione completata. Database: {database.DB_PATH}")


if __name__ == "__main__":
    migra()
