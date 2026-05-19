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
    # ⚠️ ADA/DOT/AVAX/LINK/POL desactivados: 22 symbols saturaba la VM 512MB
    # (OOM kill repetidos). Reactivar cuando se upgradee a 1GB o se optimice más.
    # "ADA/USDT",
    # "DOT/USDT",
    # "AVAX/USDT",
    # "LINK/USDT",
    # "POL/USDT",
]

# Stocks de Alpaca — solo opera en horario de mercado (9:30-16:00 ET lun-vie)
STOCK_SYMBOLS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "TSLA",
    "SPY",
    # ⚠️ META/GOOGL/AMZN/AMD/QQQ/COIN/NU desactivados: reducción de memoria
    # VM 512MB con 22 symbols sufría OOM kills repetidos. Reactivar con más RAM.
    # "META",
    # "GOOGL",
    # "AMZN",
    # "AMD",
    # "QQQ",
    # "COIN",
    # "NU",
]

SYMBOLS = CRYPTO_SYMBOLS  # legacy
SYMBOL  = CRYPTO_SYMBOLS[0]
# Config "Hybrid C" (validada por backtest 60d × 5 cryptos):
#   - TIMEFRAME=15m → 2× más oportunidades vs 30m
#   - MIN_SIGNAL_SCORE=2 → mantiene calidad de señal
#   - USE_MTF=True → filtro macro 4h bloquea BUY en bear market
#   - SL/TP 2%/4% conservadores → no se trigger por ruido
# Backtest comparativo: hybrid_C dio -6.20% PnL en 60d (vs -3.59% conservative
# y -11.52% acelerada agresiva). 19% más trades que conservative, 7.36% max DD.
TIMEFRAME    = "15m"
# 200 velas × 15m = 50h de mercado. Suficiente para todos los indicadores:
# - EMA_TREND_SLOW=50 → warmup 50, quedan 150 velas usables
# - RSI 14, MACD 26, BB 20, ADX 14, ATR 14 → todos OK
# Reducido de 400→200 para bajar memoria (era ~237MB RSS → 512MB OOM)
CANDLES_LIMIT = 100   # reducido 200→100 para aliviar memoria VM 512MB

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
# Hybrid C: score=2 mantiene la calidad de señal (mejor WR que score=1).
# El backtest mostró que score=1 deteriora WR significativamente (18% vs 27%).
MIN_SIGNAL_SCORE = 2          # mínimo de condiciones para disparar señal
USE_TREND_FILTER = True       # solo BUY en tendencia alcista, SELL en bajista
DISABLE_SIGNAL_EXIT = False   # si True: solo cierra por SL/TP/Trailing, no por SELL signal
SIGNAL_EXIT_ONLY_IN_LOSS = True  # si True: SELL signal solo cierra si el trade está en pérdida real
SIGNAL_EXIT_LOSS_BUFFER_PCT = 0.002  # buffer: SIGNAL cierra solo si precio < entrada * (1-buffer) (0.2%)

# ── Gestión de riesgo ─────────────────────────────────────────────────────────
# Hybrid C: stops conservadores (2%/4%) según backtest. Los stops ceñidos (1.5%/3%)
# se trigger por ruido normal del mercado y empeoran PnL.
RISK_PER_TRADE = 0.01        # 1% del capital por trade (aplicado por símbolo)
STOP_LOSS_PCT = 0.02         # stop loss 2% (conservador, validado por backtest)
TAKE_PROFIT_PCT = 0.04       # take profit 4% (ratio 2:1)
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
# VALIDADO POR BACKTEST: hybrid_C (con MTF on) dio -6.20% PnL vs -8.28% sin MTF
# en 60d. El filtro macro reduce trades pero mejora PnL en bear markets.
USE_MTF         = False        # ⚠️ TEMPORALMENTE OFF — la VM 512MB se saturaba
                              # con 22 símbolos × 2 fetches (15m + 4h). Reactivar
                              # cuando upgrademos memoria o reduzcamos símbolos.
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
# 10 símbolos × CANDLES_LIMIT=100 es mucho más ligero que los 22×200 anteriores.
# Mantenemos 10min para darle holgura de CPU. Con más RAM podríamos bajar a 5min.
LOOP_INTERVAL_SECONDS = 60 * 10  # cada 10 minutos
