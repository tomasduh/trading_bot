"""
Módulo de aprendizaje automático para el bot de trading.

Flujo:
  1. load_labeled_cycles()  — lee cycles.jsonl y etiqueta si la señal fue correcta
                              (mira el precio del ciclo siguiente del mismo símbolo)
  2. extract_features()     — convierte indicadores en features numéricas
  3. train_model()          — entrena RandomForest (necesita ≥ 30 señales etiquetadas)
  4. evaluate_strategy()    — reporte completo + sugerencias de parámetros
  5. suggest_params()       — devuelve dict con ajustes sugeridos para config.py

Ejecutar manualmente:
    python -m src.ml_analyst
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

logger = logging.getLogger("ml_analyst")

BASE_DIR  = Path(__file__).parent.parent
CYCLES_LOG = BASE_DIR / "data" / "cycles.jsonl"
ML_REPORT  = BASE_DIR / "data" / "ml_report.json"

MIN_SAMPLES_TO_TRAIN = 30   # señales etiquetadas mínimas para entrenar
LOOKAHEAD_CANDLES    = 2    # candles hacia adelante para etiquetar
MIN_MOVE_PCT         = 0.004  # 0.4 % — mínimo movimiento para considerar señal correcta


# ── 1. Carga y etiquetado ─────────────────────────────────────────────────────

def load_raw_cycles() -> list[dict]:
    if not CYCLES_LOG.exists():
        return []
    cycles = []
    with open(CYCLES_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    cycles.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return cycles


def load_labeled_cycles() -> list[dict]:
    """
    Para cada ciclo con señal BUY o SELL, busca el precio N ciclos después
    del mismo símbolo y añade label=1 si la señal fue correcta, 0 si no.
    """
    raw = load_raw_cycles()
    if not raw:
        return []

    # Agrupar por símbolo
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for c in raw:
        sym = c.get("symbol", "UNKNOWN")
        by_symbol[sym].append(c)

    labeled = []
    for sym, cycles in by_symbol.items():
        for i, c in enumerate(cycles):
            sig_type = c["signal"]["type"]
            if sig_type == "NONE":
                continue

            # Buscar precio N candles después
            future_idx = min(i + LOOKAHEAD_CANDLES, len(cycles) - 1)
            if future_idx == i:
                continue  # no hay datos futuros aún

            future_price = cycles[future_idx]["price"]
            current_price = c["price"]
            move = (future_price - current_price) / current_price

            if sig_type == "BUY":
                label = 1 if move >= MIN_MOVE_PCT else 0
            else:  # SELL
                label = 1 if move <= -MIN_MOVE_PCT else 0

            entry = dict(c)
            entry["symbol"] = sym
            entry["label"] = label
            entry["future_price"] = future_price
            entry["actual_move_pct"] = round(move * 100, 3)
            labeled.append(entry)

    return labeled


# ── 2. Feature engineering ────────────────────────────────────────────────────

def extract_features(cycles: list[dict]) -> tuple[list[list[float]], list[int], list[str]]:
    """
    Retorna (X, y, feature_names).
    X: lista de vectores de features
    y: lista de labels (0 o 1)
    """
    feature_names = [
        "rsi",
        "rsi_norm",          # (rsi - 30) / 40 → [0,1] para rango [30,70]
        "macd_hist",
        "macd_hist_norm",    # sign-normalizado
        "bb_position",       # (price - bb_lower) / (bb_upper - bb_lower)
        "ema_gap_pct",       # (ema_fast - ema_slow) / ema_slow * 100
        "score",
        "is_buy",            # 1=BUY, 0=SELL
    ]

    X, y = [], []
    for c in cycles:
        ind = c["indicators"]
        price = c["price"]
        sig   = c["signal"]

        rsi       = ind.get("rsi", 50)
        macd_hist = ind.get("macd_hist", 0)
        bb_upper  = ind.get("bb_upper", price)
        bb_lower  = ind.get("bb_lower", price)
        ema_fast  = ind.get("ema_fast", price)
        ema_slow  = ind.get("ema_slow", price)

        bb_range    = bb_upper - bb_lower if bb_upper != bb_lower else 1
        bb_position = (price - bb_lower) / bb_range
        ema_gap_pct = (ema_fast - ema_slow) / ema_slow * 100 if ema_slow else 0
        rsi_norm    = (rsi - 30) / 40
        macd_norm   = macd_hist / (abs(macd_hist) + 1e-8)  # -1 o +1 suavizado

        feats = [
            rsi, rsi_norm, macd_hist, macd_norm,
            bb_position, ema_gap_pct,
            sig.get("score", 0),
            1.0 if sig["type"] == "BUY" else 0.0,
        ]
        X.append(feats)
        y.append(c["label"])

    return X, y, feature_names


# ── 3. Entrenamiento ──────────────────────────────────────────────────────────

def train_model(X: list, y: list, feature_names: list) -> dict:
    """
    Entrena RandomForest y devuelve métricas + importancias de features.
    Requiere scikit-learn.
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import StandardScaler
        import numpy as np
    except ImportError:
        return {"error": "scikit-learn no instalado — añade a requirements.txt"}

    X_arr = np.array(X)
    y_arr = np.array(y)

    if len(set(y_arr)) < 2:
        return {"error": "todas las señales tienen el mismo label — necesitas más variedad"}

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_arr)

    clf = RandomForestClassifier(
        n_estimators=100,
        max_depth=4,        # evita overfitting con pocos datos
        min_samples_leaf=3,
        random_state=42,
    )

    # Cross-validation (Leave-One-Out si hay pocos datos)
    cv = min(5, len(y_arr) // 2)
    scores = cross_val_score(clf, X_scaled, y_arr, cv=cv, scoring="accuracy")

    clf.fit(X_scaled, y_arr)
    importances = dict(zip(feature_names, clf.feature_importances_.tolist()))

    return {
        "cv_accuracy_mean": round(float(scores.mean()), 3),
        "cv_accuracy_std":  round(float(scores.std()),  3),
        "feature_importance": {k: round(v, 4)
                               for k, v in sorted(importances.items(),
                                                   key=lambda x: -x[1])},
        "n_samples": len(y_arr),
        "class_balance": {
            "correct": int(y_arr.sum()),
            "wrong":   int(len(y_arr) - y_arr.sum()),
        },
    }


# ── 4. Análisis por condición ─────────────────────────────────────────────────

def analyze_conditions(labeled: list[dict]) -> dict:
    """
    Para cada condición individual (RSI, MACD cross, BB), calcula su tasa
    de acierto cuando está activa.
    """
    cond_stats: dict[str, dict] = defaultdict(lambda: {"hits": 0, "correct": 0})

    for c in labeled:
        lbl = c["label"]
        for cond in c["signal"].get("conditions", []):
            cond_stats[cond]["hits"] += 1
            cond_stats[cond]["correct"] += lbl

    result = {}
    for cond, stats in cond_stats.items():
        h = stats["hits"]
        result[cond] = {
            "hits": h,
            "accuracy": round(stats["correct"] / h * 100, 1) if h > 0 else 0,
        }
    return dict(sorted(result.items(), key=lambda x: -x[1]["hits"]))


def analyze_score_vs_accuracy(labeled: list[dict]) -> dict:
    """Tasa de acierto por score (2 vs 3 condiciones activas)."""
    score_stats: dict[int, dict] = defaultdict(lambda: {"hits": 0, "correct": 0})
    for c in labeled:
        s = c["signal"].get("score", 0)
        score_stats[s]["hits"] += 1
        score_stats[s]["correct"] += c["label"]
    return {
        str(score): {
            "hits": v["hits"],
            "accuracy": round(v["correct"] / v["hits"] * 100, 1) if v["hits"] > 0 else 0,
        }
        for score, v in sorted(score_stats.items())
    }


# ── 5. Sugerencia de parámetros ───────────────────────────────────────────────

def suggest_params(labeled: list[dict], model_result: dict) -> dict:
    """
    Basado en el análisis, sugiere ajustes a config.py.
    Devuelve dict con {param: {current, suggested, reason}}.
    """
    from src import config

    suggestions: dict[str, dict] = {}
    score_acc = analyze_score_vs_accuracy(labeled)

    # ¿El score=2 tiene baja tasa de acierto?
    acc_2 = score_acc.get("2", {}).get("accuracy", 100)
    acc_3 = score_acc.get("3", {}).get("accuracy", 100)
    if acc_2 < 50 and acc_3 > acc_2 + 15:
        suggestions["MIN_SCORE"] = {
            "current": 2,
            "suggested": 3,
            "reason": f"señales con score=2 aciertan solo {acc_2}% vs score=3 al {acc_3}%",
        }

    # Analizar RSI promedio en señales correctas vs incorrectas
    buy_correct   = [c["indicators"]["rsi"] for c in labeled
                     if c["signal"]["type"] == "BUY" and c["label"] == 1]
    buy_incorrect = [c["indicators"]["rsi"] for c in labeled
                     if c["signal"]["type"] == "BUY" and c["label"] == 0]

    if buy_correct and buy_incorrect:
        avg_rsi_ok  = sum(buy_correct)   / len(buy_correct)
        avg_rsi_bad = sum(buy_incorrect) / len(buy_incorrect)
        if avg_rsi_ok < avg_rsi_bad - 5:
            new_threshold = round(avg_rsi_ok + (avg_rsi_bad - avg_rsi_ok) / 2, 0)
            suggestions["RSI_OVERBOUGHT_BUY"] = {
                "current": config.RSI_OVERBOUGHT,
                "suggested": int(new_threshold),
                "reason": f"BUYs correctos tienen RSI avg {avg_rsi_ok:.1f} vs incorrectos {avg_rsi_bad:.1f}",
            }

    # ¿Hay muchas señales SELL sin posición? → El bot nunca compra
    sell_without_pos = sum(1 for c in labeled
                          if c["signal"]["type"] == "SELL"
                          and c.get("trade_action") is None)
    buy_count = sum(1 for c in labeled if c["signal"]["type"] == "BUY")
    if sell_without_pos > buy_count * 2:
        suggestions["MARKET_BIAS"] = {
            "current": "neutral",
            "suggested": "review_buy_conditions",
            "reason": f"{sell_without_pos} señales SELL sin posición vs {buy_count} BUY — "
                      "el mercado está en tendencia alcista, las condiciones de compra son muy estrictas",
        }

    return suggestions


# ── 6. Reporte completo ───────────────────────────────────────────────────────

def evaluate_strategy() -> dict:
    labeled = load_labeled_cycles()
    raw     = load_raw_cycles()

    if not raw:
        return {"error": "sin datos — espera al menos 1 ciclo del bot"}

    # Distribución de señales por símbolo
    by_symbol: dict[str, dict] = defaultdict(lambda: {"BUY": 0, "SELL": 0, "NONE": 0})
    for c in raw:
        sym = c.get("symbol", "UNKNOWN")
        by_symbol[sym][c["signal"]["type"]] = by_symbol[sym].get(c["signal"]["type"], 0) + 1

    result: dict = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "total_cycles":     len(raw),
        "labeled_signals":  len(labeled),
        "signals_by_symbol": dict(by_symbol),
        "condition_accuracy": {},
        "score_vs_accuracy":  {},
        "model": {},
        "param_suggestions":  {},
        "narrative": [],
    }

    if not labeled:
        result["narrative"].append(
            f"Solo {len(raw)} ciclos, ninguno etiquetable aún "
            f"(necesitas señales + datos futuros del mismo símbolo)."
        )
        _save_report(result)
        return result

    result["condition_accuracy"] = analyze_conditions(labeled)
    result["score_vs_accuracy"]  = analyze_score_vs_accuracy(labeled)

    # Narrativa básica sin ML
    cond_acc = result["condition_accuracy"]
    for cond, stats in cond_acc.items():
        if stats["hits"] >= 3:
            result["narrative"].append(
                f"'{cond}': {stats['hits']} señales, {stats['accuracy']}% de acierto"
            )

    # ML si hay suficientes datos
    if len(labeled) >= MIN_SAMPLES_TO_TRAIN:
        X, y, feat_names = extract_features(labeled)
        result["model"] = train_model(X, y, feat_names)
        result["param_suggestions"] = suggest_params(labeled, result["model"])

        acc = result["model"].get("cv_accuracy_mean", 0)
        result["narrative"].append(
            f"Modelo RandomForest entrenado con {len(labeled)} señales — "
            f"accuracy CV: {acc*100:.1f}%"
        )
    else:
        needed = MIN_SAMPLES_TO_TRAIN - len(labeled)
        result["narrative"].append(
            f"Faltan {needed} señales etiquetadas para entrenar el modelo ML "
            f"({len(labeled)}/{MIN_SAMPLES_TO_TRAIN})"
        )
        result["param_suggestions"] = suggest_params(labeled, {})

    _save_report(result)
    return result


