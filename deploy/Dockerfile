FROM python:3.12-slim

WORKDIR /app

# System deps for psycopg binary and cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY . .

# Default: web server. Override CMD in docker-compose for the agents service.
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8080"]
