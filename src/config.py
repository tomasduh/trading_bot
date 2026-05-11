import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Entorno ──────────────────────────────────────────────────────────────────
TRADING_MODE = os.getenv("TRADING_MODE", "testnet")  # "testnet" | "live"
IS_TESTNET = TRADING_MODE == "testnet"

# ── Credenciales ─────────────────────────────────────────────────────────────
if IS_TESTNET:
    API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", "")
    API_SECRET = os.getenv("BINANCE_TESTNET_SECRET", "")
else:
    API_KEY = os.getenv("BINANCE_API_KEY", "")
    API_SECRET = os.getenv("BINANCE_SECRET", "")

# ── Pares y timeframe ────────────────────────────────────────────────────────
CRYPTO_SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
]

# Stocks de Alpaca — solo opera en horario de mercado (9:30-16:00 ET lun-vie)
STOCK_SYMBOLS = [
    "AAPL",
    "NVDA",
    "TSLA",
    "SPY",
    "NU",
    "MSFT",
    "META",
]

SYMBOLS = CRYPTO_SYMBOLS  # legacy
SYMBOL  = CRYPTO_SYMBOLS[0]
TIMEFRAME    = "30m"
# 400 velas: suficiente para EMA200 (warmup 200) + 200 velas con todos los indicadores
CANDLES_LIMIT = 400

# ── Indicadores ───────────────────────────────────────────────────────────────
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND_FAST = 21    # tendencia rápida (también usable en testnet con pocas velas)
EMA_TREND_SLOW = 50    # tendencia de medio plazo
# Nota: en mainnet con historia completa, EMA50/200 sería ideal. Binance testnet
# solo entrega ~120 velas, por lo que mantenemos warmup ≤ 50.
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BBANDS_PERIOD = 20
BBANDS_STD = 2.0
ADX_PERIOD = 14
ADX_THRESHOLD = 20     # ADX > 20 indica tendencia con fuerza

# ── Estrategia ────────────────────────────────────────────────────────────────
MIN_SIGNAL_SCORE = 2          # mínimo de condiciones para disparar señal
USE_TREND_FILTER = True       # solo BUY en tendencia alcista, SELL en bajista
DISABLE_SIGNAL_EXIT = False   # si True: solo cierra por SL/TP/Trailing, no por SELL signal
SIGNAL_EXIT_ONLY_IN_LOSS = True  # si True: SELL signal solo cierra si el trade está en pérdida real
SIGNAL_EXIT_LOSS_BUFFER_PCT = 0.002  # buffer: SIGNAL cierra solo si precio < entrada * (1-buffer) (0.2%)

# ── Gestión de riesgo ─────────────────────────────────────────────────────────
RISK_PER_TRADE = 0.01        # 1% del capital por trade (aplicado por símbolo)
STOP_LOSS_PCT = 0.02         # stop loss 2% bajo el precio de entrada
TAKE_PROFIT_PCT = 0.04       # take profit 4% sobre el precio de entrada (ratio 2:1)
MAX_OPEN_TRADES = 1          # máximo 1 posición abierta por símbolo

# Trailing stop: el optimizer demostró que con SL 2% / TP 4% el TS corta trades
# prematuramente. Desactivado tras backtest 90d × 4 símbolos (variante F ganadora).
USE_TRAILING_STOP   = False
TRAILING_ACTIVATE_PCT = 0.015
TRAILING_DISTANCE_PCT = 0.01

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "data" / "trades.db"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── Fees y slippage (realismo en backtest) ────────────────────────────────────
# Binance spot: 0.1% por lado → 0.2% round-trip.
# Alpaca paper: sin comisión, pero slippage estimado conservador.
FEE_PCT: dict = {
    "BTC/USDT": 0.001,   # 0.1% Binance spot
    "ETH/USDT": 0.001,
    "SOL/USDT": 0.001,
    "BNB/USDT": 0.001,
    # Stocks Alpaca: 0 (comisión cero)
}
SLIPPAGE_PCT = 0.0005    # 0.05% estimado conservador por orden (ambos lados)

# ── Watchdog externo ──────────────────────────────────────────────────────────
# URL de healthchecks.io — el bot hace ping al final de cada ciclo.
# Si falta el ping N minutos, healthchecks.io manda alerta.
# Dejar vacío para deshabilitar.
HC_PING_URL = os.getenv("HC_PING_URL", "")

# ── Loop ───────────────────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS = 60 * 30  # cada 30 minutos (igual que el timeframe)
