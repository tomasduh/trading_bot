#!/bin/bash
set -e

# Crear subdirectorios dentro del volumen persistente
mkdir -p /app/data/logs

# Symlink /app/logs → /app/data/logs para que config.py no cambie
[ -L /app/logs ] || ln -sfn /app/data/logs /app/logs

# Un solo proceso Python: uvicorn sirve el dashboard Y lanza el bot loop
# como asyncio.to_thread en su lifespan. Esto ahorra ~150MB de RAM vs
# tener dos procesos Python separados cargando pandas/numpy/ccxt dos veces.
exec python -m uvicorn src.api:app --host 0.0.0.0 --port 8000