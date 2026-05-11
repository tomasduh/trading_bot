"""
Optimizador de estrategia: prueba múltiples variantes vía backtest y reporta cuál
genera mejor PnL ajustado al riesgo (Sharpe, profit factor, drawdown).

Uso:
    python -m src.optimizer                         # 4 símbolos × 8 variantes
    python -m src.optimizer --days 60 --symbols BTC/USDT,ETH/USDT
"""
from __future__ import annotations

import argparse
import json
import logging
import copy
from datetime import datetime, timezone
from pathlib import Path
from itertools import product

from src import config, backtest

logger = logging.getLogger("optimizer")


# ── Variantes a probar ────────────────────────────────────────────────────────

VARIANTS: list[dict] = [
    # baseline (config actual)
    {"name": "baseline",
     "MIN_SIGNAL_SCORE": 2, "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
     "USE_TRAILING_STOP": True, "USE_TREND_FILTER": True,
     "_description": "config actual"},

    # A. No cerrar por SIGNAL — solo SL/TP/Trailing
    {"name": "A_no_signal_exit",
     "MIN_SIGNAL_SCORE": 2, "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
     "USE_TRAILING_STOP": True, "USE_TREND_FILTER": True,
     "DISABLE_SIGNAL_EXIT": True,
     "_description": "no cierra por SELL signal — solo SL/TP/TS"},

    # B. Score mínimo 3 (señales más fuertes)
    {"name": "B_score_3",
     "MIN_SIGNAL_SCORE": 3, "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
     "USE_TRAILING_STOP": True, "USE_TREND_FILTER": True,
     "_description": "score 3 (las 3 condiciones obligatorias)"},

    # C. TP más cerca con SL más estrecho (ratio 2:1 mantenido)
    {"name": "C_tight_sl_tp",
     "MIN_SIGNAL_SCORE": 2, "STOP_LOSS_PCT": 0.01, "TAKE_PROFIT_PCT": 0.02,
     "USE_TRAILING_STOP": True, "USE_TREND_FILTER": True,
     "_description": "SL 1% / TP 2% (más rápido)"},

    # D. TP lejano para capturar tendencias largas
    {"name": "D_wide_tp",
     "MIN_SIGNAL_SCORE": 2, "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.08,
     "USE_TRAILING_STOP": True, "USE_TREND_FILTER": True,
     "_description": "TP 8% (deja correr ganadores)"},

    # E. Sin filtro de tendencia
    {"name": "E_no_trend_filter",
     "MIN_SIGNAL_SCORE": 2, "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
     "USE_TRAILING_STOP": True, "USE_TREND_FILTER": False,
     "_description": "sin filtro de tendencia"},

    # F. Sin trailing stop
    {"name": "F_no_trailing",
     "MIN_SIGNAL_SCORE": 2, "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
     "USE_TRAILING_STOP": False, "USE_TREND_FILTER": True,
     "_description": "sin trailing stop"},

    # G. Combinación: no signal exit + TP lejano + trailing
    {"name": "G_let_winners_run",
     "MIN_SIGNAL_SCORE": 2, "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.10,
     "USE_TRAILING_STOP": True, "USE_TREND_FILTER": True,
     "DISABLE_SIGNAL_EXIT": True,
     "_description": "deja correr ganadores: TP 10% + sin signal exit + TS"},

    # H. Cambio quirúrgico: SIGNAL solo cierra si el trade está en pérdida
    {"name": "H_signal_exit_loss_only",
     "MIN_SIGNAL_SCORE": 2, "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
     "USE_TRAILING_STOP": False, "USE_TREND_FILTER": True,
     "SIGNAL_EXIT_ONLY_IN_LOSS": True,
     "_description": "SELL signal solo cierra trades en pérdida (deja correr ganadores)"},
]


# ── Aplicar variante ──────────────────────────────────────────────────────────

def _apply_variant(variant: dict) -> dict:
    """Sobrescribe config con la variante. Retorna snapshot del config previo."""
    snapshot = {}
    for key, val in variant.items():
        if key.startswith("_") or key == "name":
            continue
        if key == "DISABLE_SIGNAL_EXIT":
            # Lo manejamos vía monkey-patch del backtest (ver run_with_variant)
            continue
        if hasattr(config, key):
            snapshot[key] = getattr(config, key)
            setattr(config, key, val)
    return snapshot


def _restore_config(snapshot: dict):
    for key, val in snapshot.items():
        setattr(config, key, val)


# ── Wrapper con DISABLE_SIGNAL_EXIT ───────────────────────────────────────────

