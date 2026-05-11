"""
Backtester para la estrategia.

Uso CLI:
    python -m src.backtest BTC/USDT 30m 90      # 90 días
    python -m src.backtest --all                  # todos los símbolos crypto

Métricas que devuelve:
    - Total trades, win rate, PnL acumulado
    - Drawdown máximo, ratio profit/loss, Sharpe ratio (simplificado)
    - Distribución de razones de salida (SL/TP/SIGNAL)
    - Equity curve para graficar

CRÍTICO: la estrategia ya usa iloc[-2] (vela cerrada), por lo que NO hay
look-ahead bias. Cada decisión usa solo datos de velas que YA cerraron.
"""
from __future__ import annotations

import argparse
import json
import math
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from src import config, indicators, strategy, risk_manager

logger = logging.getLogger("backtest")

REPORTS_DIR = config.BASE_DIR / "data" / "backtests"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ── Modelo de trade simulado ──────────────────────────────────────────────────

@dataclass
class SimTrade:
    symbol: str
    entry_time: datetime
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    highest_price: float = 0.0   # tracking para trailing stop
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # SL | TP | SIGNAL | TRAILING_STOP
    pnl_usdt: float = 0.0
    pnl_pct: float = 0.0


# ── Carga de datos históricos ─────────────────────────────────────────────────

