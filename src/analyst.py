"""
Módulo de análisis: escribe logs estructurados por ciclo y genera reportes
que Claude puede leer para ajustar la estrategia con datos reales.

Formato de log: JSONL (una línea JSON por ciclo) → data/cycles.jsonl
Cada línea contiene: timestamp, precio, todos los indicadores, señal,
condiciones activas, score, y si hubo trade + resultado.

Para generar reporte de análisis:
    python -m src.analyst
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from src.config import BASE_DIR
from src.database import get_session, Trade, Signal as DbSignal

CYCLES_LOG   = BASE_DIR / "data" / "cycles.jsonl"
FEATURES_LOG = BASE_DIR / "data" / "features.jsonl"   # para entrenamiento ML
REPORT_PATH  = BASE_DIR / "data" / "strategy_report.json"


# ── Escritura de ciclos ───────────────────────────────────────────────────────

def log_cycle(price: float, indicators: dict, signal,
              trade_action: str | None = None, symbol: str = ""):
    """
    Llamar en cada ciclo del bot. Escribe una línea JSON con todo el contexto.
    trade_action: 'OPENED' | 'CLOSED' | None
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "price": round(price, 2),
        "indicators": {k: round(float(v), 4) if v is not None else None
                       for k, v in indicators.items()},
        "signal": {
            "type": signal.type,
            "score": signal.score,
            "reason": signal.reason,
            "conditions": getattr(signal, "conditions", []),
        },
        "trade_action": trade_action,
    }
    with open(CYCLES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ── Feature logging para ML ──────────────────────────────────────────────────

def log_features(symbol: str, trade_id: int, price: float, features: dict,
                 signal_type: str = "BUY"):
    """
    Registra el vector de features en el momento de apertura de un trade.

    Formato JSONL — cada línea es un evento:
      event=entry  → features en el momento del BUY (label aún desconocido)
      event=exit   → outcome del trade (se puede hacer JOIN por trade_id)

    El script de entrenamiento (src/train_ml.py) une ambos eventos por trade_id
    para construir el dataset (X=features, y=was_winner).
    """
    record = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "event":       "entry",
        "symbol":      symbol,
        "trade_id":    trade_id,
        "signal_type": signal_type,
        "price":       round(price, 6),
        "features":    {k: (round(float(v), 6) if v is not None else None)
                        for k, v in features.items()},
    }
    with open(FEATURES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def log_outcome(trade_id: int, symbol: str, exit_reason: str,
                pnl_pct: float, pnl_usdt: float):
    """
    Registra el resultado del trade para poder etiquetar el vector de features.
    Llamar cuando se cierra un trade en bot.py.
    """
    record = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "event":       "exit",
        "symbol":      symbol,
        "trade_id":    trade_id,
        "exit_reason": exit_reason,
        "pnl_pct":     round(pnl_pct, 6),
        "pnl_usdt":    round(pnl_usdt, 4),
        "was_winner":  pnl_usdt > 0,
    }
    with open(FEATURES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_features() -> list[dict]:
    """Carga todos los eventos del features log."""
    if not FEATURES_LOG.exists():
        return []
    records = []
    with open(FEATURES_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def load_feature_dataset() -> list[dict]:
    """
    Construye el dataset de entrenamiento uniendo events entry+exit por trade_id.

    Retorna lista de {features, was_winner, exit_reason, symbol, trade_id}.
    Solo incluye trades con ambos eventos registrados (entry + exit).
    """
    records = load_features()
    entries  = {r["trade_id"]: r for r in records if r["event"] == "entry"}
    outcomes = {r["trade_id"]: r for r in records if r["event"] == "exit"}

    dataset = []
    for tid, entry in entries.items():
        if tid not in outcomes:
            continue   # trade aún abierto
        outcome = outcomes[tid]
        dataset.append({
            "trade_id":    tid,
            "symbol":      entry["symbol"],
            "ts_entry":    entry["ts"],
            "ts_exit":     outcome["ts"],
            "features":    entry["features"],
            "was_winner":  outcome["was_winner"],
            "pnl_pct":     outcome["pnl_pct"],
            "exit_reason": outcome["exit_reason"],
        })
    return dataset


def features_summary() -> dict:
    """Resumen del estado del feature log para el dashboard."""
    dataset = load_feature_dataset()
    all_records = load_features()
    entries_count  = sum(1 for r in all_records if r["event"] == "entry")
    outcomes_count = sum(1 for r in all_records if r["event"] == "exit")
    labeled_count  = len(dataset)
    winners        = sum(1 for d in dataset if d["was_winner"])
    return {
        "total_entries":  entries_count,
        "total_outcomes": outcomes_count,
        "labeled_trades": labeled_count,
        "winners":        winners,
        "losers":         labeled_count - winners,
        "win_rate_pct":   round(winners / labeled_count * 100, 1) if labeled_count else 0,
        "ready_for_ml":   labeled_count >= 200,
        "needed_for_ml":  max(0, 200 - labeled_count),
    }


# ── Análisis ──────────────────────────────────────────────────────────────────

def load_cycles() -> list[dict]:
    if not CYCLES_LOG.exists():
        return []
    with open(CYCLES_LOG, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def generate_report() -> dict:
    """
    Lee todos los ciclos y trades cerrados para producir un reporte
    que Claude puede usar para ajustar la estrategia.
    """
    cycles = load_cycles()
    if not cycles:
        return {"error": "sin datos aún"}

    with get_session() as session:
        closed_trades = session.query(Trade).filter(Trade.status == "CLOSED").all()
        all_signals = session.query(DbSignal).all()

    # ── Stats básicos de señales ──────────────────────────────────────────────
    signal_counts = {"BUY": 0, "SELL": 0, "NONE": 0}
    acted_counts  = {"BUY": 0, "SELL": 0}
    condition_hits: dict[str, int] = {}

    for c in cycles:
        sig = c["signal"]
        signal_counts[sig["type"]] = signal_counts.get(sig["type"], 0) + 1
        for cond in sig.get("conditions", []):
            condition_hits[cond] = condition_hits.get(cond, 0) + 1
        if c.get("trade_action") in ("OPENED", "CLOSED"):
            acted_counts[sig["type"]] = acted_counts.get(sig["type"], 0) + 1

    # ── Stats de trades cerrados ──────────────────────────────────────────────
    trade_stats = {
        "total": len(closed_trades),
        "wins": 0, "losses": 0,
        "total_pnl_usdt": 0.0,
        "avg_pnl_pct": 0.0,
        "best_trade": None,
        "worst_trade": None,
        "exit_reasons": {},
        "avg_duration_minutes": 0.0,
    }

    pnl_pcts = []
    durations = []

    for t in closed_trades:
        pnl = t.pnl_usdt or 0
        trade_stats["total_pnl_usdt"] += pnl
        if pnl > 0:
            trade_stats["wins"] += 1
        else:
            trade_stats["losses"] += 1

        pnl_pcts.append(t.pnl_pct or 0)

        reason = t.exit_reason or "UNKNOWN"
        trade_stats["exit_reasons"][reason] = trade_stats["exit_reasons"].get(reason, 0) + 1

        if t.entry_time and t.exit_time:
            dur = (t.exit_time - t.entry_time).total_seconds() / 60
            durations.append(dur)

        summary = {"id": t.id, "pnl_usdt": round(pnl, 2),
                   "pnl_pct": round((t.pnl_pct or 0) * 100, 2),
                   "exit_reason": t.exit_reason}
        if trade_stats["best_trade"] is None or pnl > trade_stats["best_trade"]["pnl_usdt"]:
            trade_stats["best_trade"] = summary
        if trade_stats["worst_trade"] is None or pnl < trade_stats["worst_trade"]["pnl_usdt"]:
            trade_stats["worst_trade"] = summary

    if pnl_pcts:
        trade_stats["avg_pnl_pct"] = round(sum(pnl_pcts) / len(pnl_pcts) * 100, 3)
    if durations:
        trade_stats["avg_duration_minutes"] = round(sum(durations) / len(durations), 1)
    trade_stats["win_rate_pct"] = (
        round(trade_stats["wins"] / trade_stats["total"] * 100, 1)
        if trade_stats["total"] > 0 else 0
    )
    trade_stats["total_pnl_usdt"] = round(trade_stats["total_pnl_usdt"], 2)

    # ── Análisis de indicadores en el momento de cada señal ──────────────────
    buy_signals = [c for c in cycles if c["signal"]["type"] == "BUY"]
    sell_signals = [c for c in cycles if c["signal"]["type"] == "SELL"]

    def avg_indicator(signal_list: list, key: str) -> float | None:
        vals = [c["indicators"].get(key) for c in signal_list
                if c["indicators"].get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    indicator_context = {
        "on_buy_signals": {
            "avg_rsi": avg_indicator(buy_signals, "rsi"),
            "avg_macd_hist": avg_indicator(buy_signals, "macd_hist"),
            "avg_ema_gap_pct": None,
        },
        "on_sell_signals": {
            "avg_rsi": avg_indicator(sell_signals, "rsi"),
            "avg_macd_hist": avg_indicator(sell_signals, "macd_hist"),
        },
    }

    # ── Recomendaciones automáticas ───────────────────────────────────────────
    recommendations = []

    if trade_stats["total"] >= 5:
        wr = trade_stats["win_rate_pct"]
        if wr < 40:
            recommendations.append(
                f"Win rate {wr}% < 40% — las señales generan demasiados falsos positivos. "
                "Considera subir el threshold de score a 3/3 o añadir filtro de tendencia (EMA 200)."
            )
        elif wr > 65:
            recommendations.append(
                f"Win rate {wr}% excelente. Considera aumentar RISK_PER_TRADE de 1% a 1.5%."
            )

        sl_exits = trade_stats["exit_reasons"].get("STOP_LOSS", 0)
        tp_exits = trade_stats["exit_reasons"].get("TAKE_PROFIT", 0)
        if sl_exits > tp_exits * 2:
            recommendations.append(
                f"Stop Loss se activa {sl_exits}x vs Take Profit {tp_exits}x. "
                "El SL está muy ajustado o el TP muy lejano. "
                "Prueba aumentar STOP_LOSS_PCT a 0.025 o bajar TAKE_PROFIT_PCT a 0.03."
            )

    if len(cycles) < 20:
        recommendations.append(
            f"Solo {len(cycles)} ciclos registrados. Necesitas más datos para ajustar parámetros con confianza."
        )

    most_common_cond = sorted(condition_hits.items(), key=lambda x: -x[1])[:3] if condition_hits else []

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cycles_analyzed": len(cycles),
        "signal_distribution": signal_counts,
        "condition_hits": dict(most_common_cond),
        "trade_stats": trade_stats,
        "indicator_context": indicator_context,
        "recommendations": recommendations,
    }

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return report


def print_report():
    report = generate_report()
    print("\n" + "=" * 60)
    print("REPORTE DE ESTRATEGIA")
    print("=" * 60)
    print(f"Ciclos analizados: {report.get('cycles_analyzed', 0)}")
    print(f"Señales → BUY: {report['signal_distribution'].get('BUY',0)} | "
          f"SELL: {report['signal_distribution'].get('SELL',0)} | "
          f"NONE: {report['signal_distribution'].get('NONE',0)}")

    ts = report["trade_stats"]
    print(f"\nTrades cerrados: {ts['total']}")
    if ts["total"] > 0:
        print(f"  Win rate:   {ts['win_rate_pct']}%")
        print(f"  PnL total:  {ts['total_pnl_usdt']:+.2f} USDT")
        print(f"  PnL medio:  {ts['avg_pnl_pct']:+.3f}%")
        print(f"  Duración:   {ts['avg_duration_minutes']} min promedio")
        print(f"  Exits:      {ts['exit_reasons']}")
        if ts["best_trade"]:
            bt = ts["best_trade"]
            print(f"  Mejor trade: #{bt['id']} {bt['pnl_pct']:+.2f}%")
        if ts["worst_trade"]:
            wt = ts["worst_trade"]
            print(f"  Peor trade:  #{wt['id']} {wt['pnl_pct']:+.2f}%")

    conds = report.get("condition_hits", {})
    if conds:
        print(f"\nCondiciones más activas: {conds}")

    recs = report.get("recommendations", [])
    if recs:
        print("\n⚠ RECOMENDACIONES:")
        for r in recs:
            print(f"  → {r}")
    else:
        print("\nSin recomendaciones aún (acumula más datos).")
    print("=" * 60)


if __name__ == "__main__":
    print_report()
