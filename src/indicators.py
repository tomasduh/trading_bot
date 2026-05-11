import pandas as pd
import pandas_ta as ta
from src import config


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega todas las columnas de indicadores al DataFrame de velas."""
    df = df.copy()

    # ── EMAs ──────────────────────────────────────────────────────────────────
    df["ema_fast"] = ta.ema(df["close"], length=config.EMA_FAST)
    df["ema_slow"] = ta.ema(df["close"], length=config.EMA_SLOW)

    # ── RSI ───────────────────────────────────────────────────────────────────
    df["rsi"] = ta.rsi(df["close"], length=config.RSI_PERIOD)

    # ── MACD ──────────────────────────────────────────────────────────────────
    macd = ta.macd(
        df["close"],
        fast=config.MACD_FAST,
        slow=config.MACD_SLOW,
        signal=config.MACD_SIGNAL,
    )
    if macd is not None:
        df["macd"]        = macd[f"MACD_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"]
        df["macd_signal"] = macd[f"MACDs_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"]
        df["macd_hist"]   = macd[f"MACDh_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"]
    else:
        df["macd"] = df["macd_signal"] = df["macd_hist"] = float("nan")

    # ── Bollinger Bands ────────────────────────────────────────────────────────
    bb = ta.bbands(df["close"], length=config.BBANDS_PERIOD, std=config.BBANDS_STD)
    std = config.BBANDS_STD
    period = config.BBANDS_PERIOD
    if bb is not None:
        # pandas-ta 0.4 genera: BBU_20_2.0_2.0 (period_std_std)
        df["bb_upper"] = bb[f"BBU_{period}_{std}_{std}"]
        df["bb_mid"]   = bb[f"BBM_{period}_{std}_{std}"]
        df["bb_lower"] = bb[f"BBL_{period}_{std}_{std}"]
    else:
        df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = float("nan")

    # ── EMA largo plazo (filtro de tendencia) ─────────────────────────────────
    df["ema_trend_fast"] = ta.ema(df["close"], length=config.EMA_TREND_FAST)
    df["ema_trend_slow"] = ta.ema(df["close"], length=config.EMA_TREND_SLOW)

    # ── ADX (fuerza de la tendencia) ───────────────────────────────────────────
    try:
        adx = ta.adx(df["high"], df["low"], df["close"], length=config.ADX_PERIOD)
        df["adx"] = adx[f"ADX_{config.ADX_PERIOD}"]
    except (KeyError, TypeError):
        df["adx"] = 0.0

    # ── ATR (Average True Range) — para stops dinámicos por volatilidad ───────
    try:
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=config.ATR_PERIOD)
    except Exception:
        df["atr"] = float("nan")

    return df


def latest(df: pd.DataFrame) -> pd.Series:
    """Devuelve la última fila con todos los indicadores calculados."""
    df = add_all(df)
    return df.dropna().iloc[-1]
