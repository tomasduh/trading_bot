#!/bin/bash
set -e

# Crear subdirectorios dentro del volumen persistente
mkdir -p /app/data/logs

# Symlink /app/logs → /app/data/logs para que config.py no cambie
[ -L /app/logs ] || ln -sfn /app/data/logs /app/logs

# Bot loop en segundo plano
python -m src.bot &
BOT_PID=$!

# Dashboard API en primer plano
python -m uvicorn src.api:app --host 0.0.0.0 --port 8000 &
API_PID=$!

# Si cualquiera muere, matar ambos para que Fly reinicie el contenedor
wait -n $BOT_PID $API_PID
kill $BOT_PID $API_PID 2>/dev/null
exit 1
