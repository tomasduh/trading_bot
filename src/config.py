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
    "XRP/USDT",
]

# Stocks de Alpaca — solo opera en horario de mercado (9:30-16:00 ET lun-vie)
STOCK_SYMBOLS = [
    # Big Tech (Mag 7)
    "AAPL",
    "MSFT",
    "META",
    "NVDA",
    "TSLA",
    "GOOGL",   # nuevo — Mag 7 que faltaba
    "AMZN",    # nuevo — Mag 7 que faltaba
    # Semis
    "AMD",     # nuevo — alternativa a NVDA (no 100% correlacionado)
    # ETFs
    "SPY",
    "QQQ",     # nuevo — ETF tech (complemento a SPY)
    # Crypto-correlated
    "COIN",    # nuevo — Coinbase, correlacionado con cripto
    # Otros
    "NU",
]

SYMBOLS = CRYPTO_SYMBOLS  # legacy
SYMBOL  = CRYPTO_SYMBOLS[0]
# Acelerado: 15m da 2× más velas que 30m → 2× más oportunidades de señales.
# Combinado con MIN_SIGNAL_SCORE=1 (de 2) y stops ajustados, esperamos ~5-8×
# más trades por semana — necesario para acumular 200 trades para ML en plazo
# razonable (3-4 semanas vs 100 semanas con la config conservadora anterior).
TIMEFRAME    = "15m"
# 400 velas × 15m = 100h de mercado; suficiente para EMA200 (warmup 50 con EMA_TREND_SLOW=50)
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
# Acelerado: con MIN_SIGNAL_SCORE=1 disparamos al cumplirse solo 1 de las 3
# condiciones. ~3× más signals que score=2. Compensado por el filtro de
# tendencia (USE_TREND_FILTER) y el circuit breaker (kill-switch en 3% DD).
MIN_SIGNAL_SCORE = 1          # mínimo de condiciones para disparar señal
USE_TREND_FILTER = True       # solo BUY en tendencia alcista, SELL en bajista
DISABLE_SIGNAL_EXIT = False   # si True: solo cierra por SL/TP/Trailing, no por SELL signal
SIGNAL_EXIT_ONLY_IN_LOSS = True  # si True: SELL signal solo cierra si el trade está en pérdida real
SIGNAL_EXIT_LOSS_BUFFER_PCT = 0.002  # buffer: SIGNAL cierra solo si precio < entrada * (1-buffer) (0.2%)

# ── Gestión de riesgo ─────────────────────────────────────────────────────────
# Acelerado: stops más cercanos → trades cierran más rápido (~25% menos hold
# time average) → más oportunidades de re-entrar. Ratio 2:1 SL/TP mantenido.
RISK_PER_TRADE = 0.01        # 1% del capital por trade (aplicado por símbolo)
STOP_LOSS_PCT = 0.015        # stop loss 1.5% (antes 2%)
TAKE_PROFIT_PCT = 0.03       # take profit 3% (antes 4%) — ratio 2:1 mantenido
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

# ── Multi-timeframe (Fase 2.2) ────────────────────────────────────────────────
# USE_MTF=True: añade confirmación de tendencia macro en 4h antes de abrir BUY.
# Si el 4h está en tendencia bajista → BUY bloqueado aunque el 30m diga BUY.
# Activar solo después de comparar con backtest (el filtro reduce trades pero
# mejora calidad si el mercado tiene tendencias claras en 4h).
USE_MTF         = False        # activar después de backtest comparativo
MTF_TIMEFRAME   = "4h"        # timeframe macro para confirmación de tendencia

# ── Cap global de exposición (Fase 4.3) ───────────────────────────────────────
# Acelerado: 8% permite hasta ~8 trades simultáneos (RISK_PER_TRADE=1% cada uno)
# en lugar de 5. Con 17 símbolos disponibles, este es el verdadero limitador
# de cuántos trades pueden coexistir y permite aprovechar más oportunidades.
MAX_TOTAL_EXPOSURE_PCT = 0.08  # 8% máximo del capital en riesgo simultáneo

# ── ATR-based stops (Fase 2.1) ────────────────────────────────────────────────
# Si USE_ATR_STOPS=True: SL y TP se calculan como múltiplos del ATR en lugar
# de porcentajes fijos. Más adaptativo: en activos volátiles el SL se ensancha,
# en activos tranquilos se estrecha. Mantiene ratio 2:1 (TP = 2× SL).
USE_ATR_STOPS      = False      # activar con backtest comparativo primero
ATR_PERIOD         = 14         # periodo del Average True Range
ATR_MULTIPLIER_SL  = 2.0       # SL = entry − ATR_MULT_SL × ATR(14)
ATR_MULTIPLIER_TP  = 4.0       # TP = entry + ATR_MULT_TP × ATR(14) → ratio 2:1

# ── Circuit breaker (Fase 4.2) ────────────────────────────────────────────────
CB_DAILY_DRAWDOWN_PCT   = 3.0   # pausa si pierde >3% del capital en el día
CB_TOTAL_LOSS_PCT       = 10.0  # pausa si pierde >10% del capital inicial
CB_CONSECUTIVE_LOSSES   = 5     # pausa tras 5 trades perdedores consecutivos

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
# Acelerado: timeframe=15m → ciclo=5min mantiene el ratio 1:3 (3 chequeos de
# SL/TP por cada vela cerrada). SL/TP, circuit breaker y reconciliador reaccionan
# 2× más rápido que con la config anterior (10min/30m=1:3 → 5min/15m=1:3 igual
# ratio, pero menor latencia absoluta).
LOOP_INTERVAL_SECONDS = 60 * 5  # cada 5 minutos