_ORIG_SIM_LOOP = None


def _patch_disable_signal_exit():
    """Modifica el backtest para ignorar SELL signals como exits."""
    import src.backtest as bt
    global _ORIG_SIM_LOOP
    if _ORIG_SIM_LOOP is None:
        _ORIG_SIM_LOOP = bt.run_backtest

    def patched_run(symbol, timeframe="30m", days=90, capital=10_000.0):
        # Antes de llamar al original, monkey-patcheamos el "if signal.type == SELL"
        # del loop. La forma limpia es duplicar el loop, pero usamos un flag.
        return _run_backtest_impl(symbol, timeframe, days, capital,
                                  disable_signal_exit=True)

    bt.run_backtest_no_signal_exit = patched_run


def _run_backtest_impl(symbol, timeframe, days, capital, disable_signal_exit=False,
                       signal_exit_only_in_loss=False):
    """Versión modificable del run_backtest que respeta disable_signal_exit."""
    import pandas as pd
    from src import indicators, strategy, risk_manager
    from src.backtest import fetch_history, SimTrade, _summarize

    try:
        df = fetch_history(symbol, timeframe, days)
    except Exception as e:
        return {"symbol": symbol, "timeframe": timeframe, "days": days,
                "error": f"fetch falló: {e}"}
    if df.empty or len(df) < 250:
        return {"symbol": symbol, "timeframe": timeframe, "days": days,
                "error": f"datos insuficientes ({len(df)} velas)"}

    df_ind = indicators.add_all(df)
    warmup = max(config.EMA_TREND_SLOW + 5, 50)

    trades = []
    open_trade = None
    equity_curve = []
    equity = capital

    for i in range(warmup, len(df_ind)):
        window = df_ind.iloc[: i + 1]
        signal = strategy.evaluate(window)

        live_price = float(df_ind.iloc[i]["open"])
        high       = float(df_ind.iloc[i]["high"])
        low        = float(df_ind.iloc[i]["low"])
        close      = float(df_ind.iloc[i]["close"])
        current_time = df_ind.index[i].to_pydatetime()

        if open_trade:
            if high > open_trade.highest_price:
                open_trade.highest_price = high

            exit_price = None
            exit_reason = None

            if low <= open_trade.stop_loss:
                exit_price, exit_reason = open_trade.stop_loss, "STOP_LOSS"
            elif config.USE_TRAILING_STOP:
                ts_price = risk_manager.trailing_stop_price(
                    open_trade.entry_price, open_trade.highest_price)
                if ts_price is not None and low <= ts_price:
                    exit_price, exit_reason = ts_price, "TRAILING_STOP"
            if exit_price is None and high >= open_trade.take_profit:
                exit_price, exit_reason = open_trade.take_profit, "TAKE_PROFIT"
            if exit_price is None and signal.type == "SELL" and not disable_signal_exit:
                # signal_exit_only_in_loss: solo cerrar si hay pérdida real (con buffer)
                loss_threshold = open_trade.entry_price * (1 - config.SIGNAL_EXIT_LOSS_BUFFER_PCT)
                if not signal_exit_only_in_loss or close < loss_threshold:
                    exit_price, exit_reason = close, "SIGNAL"

            if exit_price is not None:
                pnl_usdt, pnl_pct = risk_manager.pnl(
                    open_trade.entry_price, exit_price, open_trade.quantity)
                open_trade.exit_time = current_time
                open_trade.exit_price = exit_price
                open_trade.exit_reason = exit_reason
                open_trade.pnl_usdt = pnl_usdt
                open_trade.pnl_pct = pnl_pct
                trades.append(open_trade)
                equity += pnl_usdt
                open_trade = None
                equity_curve.append((current_time, equity))

        if open_trade is None and signal.type == "BUY":
            qty = risk_manager.position_size(equity, live_price, symbol)
            if qty <= 0:
                continue  # min_notional no alcanzado
            open_trade = SimTrade(
                symbol=symbol, entry_time=current_time, entry_price=live_price,
                quantity=qty,
                stop_loss=risk_manager.stop_loss_price(live_price, symbol),
                take_profit=risk_manager.take_profit_price(live_price, symbol),
                highest_price=live_price,
            )

    # Cerrar al final
    if open_trade:
        last_price = float(df_ind.iloc[-1]["close"])
        pnl_usdt, pnl_pct = risk_manager.pnl(
            open_trade.entry_price, last_price, open_trade.quantity)
        open_trade.exit_time = df_ind.index[-1].to_pydatetime()
        open_trade.exit_price = last_price
        open_trade.exit_reason = "END_OF_BACKTEST"
        open_trade.pnl_usdt = pnl_usdt
        open_trade.pnl_pct = pnl_pct
        trades.append(open_trade)
        equity += pnl_usdt

    return _summarize(symbol, timeframe, days, capital, equity, trades, equity_curve, df_ind)


