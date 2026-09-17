# Fatturazione Utenze — container separato da WMS SmartH2O (vedi project doc
# "riepilogo-progetto-wms-smarth2o.md", sezioni 4.7/4.9): stesso VPS, database
# e dipendenze indipendenti, collegati solo tramite la rete Docker interna
# condivisa "rete-interna-idrico" (vedi docker-compose.yml).

FROM python:3.12-slim

WORKDIR /app

# Le dipendenze cambiano raramente rispetto al codice: copiarle e
# installarle per prime sfrutta la cache di Docker (build molto più
# veloci quando si modifica solo il codice Python).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

# Cartelle dati: in produzione vengono sostituite dai volumi Docker
# (docker-compose.yml), ma servono comunque perché il codice le usa con
# percorsi relativi ("archivio/...", "output/...") — vedi motore_calcolo.py.
RUN mkdir -p archivio input output

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
