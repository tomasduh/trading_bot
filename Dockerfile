FROM python:3.12-slim

WORKDIR /app

# Sistema mínimo
RUN apt-get update -qq && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# Dependencias Python primero (cachea bien)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código
COPY src/      src/
COPY static/   static/

# Directorios persistidos vía Fly Volume (montado en /app/data y /app/logs)
RUN mkdir -p data logs

EXPOSE 8000

COPY deploy/start.sh /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]