# ── Score de cada variante ────────────────────────────────────────────────────

def _score(report: dict) -> float:
    """
    Métrica compuesta para rankear variantes.
    Penaliza drawdown y favorece profit factor + return.
    """
    if "error" in report:
        return -1e9
    pnl    = report.get("total_return_pct", 0)
    pf     = report.get("profit_factor", 0)
    if pf == "∞":
        pf = 5.0
    sharpe = report.get("sharpe_ratio", 0)
    dd     = report.get("max_drawdown_pct", 0)
    # Heurística: PnL absoluto + bonus por PF y Sharpe, penaliza DD severo
    return pnl + (pf - 1) * 5 + sharpe * 3 - max(0, dd - 10) * 0.5


# ── Run ───────────────────────────────────────────────────────────────────────

def run(symbols: list[str], days: int = 90, capital: float = 10_000.0):
    results: dict[str, dict] = {}  # variant_name -> {symbol -> report}

    for variant in VARIANTS:
        name = variant["name"]
        snap = _apply_variant(variant)
        results[name] = {"_meta": variant, "_total_score": 0, "symbols": {}}

        try:
            for sym in symbols:
                disable = variant.get("DISABLE_SIGNAL_EXIT", False)
                only_loss = variant.get("SIGNAL_EXIT_ONLY_IN_LOSS", False)
                report = _run_backtest_impl(sym, "30m", days, capital, disable, only_loss)
                results[name]["symbols"][sym] = report
                results[name]["_total_score"] += _score(report)
        finally:
            _restore_config(snap)

    return results


def print_table(results: dict, symbols: list[str]):
    print("\n" + "=" * 100)
    print(f"{'VARIANTE':<22} {'PnL%':>8} {'WR%':>6} {'PF':>5} {'DD%':>6} {'Trades':>7} {'Sharpe':>7}  Score")
    print("-" * 100)

    rows = []
    for name, data in results.items():
        meta = data["_meta"]
        # Promedios entre símbolos
        valid_reports = [r for r in data["symbols"].values() if "error" not in r]
        if not valid_reports:
            continue
        avg_pnl    = sum(r["total_return_pct"] for r in valid_reports) / len(valid_reports)
        avg_wr     = sum(r["win_rate_pct"]    for r in valid_reports) / len(valid_reports)
        avg_pf     = sum((r["profit_factor"] if r["profit_factor"] != "∞" else 5)
                          for r in valid_reports) / len(valid_reports)
        avg_dd     = sum(r["max_drawdown_pct"] for r in valid_reports) / len(valid_reports)
        avg_trades = sum(r["total_trades"]    for r in valid_reports) / len(valid_reports)
        avg_sharpe = sum(r["sharpe_ratio"]    for r in valid_reports) / len(valid_reports)
        score      = data["_total_score"]
        rows.append((name, avg_pnl, avg_wr, avg_pf, avg_dd, avg_trades, avg_sharpe, score, meta.get("_description","")))

    rows.sort(key=lambda r: -r[7])  # rank por score desc
    for r in rows:
        print(f"{r[0]:<22} {r[1]:>+7.2f} {r[2]:>5.1f} {r[3]:>5.2f} {r[4]:>5.2f} {r[5]:>7.0f} {r[6]:>7.2f}  {r[7]:>+6.1f}  | {r[8]}")

    print("=" * 100)
    if rows:
        print(f"\nGanador: {rows[0][0]} — {rows[0][8]}")
        print(f"PnL promedio: {rows[0][1]:+.2f}% sobre {len(symbols)} símbolos en 90 días")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default=",".join(config.CRYPTO_SYMBOLS))
    parser.add_argument("--capital", type=float, default=10_000.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)  # silenciar INFO de fetch
    syms = [s.strip() for s in args.symbols.split(",")]

    print(f"\nOptimizando estrategia sobre {len(syms)} símbolos × {len(VARIANTS)} variantes × {args.days} días...")
    print(f"Símbolos: {syms}")
    results = run(syms, args.days, args.capital)
    print_table(results, syms)

    # Guardar JSON
    out = config.BASE_DIR / "data" / "optimizer_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nReporte completo: {out}")


if __name__ == "__main__":
    main()
