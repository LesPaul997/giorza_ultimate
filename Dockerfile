# ---------------- Dockerfile (root del repo) ----------------
FROM python:3.11-bookworm         

# Non bufferizzare stdout/stderr
ENV PYTHONUNBUFFERED=1

# ------------ installa MS-ODBC 18 + tool di build ----------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl gpg ca-certificates build-essential unixodbc-dev && \
    # importa la chiave Microsoft nel formato corretto
    curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/ms.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/ms.gpg] \
        https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y msodbcsql18 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ------------ dipendenze Python ----------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ------------ codice ---------------------------------------
COPY . .

# Esegui gunicorn sulla porta che Render mette in $PORT
CMD ["sh", "-c", "gunicorn app:app -b 0.0.0.0:${PORT}"]