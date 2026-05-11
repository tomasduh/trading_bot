"""
Estrategia multi-señal con filtro de tendencia.

FILTRO DE TENDENCIA (gating obligatorio si USE_TREND_FILTER=True):
  - BUY  permitido solo si EMA50 > EMA200 (mercado alcista) y ADX > 20 (con fuerza)
  - SELL permitido solo si EMA50 < EMA200 (mercado bajista) y ADX > 20

SEÑALES BUY (necesita 2 de 3):
  1. MACD line cruza por ENCIMA de signal line
  2. RSI < 55 y subiendo
  3. Precio rebota desde BB lower

SEÑALES SELL (necesita 2 de 3):
  1. MACD line cruza por DEBAJO de signal line
  2. RSI > 45 y bajando
  3. Precio toca BB upper

IMPORTANTE: usamos iloc[-2] (última vela CERRADA) para evitar look-ahead bias.
"""
from dataclasses import dataclass, field
import pandas as pd
from src import config
from src import indicators


@dataclass
class Signal:
    type: str
    reason: str
    price: float
    rsi: float
    ema_fast: float
    ema_slow: float
    macd_hist: float
    score: int = 0
    conditions: list = field(default_factory=list)
    trend: str = "neutral"     # "up" | "down" | "neutral"
    adx: float = 0.0


def _detect_trend(curr) -> tuple[str, float]:
    """Devuelve ('up'|'down'|'neutral', adx_value)."""
    ema_t_fast = float(curr.get("ema_trend_fast", 0) or 0)
    ema_t_slow = float(curr.get("ema_trend_slow", 0) or 0)
    adx        = float(curr.get("adx", 0) or 0)

    if ema_t_fast == 0 or ema_t_slow == 0:
        return "neutral", adx

    if adx < config.ADX_THRESHOLD:
        return "neutral", adx

    if ema_t_fast > ema_t_slow:
        return "up", adx
    if ema_t_fast < ema_t_slow:
        return "down", adx
    return "neutral", adx


def evaluate(df: pd.DataFrame) -> Signal:
    # Si los indicadores ya están calculados (caller pasó df_ind), evitamos recálculo.
    # Detección: presencia de columnas clave creadas por indicators.add_all.
    if "ema_fast" not in df.columns or "macd_hist" not in df.columns:
        df = indicators.add_all(df)
    df = df.dropna()

    # Necesitamos al menos EMA_TREND_SLOW + 2 velas para tener todos los indicadores
    min_len = max(4, config.EMA_TREND_SLOW + 2) if config.USE_TREND_FILTER else 4
    if len(df) < min_len:
        return Signal("NONE", "not enough data", 0, 0, 0, 0, 0)

    # Última vela CERRADA (anti look-ahead)
    curr = df.iloc[-2]
    prev = df.iloc[-3]

    price      = float(curr["close"])
    rsi        = float(curr["rsi"])
    rsi_prev   = float(prev["rsi"])
    macd       = float(curr["macd"])
    macd_sig   = float(curr["macd_signal"])
    macd_prev  = float(prev["macd"])
    msig_prev  = float(prev["macd_signal"])
    macd_hist  = float(curr["macd_hist"])
    ema_fast   = float(curr["ema_fast"])
    ema_slow   = float(curr["ema_slow"])
    bb_upper   = float(curr["bb_upper"])
    bb_lower   = float(curr["bb_lower"])
    bb_mid     = float(curr["bb_mid"])

    trend, adx = _detect_trend(curr)

    # ── Condiciones BUY ──────────────────────────────────────────────────────
    buy_conditions = []
    if macd_prev < msig_prev and macd > macd_sig:
        buy_conditions.append("MACD cross ↑")
    if rsi < 55 and rsi > rsi_prev:
        buy_conditions.append(f"RSI {rsi:.0f} subiendo")
    bb_range = bb_upper - bb_lower
    if bb_range > 0 and price < bb_mid and (price - bb_lower) / bb_range < 0.35:
        buy_conditions.append("precio cerca BB lower")

    # ── Condiciones SELL ─────────────────────────────────────────────────────
    sell_conditions = []
    if macd_prev > msig_prev and macd < macd_sig:
        sell_conditions.append("MACD cross ↓")
    if rsi > 45 and rsi < rsi_prev:
        sell_conditions.append(f"RSI {rsi:.0f} bajando")
    if bb_range > 0 and price > bb_mid and (bb_upper - price) / bb_range < 0.35:
        sell_conditions.append("precio cerca BB upper")

    threshold = config.MIN_SIGNAL_SCORE

    def _signal(typ: str, conds: list) -> Signal:
        return Signal(
            type=typ,
            reason=" | ".join(conds),
            price=price, rsi=rsi, ema_fast=ema_fast, ema_slow=ema_slow,
            macd_hist=macd_hist, score=len(conds),
            conditions=conds, trend=trend, adx=round(adx, 1),
        )

    # ── Decisión con filtro de tendencia ─────────────────────────────────────
    if len(buy_conditions) >= threshold:
        if config.USE_TREND_FILTER and trend != "up":
            return Signal("NONE",
                f"BUY bloqueado por filtro de tendencia (trend={trend}, adx={adx:.1f}) | activas: {', '.join(buy_conditions)}",
                price, rsi, ema_fast, ema_slow, macd_hist, 0,
                trend=trend, adx=round(adx, 1))
        return _signal("BUY", buy_conditions)

    if len(sell_conditions) >= threshold:
        if config.USE_TREND_FILTER and trend != "down":
            # SELL sin tendencia bajista solo se permite para CERRAR posiciones existentes
            # (el bot.py decide si actuar). Lo pasamos como señal pero etiquetada.
            return _signal("SELL", sell_conditions)
        return _signal("SELL", sell_conditions)

    all_conds = buy_conditions + sell_conditions
    reason = f"score insuficiente (buy={len(buy_conditions)}, sell={len(sell_conditions)})"
    if all_conds:
        reason += f" | activas: {', '.join(all_conds)}"

    return Signal(
        type="NONE", reason=reason,
        price=price, rsi=rsi, ema_fast=ema_fast, ema_slow=ema_slow,
        macd_hist=macd_hist, score=0,
        trend=trend, adx=round(adx, 1),
    )
