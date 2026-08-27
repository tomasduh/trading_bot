"""
Compara configs específicas vía backtest:
  1. Baseline (config actual)
  2. ATR stops ON vs OFF
  3. Multi-Timeframe (MTF 4h) ON vs OFF

Uso:
    python -m src.compare_configs --days 60
    python -m src.compare_configs --days 90 --symbols BTC/USDT,ETH/USDT

Imprime tabla comparativa y guarda reporte JSON.
"""
from __future__ import annotations

import argparse
import json
import logging
import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from src import config, indicators, strategy, risk_manager
from src.backtest import fetch_history, SimTrade, _summarize, REPORTS_DIR

logger = logging.getLogger("compare_configs")


# ── Configs a comparar ────────────────────────────────────────────────────────

CONFIGS = [
    # Referencia (anchors)
    {
        "name": "conservative",
        "_description": "REFERENCIA — Config CONSERVADORA (TF=30m, score=2, SL=2%/4%)",
        "TIMEFRAME": "30m", "MIN_SIGNAL_SCORE": 2,
        "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
        "USE_ATR_STOPS": False, "USE_MTF": False,
    },
    # Híbridos — buscando el sweet spot
    {
        "name": "hybrid_A_score1_30m",
        "_description": "HÍBRIDO A — solo relajar score (TF=30m, score=1, SL=2%/4%)",
        "TIMEFRAME": "30m", "MIN_SIGNAL_SCORE": 1,
        "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
        "USE_ATR_STOPS": False, "USE_MTF": False,
    },
    {
        "name": "hybrid_B_15m_score2",
        "_description": "HÍBRIDO B — solo acelerar TF (TF=15m, score=2, SL=2%/4%)",
        "TIMEFRAME": "15m", "MIN_SIGNAL_SCORE": 2,
        "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
        "USE_ATR_STOPS": False, "USE_MTF": False,
    },
    {
        "name": "hybrid_C_15m_score2_mtf",
        "_description": "HÍBRIDO C — TF rápido + MTF como filtro de calidad (15m+score=2+MTF on)",
        "TIMEFRAME": "15m", "MIN_SIGNAL_SCORE": 2,
        "STOP_LOSS_PCT": 0.02, "TAKE_PROFIT_PCT": 0.04,
        "USE_ATR_STOPS": False, "USE_MTF": True,
    },
    {
        "name": "hybrid_D_15m_score2_tight",
        "_description": "HÍBRIDO D — TF rápido + stops moderados (15m+score=2, SL=1.75%/3.5%)",
        "TIMEFRAME": "15m", "MIN_SIGNAL_SCORE": 2,
        "STOP_LOSS_PCT": 0.0175, "TAKE_PROFIT_PCT": 0.035,
        "USE_ATR_STOPS": False, "USE_MTF": False,
    },
    {
        "name": "current_accelerated",
        "_description": "REFERENCIA — Config ACELERADA actual (TF=15m, score=1, SL=1.5%/3%)",
        "TIMEFRAME": "15m", "MIN_SIGNAL_SCORE": 1,
        "STOP_LOSS_PCT": 0.015, "TAKE_PROFIT_PCT": 0.03,
        "USE_ATR_STOPS": False, "USE_MTF": False,
    },
]


# ── Aplicar config temporalmente ──────────────────────────────────────────────

def _apply(cfg: dict) -> dict:
    snapshot = {}
    for k, v in cfg.items():
        if k.startswith("_") or k == "name":
            continue
        if hasattr(config, k):
            snapshot[k] = getattr(config, k)
            setattr(config, k, v)
    return snapshot


def _restore(snapshot: dict):
    for k, v in snapshot.items():
        setattr(config, k, v)


# ── Backtest engine con MTF integrado ─────────────────────────────────────────

