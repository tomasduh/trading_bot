#!/bin/bash
# Ejecutar como root en Ubuntu 22.04 LTS
# Uso: bash setup.sh
set -e

APP_USER="bottrading"
APP_DIR="/home/$APP_USER/bot"

echo "=== 1. Sistema base ==="
apt-get update -qq
apt-get install -y python3.11 python3.11-venv python3-pip nginx git curl ufw

echo "=== 2. Usuario de app ==="
id "$APP_USER" &>/dev/null || useradd -m -s /bin/bash "$APP_USER"

echo "=== 3. Directorio de la app ==="
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"

echo "=== 4. Instalar dependencias Python ==="
sudo -u "$APP_USER" python3.11 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install \
    fastapi uvicorn[standard] \
    ccxt pandas pandas-ta numpy python-dotenv \
    SQLAlchemy scikit-learn xgboost requests colorlog \
    alpaca-py -q

echo "=== 5. Directorios de datos ==="
sudo -u "$APP_USER" mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/static"

echo "=== 6. Servicios systemd ==="
cp /tmp/bot-trading-bot.service  /etc/systemd/system/
cp /tmp/bot-trading-api.service  /etc/systemd/system/
systemctl daemon-reload
systemctl enable bot-trading-bot bot-trading-api

echo "=== 7. Nginx ==="
cp /tmp/bot-trading-nginx.conf /etc/nginx/sites-available/bot-trading
ln -sf /etc/nginx/sites-available/bot-trading /etc/nginx/sites-enabled/bot-trading
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl enable nginx

echo "=== 8. Firewall ==="
ufw allow OpenSSH
ufw allow 80
ufw --force enable

echo ""
echo "✓ Setup completo."
echo "  Siguiente paso: sube el código y el .env a $APP_DIR"
echo "  Luego: systemctl start bot-trading-api bot-trading-bot"