def fetch_history(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """
    Descarga ohlcv históricos vía ccxt (paginado para soportar miles de velas).
    Funciona en modo public (no requiere keys).
    """
    import ccxt
    # Siempre usamos mainnet para backtesting (datos públicos, no auth necesaria)
    # El testnet solo tiene historia limitada y errática.
    ex = ccxt.binance({"enableRateLimit": True, "timeout": 20000})

    tf_seconds = ex.parse_timeframe(timeframe)  # segundos por vela
    since = ex.milliseconds() - days * 24 * 60 * 60 * 1000

    all_rows = []
    while True:
        rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not rows:
            break
        all_rows.extend(rows)
        last_ts = rows[-1][0]
        if len(rows) < 1000:
            break
        since = last_ts + tf_seconds * 1000

    df = pd.DataFrame(all_rows, columns=["timestamp","open","high","low","close","volume"])
    df = df.drop_duplicates(subset="timestamp")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    return df


# ── Loop de backtest ──────────────────────────────────────────────────────────

def run_backtest(symbol: str, timeframe: str = "30m",
                 days: int = 90, capital: float = 10_000.0,
                 df_preloaded: Optional[pd.DataFrame] = None) -> dict:
    """
    Simula la estrategia sobre N días de historia.
    Cada vela cerrada se evalúa como si el bot estuviera ejecutándose en ese momento.

    Si se pasa df_preloaded, no descarga datos (útil para walk-forward).
    """
    if df_preloaded is not None:
        df = df_preloaded
        logger.info(f"Usando df pre-cargado: {len(df)} velas | {symbol}")
    else:
        logger.info(f"Descargando {days} días de {symbol} en {timeframe}...")
        try:
            df = fetch_history(symbol, timeframe, days)
        except Exception as e:
            return {"symbol": symbol, "timeframe": timeframe, "days": days,
                    "error": f"fetch falló: {e}"}

    if df.empty or len(df) < 250:
        return {"symbol": symbol, "timeframe": timeframe, "days": days,
                "error": f"datos insuficientes ({len(df)} velas)"}

    logger.info(f"Velas: {len(df)} | rango: {df.index[0]} → {df.index[-1]}")

    # Fee round-trip: comisión del exchange + slippage estimado
    # (0 para stocks Alpaca que no tienen comisión)
    fee_pct = config.FEE_PCT.get(symbol, 0.0) + config.SLIPPAGE_PCT

    # Pre-calculamos todos los indicadores una sola vez
    df_ind = indicators.add_all(df)

    # Necesitamos warmup para los indicadores largos (EMA200)
    warmup = max(config.EMA_TREND_SLOW + 5, 50)

    trades: list[SimTrade] = []
    open_trade: Optional[SimTrade] = None
    equity_curve: list[tuple[datetime, float]] = []
    equity = capital

    for i in range(warmup, len(df_ind)):
        # En el momento i, el bot ve velas [0..i]. strategy.evaluate usa iloc[-2]
        # internamente (vela cerrada), así que pasamos la ventana completa.
        window = df_ind.iloc[: i + 1]
        signal = strategy.evaluate(window)

        live_price   = float(df_ind.iloc[i]["open"])
        high         = float(df_ind.iloc[i]["high"])
        low          = float(df_ind.iloc[i]["low"])
        close        = float(df_ind.iloc[i]["close"])
        current_time = df_ind.index[i].to_pydatetime()

        # 1. Si hay trade abierto, evaluar exits en orden de probabilidad temporal:
        #    open → high/low → close. Asumimos peor caso (SL antes que TP si ambos
        #    se tocan dentro de la vela).
        if open_trade:
            # Actualizar highest_price para trailing stop ANTES de chequear
            if high > open_trade.highest_price:
                open_trade.highest_price = high

            exit_price  = None
            exit_reason = None

            # 1a. Stop Loss fijo (peor caso primero)
            if low <= open_trade.stop_loss:
                exit_price  = open_trade.stop_loss
                exit_reason = "STOP_LOSS"

            # 1b. Trailing Stop (si el TS calculado fue tocado por low de la vela)
            elif config.USE_TRAILING_STOP:
                ts_price = risk_manager.trailing_stop_price(
                    open_trade.entry_price, open_trade.highest_price)
                if ts_price is not None and low <= ts_price:
                    exit_price  = ts_price
                    exit_reason = "TRAILING_STOP"

            # 1c. Take Profit fijo
            if exit_price is None and high >= open_trade.take_profit:
                exit_price  = open_trade.take_profit
                exit_reason = "TAKE_PROFIT"

            # 1d. SELL signal cierra al close de la vela
            # Con SIGNAL_EXIT_ONLY_IN_LOSS: solo cierra si el trade está en pérdida real (con buffer)
            if exit_price is None and signal.type == "SELL":
                loss_threshold = open_trade.entry_price * (1 - config.SIGNAL_EXIT_LOSS_BUFFER_PCT)
                if not config.SIGNAL_EXIT_ONLY_IN_LOSS or close < loss_threshold:
                    exit_price  = close
                    exit_reason = "SIGNAL"

            if exit_price is not None:
                pnl_usdt, pnl_pct = risk_manager.pnl(
                    open_trade.entry_price, exit_price, open_trade.quantity, fee_pct)
                open_trade.exit_time   = current_time
                open_trade.exit_price  = exit_price
                open_trade.exit_reason = exit_reason
                open_trade.pnl_usdt    = pnl_usdt
                open_trade.pnl_pct     = pnl_pct
                trades.append(open_trade)
                equity += pnl_usdt
                open_trade = None
                equity_curve.append((current_time, equity))

        # 2. Si no hay trade y hay BUY, abre al open de la vela
        if open_trade is None and signal.type == "BUY":
            qty = risk_manager.position_size(equity, live_price, symbol)
            if qty <= 0:
                continue
            atr_val = getattr(signal, "atr", 0.0) or 0.0
            sl, tp  = risk_manager.get_sl_tp(live_price, atr_val, symbol)
            open_trade = SimTrade(
                symbol=symbol,
                entry_time=current_time,
                entry_price=live_price,
                quantity=qty,
                stop_loss=sl,
                take_profit=tp,
                highest_price=live_price,
            )

    # Cerrar trade abierto al final con último precio
    if open_trade:
        last_price = float(df_ind.iloc[-1]["close"])
        pnl_usdt, pnl_pct = risk_manager.pnl(
            open_trade.entry_price, last_price, open_trade.quantity, fee_pct)
        open_trade.exit_time = df_ind.index[-1].to_pydatetime()
        open_trade.exit_price = last_price
        open_trade.exit_reason = "END_OF_BACKTEST"
        open_trade.pnl_usdt = pnl_usdt
        open_trade.pnl_pct = pnl_pct
        trades.append(open_trade)
        equity += pnl_usdt

    return _summarize(symbol, timeframe, days, capital, equity, trades, equity_curve, df_ind, fee_pct)


def _summarize(symbol, timeframe, days, capital, final_equity, trades, equity_curve, df,
               fee_pct: float = 0.0) -> dict:
    if not trades:
        return {
            "symbol": symbol, "timeframe": timeframe, "days": days,
            "capital_initial": capital, "capital_final": final_equity,
            "total_trades": 0, "error": "ningún trade ejecutado en este periodo",
        }

    wins   = [t for t in trades if t.pnl_usdt > 0]
    losses = [t for t in trades if t.pnl_usdt <= 0]

    pnl_total = sum(t.pnl_usdt for t in trades)
    win_rate  = len(wins) / len(trades) * 100

    avg_win  = sum(t.pnl_usdt for t in wins)   / len(wins)   if wins   else 0
    avg_loss = sum(t.pnl_usdt for t in losses) / len(losses) if losses else 0
    profit_factor = (
        sum(t.pnl_usdt for t in wins) / abs(sum(t.pnl_usdt for t in losses))
        if losses and sum(t.pnl_usdt for t in losses) != 0 else float("inf")
    )

    # Drawdown máximo
    peak = capital
    max_dd_pct = 0.0
    eq = capital
    for t in trades:
        eq += t.pnl_usdt
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        max_dd_pct = max(max_dd_pct, dd)

    # Sharpe simplificado (returns por trade, anualizado asumiendo 252*N trades/año)
    returns = [t.pnl_pct for t in trades]
    if len(returns) > 1:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(var)
        sharpe = (mean_r / std_r) * math.sqrt(len(returns)) if std_r > 0 else 0
    else:
        sharpe = 0

    exit_reasons: dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    avg_dur = sum((t.exit_time - t.entry_time).total_seconds() / 60
                  for t in trades) / len(trades)

    # Buy & Hold benchmark
    bh_return = (df.iloc[-1]["close"] / df.iloc[0]["close"] - 1) * 100

    return {
        "symbol":          symbol,
        "timeframe":       timeframe,
        "days":            days,
        "candles_used":    len(df),
        "capital_initial": round(capital, 2),
        "capital_final":   round(final_equity, 2),
        "total_return_pct":round((final_equity - capital) / capital * 100, 2),
        "buy_hold_pct":    round(bh_return, 2),
        "total_trades":    len(trades),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate_pct":    round(win_rate, 2),
        "pnl_total_usdt":  round(pnl_total, 2),
        "avg_win_usdt":    round(avg_win, 2),
        "avg_loss_usdt":   round(avg_loss, 2),
        "profit_factor":   round(profit_factor, 2) if profit_factor != float("inf") else "∞",
        "max_drawdown_pct":round(max_dd_pct, 2),
        "sharpe_ratio":    round(sharpe, 2),
        "avg_duration_min":round(avg_dur, 1),
        "fee_pct_used":    round(fee_pct * 100, 4),   # % por orden (entry + exit)
        "atr_stops_used":  config.USE_ATR_STOPS,
        "exit_reasons":    exit_reasons,
        "trades_sample":   [
            {**asdict(t),
             "entry_time": t.entry_time.isoformat() if t.entry_time else None,
             "exit_time":  t.exit_time.isoformat() if t.exit_time else None}
            for t in trades[:20]
        ],
    }


def save_report(report: dict) -> Path:
    sym = report["symbol"].replace("/", "-")
    ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"{sym}_{report['timeframe']}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    return path


def print_summary(r: dict):
    print("\n" + "=" * 60)
    print(f"BACKTEST {r['symbol']} {r['timeframe']} - {r['days']} dias")
    print("=" * 60)
    if "error" in r:
        print(f"  ERROR: {r['error']}")
        return
    tag = "[OK]" if r["pnl_total_usdt"] >= 0 else "[LOSS]"
    print(f"  {tag} Trades: {r['total_trades']} | Win rate: {r['win_rate_pct']}%")
    print(f"  PnL: {r['pnl_total_usdt']:+,.2f} USDT ({r['total_return_pct']:+.2f}%)")
    print(f"  Buy & Hold: {r['buy_hold_pct']:+.2f}% (referencia)")
    print(f"  Profit factor: {r['profit_factor']}")
    print(f"  Max drawdown: {r['max_drawdown_pct']}%")
    print(f"  Sharpe: {r['sharpe_ratio']}")
    print(f"  Duracion media: {r['avg_duration_min']:.0f} min")
    print(f"  Exits: {r['exit_reasons']}")
    print(f"  Fee/slippage por trade: {r.get('fee_pct_used', 0):.4f}% × 2 lados")
    print("=" * 60)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest del bot de trading")
    parser.add_argument("symbol", nargs="?", help="ej: BTC/USDT")
    parser.add_argument("timeframe", nargs="?", default="30m")
    parser.add_argument("days", nargs="?", type=int, default=90)
    parser.add_argument("--all", action="store_true", help="todos los símbolos crypto")
    parser.add_argument("--capital", type=float, default=10_000.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    symbols = config.CRYPTO_SYMBOLS if args.all else [args.symbol or config.CRYPTO_SYMBOLS[0]]

    for sym in symbols:
        report = run_backtest(sym, args.timeframe, args.days, args.capital)
        path = save_report(report)
        print_summary(report)
        print(f"  Reporte: {path}\n")


if __name__ == "__main__":
    main()