def _run_backtest(symbol: str, days: int, capital: float = 10_000.0,
                  use_mtf: bool = False) -> dict:
    """Versión local del backtest con soporte MTF (fetch 4h paralelo)."""
    tf = config.TIMEFRAME

    try:
        df = fetch_history(symbol, tf, days)
    except Exception as e:
        return {"symbol": symbol, "error": f"fetch {tf} falló: {e}"}

    # Stocks operan 6.5h/dia vs 24h crypto — menos velas por dia. Con 30d+ de historia
    # y 15m TF, stocks dan ~570 velas (suficiente). Umbral conservador: 80 velas.
    min_candles = 80 if "/" not in symbol else 250
    if df.empty or len(df) < min_candles:
        return {"symbol": symbol, "error": f"datos insuficientes ({len(df)})"}

    # Si MTF: cargar también 4h
    df_macro = None
    if use_mtf:
        try:
            df_macro = fetch_history(symbol, "4h", days)
            df_macro = indicators.add_all(df_macro)
        except Exception as e:
            logger.warning(f"MTF fetch falló para {symbol}: {e}")
            df_macro = None

    fee_pct = config.FEE_PCT.get(symbol, 0.0) + config.SLIPPAGE_PCT
    df_ind = indicators.add_all(df)
    warmup = max(config.EMA_TREND_SLOW + 5, 50)

    trades, open_trade = [], None
    equity_curve, equity = [], capital

    for i in range(warmup, len(df_ind)):
        window = df_ind.iloc[:i+1]

        if use_mtf and df_macro is not None:
            # Encontrar el bar 4h más reciente <= ts actual
            current_ts = window.index[-1]
            macro_slice = df_macro[df_macro.index <= current_ts]
            if len(macro_slice) >= 2:
                signal = strategy.evaluate_mtf(window, macro_slice)
            else:
                signal = strategy.evaluate(window)
        else:
            signal = strategy.evaluate(window)

        live_price = float(df_ind.iloc[i]["open"])
        high       = float(df_ind.iloc[i]["high"])
        low        = float(df_ind.iloc[i]["low"])
        close      = float(df_ind.iloc[i]["close"])
        current_time = df_ind.index[i].to_pydatetime()

        if open_trade:
            exit_price = None
            exit_reason = None

            if low <= open_trade.stop_loss:
                exit_price, exit_reason = open_trade.stop_loss, "STOP_LOSS"
            if exit_price is None and high >= open_trade.take_profit:
                exit_price, exit_reason = open_trade.take_profit, "TAKE_PROFIT"
            if exit_price is None and signal.type == "SELL":
                loss_threshold = open_trade.entry_price * (1 - config.SIGNAL_EXIT_LOSS_BUFFER_PCT)
                if not config.SIGNAL_EXIT_ONLY_IN_LOSS or close < loss_threshold:
                    exit_price, exit_reason = close, "SIGNAL"

            if exit_price is not None:
                pnl_usdt, pnl_pct = risk_manager.pnl(
                    open_trade.entry_price, exit_price, open_trade.quantity, fee_pct)
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
                continue
            atr_val = getattr(signal, "atr", 0.0) or 0.0
            sl, tp = risk_manager.get_sl_tp(live_price, atr_val, symbol)
            open_trade = SimTrade(
                symbol=symbol, entry_time=current_time, entry_price=live_price,
                quantity=qty, stop_loss=sl, take_profit=tp,
                highest_price=live_price,
            )

    # cerrar último al fin del backtest
    if open_trade:
        last = float(df_ind.iloc[-1]["close"])
        pnl_usdt, pnl_pct = risk_manager.pnl(
            open_trade.entry_price, last, open_trade.quantity, fee_pct)
        open_trade.exit_time = df_ind.index[-1].to_pydatetime()
        open_trade.exit_price = last
        open_trade.exit_reason = "END"
        open_trade.pnl_usdt = pnl_usdt
        open_trade.pnl_pct = pnl_pct
        trades.append(open_trade)
        equity += pnl_usdt

    return _summarize(symbol, tf, days, capital, equity, trades, equity_curve, df_ind, fee_pct)


def _score(report: dict) -> float:
    """Métrica compuesta: PnL + bonus PF/Sharpe - penalty DD."""
    if "error" in report or report.get("total_trades", 0) == 0:
        return -1e9
    pnl = report.get("total_return_pct", 0)
    pf = report.get("profit_factor", 0)
    if pf == "∞":
        pf = 5.0
    sharpe = report.get("sharpe_ratio", 0)
    dd = report.get("max_drawdown_pct", 0)
    return pnl + (pf - 1) * 3 + sharpe * 2 - max(0, dd - 5) * 0.5


# ── Run ───────────────────────────────────────────────────────────────────────

