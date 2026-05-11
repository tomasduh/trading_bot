#!/bin/bash
# Ejecutar desde tu PC (Windows Git Bash / WSL)
# Uso: bash deploy/upload.sh <IP_DEL_SERVIDOR>
# Ejemplo: bash deploy/upload.sh 65.21.123.45

SERVER_IP="${1:?Falta IP del servidor. Uso: bash upload.sh <IP>}"
REMOTE="root@$SERVER_IP"
APP_DIR="/home/bottrading/bot"

echo "=== Subiendo archivos de configuración al servidor ==="
scp deploy/bot-trading-bot.service  "$REMOTE:/tmp/"
scp deploy/bot-trading-api.service  "$REMOTE:/tmp/"
scp deploy/bot-trading-nginx.conf   "$REMOTE:/tmp/"
scp deploy/setup.sh                 "$REMOTE:/tmp/"

echo "=== Ejecutando setup en el servidor ==="
ssh "$REMOTE" "bash /tmp/setup.sh"

echo "=== Subiendo código del bot ==="
rsync -av --exclude='.env' \
          --exclude='__pycache__' \
          --exclude='*.pyc' \
          --exclude='.venv' \
          --exclude='data/*.db' \
          --exclude='data/*.jsonl' \
          --exclude='logs/' \
          . "$REMOTE:$APP_DIR/"

echo "=== Subiendo .env (claves privadas) ==="
scp .env "$REMOTE:$APP_DIR/.env"
ssh "$REMOTE" "chown bottrading:bottrading $APP_DIR/.env && chmod 600 $APP_DIR/.env"

echo "=== Iniciando servicios ==="
ssh "$REMOTE" "systemctl start bot-trading-api bot-trading-bot"
ssh "$REMOTE" "systemctl status bot-trading-api --no-pager -l"

echo ""
echo "✓ Deploy completo!"
echo "  Dashboard: http://$SERVER_IP"
echo "  Logs API:  ssh $REMOTE 'journalctl -fu bot-trading-api'"
echo "  Logs Bot:  ssh $REMOTE 'journalctl -fu bot-trading-bot'"