def _save_report(report: dict):
    ML_REPORT.parent.mkdir(exist_ok=True)
    with open(ML_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


# ── CLI ───────────────────────────────────────────────────────────────────────

def print_report():
    r = evaluate_strategy()
    print("\n" + "=" * 60)
    print("REPORTE ML — ESTRATEGIA DE TRADING")
    print("=" * 60)
    print(f"Ciclos totales: {r['total_cycles']}")
    print(f"Señales etiquetadas: {r['labeled_signals']}")

    if r.get("signals_by_symbol"):
        print("\nSeñales por símbolo:")
        for sym, counts in r["signals_by_symbol"].items():
            print(f"  {sym}: BUY={counts.get('BUY',0)} SELL={counts.get('SELL',0)} NONE={counts.get('NONE',0)}")

    if r.get("condition_accuracy"):
        print("\nTasa de acierto por condición:")
        for cond, stats in r["condition_accuracy"].items():
            print(f"  '{cond}': {stats['hits']} señales → {stats['accuracy']}% correcto")

    if r.get("score_vs_accuracy"):
        print("\nAcierto por score:")
        for score, stats in r["score_vs_accuracy"].items():
            print(f"  Score {score}: {stats['hits']} señales → {stats['accuracy']}% correcto")

    if r.get("model") and "cv_accuracy_mean" in r["model"]:
        m = r["model"]
        print(f"\nModelo ML — accuracy CV: {m['cv_accuracy_mean']*100:.1f}% ± {m['cv_accuracy_std']*100:.1f}%")
        print("  Features más importantes:")
        for feat, imp in list(m["feature_importance"].items())[:4]:
            print(f"    {feat}: {imp:.3f}")

    if r.get("param_suggestions"):
        print("\nSUGERENCIAS DE PARÁMETROS:")
        for param, s in r["param_suggestions"].items():
            print(f"  {param}: {s['current']} → {s['suggested']}")
            print(f"    Razón: {s['reason']}")

    if r.get("narrative"):
        print("\nNARRATIVA:")
        for line in r["narrative"]:
            print(f"  → {line}")

    print("=" * 60)


if __name__ == "__main__":
    print_report()
