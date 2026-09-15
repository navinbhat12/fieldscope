# Single image for both the API and the loader. They share a database driver
# and a Parquet reader, and keeping them together means the deploy sequence on
# the VM is "docker compose up" then "docker compose run api python
# scripts/load_serving.py" -- no second build, no second dependency set.
FROM python:3.11-slim

# GEOS/PROJ are not needed here: every geometric operation happens inside
# PostGIS, and this container only ever ships WKB and GeoJSON across the wire.
# That is what keeps the image small enough to be comfortable on an e2-micro.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Schema DDL travels with the image so the loader applies the same schema on
# the VM as it does locally -- this is what replaces a migration tool.
COPY schema.sql indexes.sql ./

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
