"""
Walk-forward validation para la estrategia de trading.

Metodología
───────────
1. Descarga `total_days` de historia completa (una sola vez por símbolo).
2. Divide en ventanas deslizantes:
     ventana k = [train_start_k : train_start_k + train_days | test_days]
   Cada ventana avanza `test_days` hacia adelante.
3. Para cada ventana:
   a. Optimiza en el slice de TRAIN → elige la variante con mayor score.
   b. Evalúa esa variante en el slice de TEST (out-of-sample).
4. Reporta por símbolo:
   - Resultados OOS de cada ventana (Sharpe, PnL%, win-rate)
   - Sharpe OOS agregado (media ± desviación estándar)
   - Consistencia: % de ventanas donde la variante ganó vs baseline en OOS
   - Tabla resumen

Evita el principal sesgo del backtest simple: elegir los mejores parámetros
sobre toda la historia sin dejar datos OOS para verificar.

Uso CLI
───────
    python -m src.walk_forward BTC/USDT                    # defaults
    python -m src.walk_forward BTC/USDT --train 60 --test 20 --total 180
    python -m src.walk_forward --all --train 60 --test 20 --total 160

Argumentos
──────────
  symbol     Símbolo a validar (ej: BTC/USDT). Ignorado si --all.
  --all      Valida todos los CRYPTO_SYMBOLS de config.
  --train    Días en la ventana de entrenamiento (default: 60).
  --test     Días en la ventana de test/OOS (default: 20).
  --total    Total de días a descargar (default: train + 4×test).
  --capital  Capital inicial simulado (default: 10000).
  --timeframe Timeframe de las velas (default: 30m).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from src import config, indicators
from src.backtest import fetch_history, run_backtest, REPORTS_DIR
from src.optimizer import VARIANTS, _apply_variant, _restore_config, _score, _run_backtest_impl

logger = logging.getLogger("walk_forward")


# ── Core ──────────────────────────────────────────────────────────────────────

def _candles_for_days(timeframe: str, days: int) -> int:
    """Estima el número de velas para N días en el timeframe dado."""
    tf_minutes = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440,
    }.get(timeframe, 30)
    return int(days * 24 * 60 / tf_minutes)


def _run_variants_on_df(symbol: str, df: pd.DataFrame,
                        timeframe: str, capital: float,
                        variant_names: list[str] | None = None) -> dict[str, dict]:
    """Corre todas las variantes (o un subconjunto) sobre un DataFrame pre-cargado.

    Retorna {variant_name: report_dict}.
    """
    results: dict[str, dict] = {}
    variants_to_run = [v for v in VARIANTS
                       if variant_names is None or v["name"] in variant_names]

    for variant in variants_to_run:
        name = variant["name"]
        snap = _apply_variant(variant)
        try:
            disable    = variant.get("DISABLE_SIGNAL_EXIT", False)
            only_loss  = variant.get("SIGNAL_EXIT_ONLY_IN_LOSS", False)
            report = _run_backtest_impl(
                symbol, timeframe, 0, capital,   # days=0 → no descarga
                disable_signal_exit=disable,
                signal_exit_only_in_loss=only_loss,
                df_preloaded=df,
            )
            results[name] = report
        except Exception as e:
            results[name] = {"error": str(e)}
        finally:
            _restore_config(snap)

    return results


def run_walk_forward(symbol: str,
                     timeframe: str = "30m",
                     total_days: int = 180,
                     train_days: int = 60,
                     test_days:  int = 20,
                     capital:    float = 10_000.0) -> dict:
    """
    Ejecuta walk-forward validation para un símbolo.

    Returns
    ───────
    dict con:
      windows: lista de resultados por ventana
      oos_sharpe_mean / oos_sharpe_std
      oos_pnl_mean / oos_pnl_std
      consistency_pct   — % ventanas donde winner-train gana al baseline en OOS
      best_variant_train_count — variante más frecuente como ganadora en train
    """
    logger.info(f"[WF] {symbol} | total={total_days}d train={train_days}d test={test_days}d")

    # ── 1. Descargar historia completa ────────────────────────────────────────
    try:
        df_full = fetch_history(symbol, timeframe, total_days)
    except Exception as e:
        return {"symbol": symbol, "error": f"fetch falló: {e}"}

    min_candles = _candles_for_days(timeframe, train_days + test_days)
    if len(df_full) < min_candles:
        return {"symbol": symbol,
                "error": f"datos insuficientes ({len(df_full)} velas, se necesitan ~{min_candles})"}

    logger.info(f"[WF] {symbol} descargado: {len(df_full)} velas | "
                f"{df_full.index[0]} → {df_full.index[-1]}")

    # ── 2. Construir ventanas ─────────────────────────────────────────────────
    train_len = _candles_for_days(timeframe, train_days)
    test_len  = _candles_for_days(timeframe, test_days)

    windows_data: list[dict] = []
    start = 0
    window_idx = 0

    while start + train_len + test_len <= len(df_full):
        train_df = df_full.iloc[start : start + train_len]
        test_df  = df_full.iloc[start + train_len : start + train_len + test_len]

        if len(train_df) < 250 or len(test_df) < 20:
            break

        window_idx += 1
        t_start = str(train_df.index[0])[:10]
        t_split = str(test_df.index[0])[:10]
        t_end   = str(test_df.index[-1])[:10]
        logger.info(f"[WF] Ventana {window_idx}: train [{t_start}→{t_split}] "
                    f"test [{t_split}→{t_end}]")

        # ── 2a. Optimizar en TRAIN ────────────────────────────────────────────
        train_results = _run_variants_on_df(symbol, train_df, timeframe, capital)
        best_name     = max(train_results, key=lambda n: _score(train_results[n]))
        best_train    = train_results[best_name]

        # ── 2b. Evaluar WINNER en TEST (out-of-sample) ────────────────────────
        variant_obj = next((v for v in VARIANTS if v["name"] == best_name), None)
        if variant_obj:
            snap = _apply_variant(variant_obj)
            try:
                oos_report = _run_backtest_impl(
                    symbol, timeframe, 0, capital,
                    disable_signal_exit=variant_obj.get("DISABLE_SIGNAL_EXIT", False),
                    signal_exit_only_in_loss=variant_obj.get("SIGNAL_EXIT_ONLY_IN_LOSS", False),
                    df_preloaded=test_df,
                )
            except Exception as e:
                oos_report = {"error": str(e)}
            finally:
                _restore_config(snap)
        else:
            oos_report = {"error": "variante no encontrada"}

        # ── 2c. Baseline en TEST (para comparar) ─────────────────────────────
        baseline_v = next((v for v in VARIANTS if v["name"] == "baseline"), VARIANTS[0])
        snap = _apply_variant(baseline_v)
        try:
            baseline_oos = _run_backtest_impl(
                symbol, timeframe, 0, capital, df_preloaded=test_df)
        except Exception as e:
            baseline_oos = {"error": str(e)}
        finally:
            _restore_config(snap)

        windows_data.append({
            "window":         window_idx,
            "train_period":   f"{t_start} → {t_split}",
            "test_period":    f"{t_split} → {t_end}",
            "train_winner":   best_name,
            "train_score":    round(_score(best_train), 2),
            "train_pnl_pct":  best_train.get("total_return_pct", None),
            "oos_pnl_pct":    oos_report.get("total_return_pct", None),
            "oos_sharpe":     oos_report.get("sharpe_ratio", None),
            "oos_win_rate":   oos_report.get("win_rate_pct", None),
            "oos_trades":     oos_report.get("total_trades", 0),
            "oos_dd":         oos_report.get("max_drawdown_pct", None),
            "baseline_oos_pnl": baseline_oos.get("total_return_pct", None),
            "winner_beat_baseline": (
                (oos_report.get("total_return_pct") or 0) >
                (baseline_oos.get("total_return_pct") or 0)
            ),
        })

        start += test_len   # avanzar por test_days (anchored rolling)

    if not windows_data:
        return {"symbol": symbol, "error": "sin suficientes datos para ninguna ventana"}

    # ── 3. Métricas agregadas ─────────────────────────────────────────────────
    valid_oos = [w for w in windows_data if w["oos_sharpe"] is not None]
    sharpes   = [w["oos_sharpe"]  for w in valid_oos]
    pnls      = [w["oos_pnl_pct"] for w in valid_oos if w["oos_pnl_pct"] is not None]
    beaten    = sum(1 for w in windows_data if w["winner_beat_baseline"])

    def _mean(lst):
        return sum(lst) / len(lst) if lst else None

    def _std(lst):
        if len(lst) < 2:
            return None
        m = _mean(lst)
        return math.sqrt(sum((x - m) ** 2 for x in lst) / (len(lst) - 1))

    # Variante más frecuente como ganadora en train
    from collections import Counter
    winner_counts = Counter(w["train_winner"] for w in windows_data)
    best_variant_overall = winner_counts.most_common(1)[0][0] if winner_counts else "—"

    return {
        "symbol":              symbol,
        "timeframe":           timeframe,
        "total_days":          total_days,
        "train_days":          train_days,
        "test_days":           test_days,
        "n_windows":           len(windows_data),
        "oos_sharpe_mean":     round(_mean(sharpes), 3)    if _mean(sharpes) is not None else None,
        "oos_sharpe_std":      round(_std(sharpes), 3)     if _std(sharpes) is not None else None,
        "oos_pnl_mean_pct":    round(_mean(pnls), 2)       if _mean(pnls) is not None else None,
        "oos_pnl_std_pct":     round(_std(pnls), 2)        if _std(pnls) is not None else None,
        "consistency_pct":     round(beaten / len(windows_data) * 100, 1),
        "best_variant_train":  best_variant_overall,
        "windows":             windows_data,
    }


def print_summary(r: dict):
    print("\n" + "=" * 70)
    print(f"WALK-FORWARD  {r.get('symbol','')}  "
          f"{r.get('train_days',0)}d train / {r.get('test_days',0)}d test")
    print("=" * 70)
    if "error" in r:
        print(f"  ERROR: {r['error']}")
        return

    print(f"  Ventanas OOS: {r['n_windows']}")
    print(f"  Sharpe OOS:   {r['oos_sharpe_mean']} ± {r['oos_sharpe_std']}")
    print(f"  PnL OOS:      {r['oos_pnl_mean_pct']:+.2f}% ± {r['oos_pnl_std_pct']:.2f}%"
          if r.get('oos_pnl_mean_pct') is not None else "  PnL OOS:      —")
    print(f"  Consistencia: {r['consistency_pct']}% ventanas donde winner > baseline")
    print(f"  Variante más ganadora en train: {r['best_variant_train']}")
    print()
    print(f"  {'Win':>3}  {'Period (test)':>25}  {'Variante':>22}  "
          f"{'PnL OOS':>8}  {'Sharpe':>6}  {'WR%':>5}  {'Beat?':>5}")
    print("  " + "-" * 80)
    for w in r["windows"]:
        beat = "✅" if w["winner_beat_baseline"] else "❌"
        pnl  = f"{w['oos_pnl_pct']:+.2f}%" if w["oos_pnl_pct"] is not None else "—"
        shr  = f"{w['oos_sharpe']:.2f}"    if w["oos_sharpe"]  is not None else "—"
        wr   = f"{w['oos_win_rate']:.0f}%" if w["oos_win_rate"] is not None else "—"
        print(f"  {w['window']:>3}  {w['test_period']:>25}  "
              f"{w['train_winner']:>22}  {pnl:>8}  {shr:>6}  {wr:>5}  {beat:>5}")
    print("=" * 70)

    # Advertencia si la consistencia es baja
    if r["consistency_pct"] < 50:
        print(f"\n  ⚠️  Consistencia {r['consistency_pct']}% < 50% → los parámetros "
              f"optimizados NO generalizan bien. Considerar simplificar la estrategia.")
    elif r["consistency_pct"] >= 70:
        print(f"\n  ✅ Consistencia {r['consistency_pct']}% ≥ 70% → los parámetros "
              f"generalizan razonablemente bien.")


def save_report(r: dict) -> Path:
    sym = r.get("symbol", "unknown").replace("/", "-")
    ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"wf_{sym}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2, ensure_ascii=False, default=str)
    return path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Walk-forward validation")
    parser.add_argument("symbol", nargs="?", help="ej: BTC/USDT")
    parser.add_argument("--all",       action="store_true")
    parser.add_argument("--train",     type=int, default=60,    help="días de entrenamiento")
    parser.add_argument("--test",      type=int, default=20,    help="días de test OOS")
    parser.add_argument("--total",     type=int, default=0,
                        help="total días a descargar (default: train + 4×test)")
    parser.add_argument("--capital",   type=float, default=10_000.0)
    parser.add_argument("--timeframe", default="30m")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    total = args.total if args.total > 0 else args.train + 4 * args.test
    symbols = config.CRYPTO_SYMBOLS if args.all else [args.symbol or config.CRYPTO_SYMBOLS[0]]

    for sym in symbols:
        r    = run_walk_forward(sym, args.timeframe, total, args.train, args.test, args.capital)
        path = save_report(r)
        print_summary(r)
        print(f"  Reporte: {path}\n")


if __name__ == "__main__":
    main()
