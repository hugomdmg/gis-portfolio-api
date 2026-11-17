
FROM python:3.12-slim

WORKDIR /app

# Instalar herramientas de PostGIS y cliente PostgreSQL
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    postgis \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código y datos
COPY ./src ./app
COPY ./data ./data

# Comprobar que las herramientas están instaladas
RUN command -v raster2pgsql && command -v psql

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