def run_comparison(symbols: list[str], days: int = 60, capital: float = 10_000.0) -> dict:
    results = {}

    for cfg in CONFIGS:
        name = cfg["name"]
        logger.info(f"\n{'='*70}\nRunning: {name} — {cfg['_description']}\n{'='*70}")
        snap = _apply(cfg)
        results[name] = {"_meta": cfg, "_total_score": 0, "symbols": {}}
        try:
            for sym in symbols:
                use_mtf = cfg.get("USE_MTF", False)
                report = _run_backtest(sym, days, capital, use_mtf=use_mtf)
                results[name]["symbols"][sym] = report
                results[name]["_total_score"] += _score(report)
                logger.info(f"  {sym}: PnL={report.get('total_return_pct','—')}% "
                            f"trades={report.get('total_trades', 0)} "
                            f"wr={report.get('win_rate_pct','—')}%")
        finally:
            _restore(snap)

    return results


def print_table(results: dict, symbols: list[str]):
    print("\n" + "═" * 110)
    print(f"  {'CONFIG':<22} {'PnL%':>7} {'Trades':>7} {'WR%':>6} {'PF':>5} {'DD%':>6} {'Sharpe':>7}  Score   Descripción")
    print("─" * 110)

    rows = []
    for name, data in results.items():
        valid = [r for r in data["symbols"].values() if "error" not in r and r.get("total_trades", 0) > 0]
        if not valid:
            rows.append((name, "N/A", 0, "—", "—", "—", "—", data["_total_score"], data["_meta"]["_description"]))
            continue

        avg_pnl = sum(r["total_return_pct"] for r in valid) / len(valid)
        sum_trades = sum(r["total_trades"] for r in valid)
        avg_wr = sum(r["win_rate_pct"] for r in valid) / len(valid)
        pfs = [r["profit_factor"] if r["profit_factor"] != "∞" else 5 for r in valid]
        avg_pf = sum(pfs) / len(pfs)
        avg_dd = sum(r["max_drawdown_pct"] for r in valid) / len(valid)
        avg_sh = sum(r["sharpe_ratio"] for r in valid) / len(valid)
        score = data["_total_score"]
        rows.append((name, avg_pnl, sum_trades, avg_wr, avg_pf, avg_dd, avg_sh, score, data["_meta"]["_description"]))

    # Ordenar por score desc
    rows.sort(key=lambda r: -r[7] if isinstance(r[7], (int, float)) else -1e9)

    for r in rows:
        name, pnl, trades, wr, pf, dd, sh, score, desc = r
        pnl_s = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else pnl
        wr_s  = f"{wr:.1f}" if isinstance(wr, (int, float)) else wr
        pf_s  = f"{pf:.2f}" if isinstance(pf, (int, float)) else pf
        dd_s  = f"{dd:.2f}" if isinstance(dd, (int, float)) else dd
        sh_s  = f"{sh:.2f}" if isinstance(sh, (int, float)) else sh
        score_s = f"{score:+.1f}" if isinstance(score, (int, float)) else score
        print(f"  {name:<22} {pnl_s:>7} {trades:>7} {wr_s:>6} {pf_s:>5} {dd_s:>6} {sh_s:>7}  {score_s:>6}  | {desc}")

    print("═" * 110)
    if rows and isinstance(rows[0][7], (int, float)):
        print(f"\n  GANADOR: {rows[0][0]}")
        print(f"     -> {rows[0][8]}")
        print(f"     -> PnL promedio: {rows[0][1]:+.2f}% sobre {len(symbols)} simbolos")


def main():
    parser = argparse.ArgumentParser()
    _all_symbols = list(config.CRYPTO_SYMBOLS) + list(config.STOCK_SYMBOLS)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--symbols", default=",".join(_all_symbols))
    parser.add_argument("--capital", type=float, default=10_000.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    syms = [s.strip() for s in args.symbols.split(",")]

    print(f"\nComparando {len(CONFIGS)} configs x {len(syms)} simbolos x {args.days} dias\n")
    results = run_comparison(syms, args.days, args.capital)
    print_table(results, syms)

    out = config.BASE_DIR / "data" / "compare_configs_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Reporte completo: {out}")


if __name__ == "__main__":
    main()
