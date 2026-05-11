# 🗺️ Roadmap del Bot de Trading

Pasos a seguir, ordenados por prioridad y dificultad. Cada paso incluye **qué hacer**, **por qué**, **archivos a tocar** y **cómo testear**.

---

## 📍 Estado actual (snapshot)

> **Última actualización:** 2026-05-11 — Sprints Semana 1 y Semana 2 completados.

| | |
|---|---|
| Plataforma | Fly.io (Frankfurt) — 24/7 |
| Mercados | 4 crypto (Binance Testnet) + 7 stocks (Alpaca Paper) |
| Estrategia | RSI + MACD + Bollinger + filtro tendencia (EMA21/50 + ADX) + ATR disponible |
| Timeframe | 30m |
| Risk per trade | 1% del capital |
| SL / TP | 2% / 4% fijo (ratio 2:1) — ATR-based disponible con `USE_ATR_STOPS=True` |
| Trailing stop | Desactivado (optimizer demostró que perjudica) |
| Signal exit | Solo si pérdida real > 0.2% (buffer) |
| Fees en backtest | ✅ 0.1% Binance + 0.05% slippage (round-trip) |
| Walk-forward | ✅ `python -m src.walk_forward BTC/USDT` |
| Circuit breaker | ✅ DD diario 3% / pérdida total 10% / 5 pérdidas consecutivas |
| Reconciliador | ✅ DB vs exchange al inicio de cada ciclo |
| Watchdog externo | ✅ healthchecks.io via `HC_PING_URL` env var |
| Rate limiting | ✅ slowapi 60-200/min por endpoint |
| Auth dashboard | Token obligatorio + Origin check + CSRF protection |
| Tests | 28/28 ✅ |

### 📦 Commits recientes

| Commit | Descripción |
|---|---|
| `7bba1b3` | feat: Sprint Semana 2 — walk-forward, ATR stops, circuit breaker |
| `5a12e7a` | feat: Sprint Semana 1 — fees en backtest, reconciliador, watchdog, quick wins |
| `393f2aa` | docs: ROADMAP.md inicial |
| `5457db9` | Hardening: auth obligatoria, .gitignore, fix bugs riesgo/SIGNAL exit |
| `fd79c43` | first commit (limpio, sin .env en historial) |

---

## 🎯 Fase 1 — Realismo del backtest (prioridad ALTA)

> **Por qué primero**: hoy nuestras métricas (Sharpe, PnL, profit factor) están **sobreestimadas** porque no contemplamos fees ni slippage. Cualquier decisión de ir a LIVE necesita esto resuelto.

### ✅ 1.1 Agregar fees al backtest — **COMPLETADO** (commit `5a12e7a`)

**Qué:** Restar la comisión real del exchange en cada trade simulado.

- **Binance spot:** 0.1% por lado (0.2% round-trip).
- **Alpaca paper:** 0% comisión, pero **slippage** sí impacta.

**Cómo:**
```python
# src/config.py
FEE_PCT = {
    "BTC/USDT": 0.001,  # 0.1% Binance spot
    "ETH/USDT": 0.001,
    "SOL/USDT": 0.001,
    "BNB/USDT": 0.001,
    # stocks Alpaca: 0
}
SLIPPAGE_PCT = 0.0005  # 0.05% slippage por orden (estimado conservador)
```

**Archivos a modificar:**
- `src/backtest.py` (línea ~167, donde calculas pnl):
  ```python
  fee = config.FEE_PCT.get(symbol, 0) + config.SLIPPAGE_PCT
  pnl_usdt -= (entry_price + exit_price) * qty * fee
  ```
- `src/optimizer.py` igual lógica
- `src/risk_manager.py`: `pnl()` opcionalmente acepta `fee_pct` param

**Test:**
```bash
python -m src.backtest BTC/USDT 30m 90
```
Comparar PnL antes/después. Esperamos un **degrade de ~0.4% absoluto** en cada trade.

**Tiempo estimado:** 2h

---

### ✅ 1.2 Walk-forward validation — **COMPLETADO** (commit `7bba1b3`)

**Qué:** En lugar de backtest sobre 90 días continuos, dividir en N ventanas y re-entrenar/optimizar en cada una para evitar overfitting.

**Por qué:** Si optimizamos sobre 90 días y elegimos los mejores params, ya estamos viendo el "futuro". Walk-forward garantiza que los params funcionen out-of-sample.

**Implementación:**
- Nuevo archivo `src/walk_forward.py`
- Para cada ventana: entrenar en `[t0, t1]`, evaluar en `[t1, t2]`, avanzar
- Reportar Sharpe agregado y consistencia entre ventanas

**Tiempo estimado:** 3h

---

## 🎯 Fase 2 — Mejoras de estrategia (prioridad MEDIA-ALTA)

### ✅ 2.1 ATR-based stops — **COMPLETADO** (commit `7bba1b3`)

**Qué:** En vez de SL fijo del 2%, usar `SL = entry - k·ATR(14)` donde k≈2.

**Por qué:** BTC tiene volatilidad muy distinta a SOL. Un SL del 2% en BTC es razonable, pero en SOL queda dentro del ruido normal y se ejecuta constantemente.

**Implementación:**
```python
# src/risk_manager.py
def stop_loss_price_atr(entry_price, atr, k=2.0):
    return entry_price - k * atr

# src/strategy.py — añadir ATR al snapshot
# src/bot.py — pasar atr a executor.open_trade
```

**Config nuevo:**
```python
USE_ATR_STOPS = True
ATR_PERIOD = 14
ATR_MULTIPLIER_SL = 2.0
ATR_MULTIPLIER_TP = 4.0  # ratio 2:1 mantenido
```

**Test:** backtest comparativo SL fijo vs SL ATR.

**Tiempo estimado:** 3h

---

### ⏳ 2.2 Multi-timeframe (4h confirmación, 30m entrada)

**Qué:** Usar timeframe 4h para detectar tendencia macro, 30m para timing de entrada.

**Por qué:** El filtro actual EMA21/50 en 30m es ruidoso. Usar 4h para tendencia + 30m para entry da mejor calidad de señales.

**Implementación:**
```python
# src/strategy.py
def evaluate_with_macro(df_30m, df_4h):
    macro_trend = _detect_trend(df_4h)  # up/down/neutral
    if macro_trend != "up":
        return Signal("NONE", ...)  # solo BUY en tendencia macro alcista
    return evaluate(df_30m)
```

**Tiempo estimado:** 4h

---

### ⏳ 2.3 Régimen de mercado (trending / ranging / choppy)

**Qué:** Clasificar el estado del mercado y operar solo cuando sea favorable.

**Reglas:**
- **Trending:** ADX > 25 → BUY en pullbacks
- **Ranging:** ADX < 20 y BB-width estable → BUY en BB inferior
- **Choppy:** alta volatilidad sin dirección → **NO operar**

**Implementación:**
```python
# src/strategy.py
def detect_regime(df):
    adx = df["adx"].iloc[-2]
    bb_width = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    if adx > 25 and bb_width.iloc[-2] > bb_width.median():
        return "trending"
    if adx < 20:
        return "ranging"
    return "choppy"
```

**Tiempo estimado:** 3h

---

## 🤖 Fase 3 — Machine Learning (prioridad MEDIA)

> **Pre-requisito:** tener acumulados **≥ 200 trades cerrados** para entrenar con confianza. Actualmente tenemos 48h+ de cycles loggeados — empezar a recolectar features ya.

### ⏳ 3.1 Loguear feature vectors completos

**Qué:** Cada vez que el bot evalúa, guardar **todas las features** que generó (no solo el resultado).

**Por qué:** Hoy en `cycles.jsonl` guardamos algunos indicadores. Necesitamos vector completo para entrenar.

**Implementación:**
```python
# src/analyst.py — log_cycle
features = {
    "rsi": ..., "rsi_diff": ..., "macd_hist": ..., "macd_diff": ...,
    "bb_pos": (close - bb_lower) / (bb_upper - bb_lower),  # normalized
    "adx": ..., "ema_ratio": ema_fast / ema_slow,
    "trend": ..., "regime": ..., "vol_ratio": ...,
    "hour_utc": ts.hour, "day_of_week": ts.dayofweek,
}
```

Loguear en archivo separado: `data/features.jsonl`

**Tiempo estimado:** 2h

---

### ⏳ 3.2 ML Meta-classifier con LightGBM

**Qué:** Entrenar un modelo que prediga `P(trade_es_ganador)` dadas las features.

**Pipeline:**
1. Construir dataset: `(features_at_entry, was_winner)` por cada trade cerrado
2. Train/test split temporal (no random)
3. LightGBM con `objective=binary`
4. Threshold ajustable: solo abrir trade si `P > 0.6`

**Implementación:**
```python
# src/ml_filter.py (nuevo)
import lightgbm as lgb
import joblib

class MLFilter:
    def __init__(self):
        self.model = joblib.load("data/ml_model.pkl")

    def should_trade(self, features: dict) -> bool:
        p = self.model.predict_proba([list(features.values())])[0, 1]
        return p > config.ML_THRESHOLD

# src/bot.py
if signal.type == "BUY":
    if ml_filter.should_trade(extract_features(df_ind)):
        executor.open_trade(...)
```

**Training script:** `src/train_ml.py`
```bash
python -m src.train_ml --features data/features.jsonl --trades data/trades.db
```

**Métricas a vigilar:**
- Precision @ threshold 0.6
- Recall (cuántas oportunidades nos perdemos)
- Backtest con/sin filtro ML

**Tiempo estimado:** 8h (train + eval + integration)

---

### ⏳ 3.3 Re-entrenamiento automático

**Qué:** Cada N días, retrain con datos nuevos.

**Cómo:**
- Cron job en Fly.io o script weekly en GitHub Actions
- Guardar versión del modelo, comparar AUC antes de pisar

**Tiempo estimado:** 3h

---

## 🛡️ Fase 4 — Robustez de producción (CRÍTICO antes de LIVE)

### ✅ 4.1 Reconciliación periódica con exchange — **COMPLETADO** (commit `5a12e7a`)

**Qué:** Cada N ciclos, comparar trades abiertos en DB vs órdenes/posiciones en exchange.

**Por qué:** Si una orden se ejecuta parcialmente, o el bot reinicia justo cuando se cierra una posición manualmente, la DB queda divergente.

**Implementación:**
```python
# src/reconciler.py
def reconcile(executor, fetcher):
    db_open = {t.symbol: t for t in executor._open_trades.values() if t}
    exchange_positions = fetcher.get_open_positions()  # ya existe en alpaca_fetcher
    for symbol, db_trade in db_open.items():
        if symbol not in exchange_positions:
            logger.warning(f"DIVERGENCE: DB tiene {symbol} abierto pero exchange NO")
            # Opciones: cerrar en DB / reabrir en exchange / alertar
```

**Cuándo correr:** al inicio de cada ciclo en `bot.py`.

**Tiempo estimado:** 4h

---

### ✅ 4.2 Circuit breaker server-side — **COMPLETADO** (commit `7bba1b3`)

**Qué:** Pausa automática del bot si:
- Drawdown diario > 3%
- PnL acumulado < -10% del capital inicial
- 5 trades consecutivos perdedores
- Latencia exchange > 10s sostenida

**Implementación:**
```python
# src/circuit_breaker.py
class CircuitBreaker:
    def check(self, recent_trades, equity_curve) -> tuple[bool, str]:
        # daily_dd, consecutive_losses, etc.
        ...
        return (should_pause, reason)

# bot.py
should_pause, reason = breaker.check(...)
if should_pause:
    PAUSE_FILE.touch()
    alerts.notify_error(f"🚨 Circuit breaker: {reason}")
```

**Tiempo estimado:** 4h

---

### ⏳ 4.3 Cap global de exposición

**Qué:** Hoy `MAX_OPEN_TRADES=1` es por símbolo. Si los 4 cryptos dan BUY simultáneamente, abrimos 4 trades = 4% de exposición sin tracking agregado.

**Implementación:**
```python
# src/config.py
MAX_TOTAL_EXPOSURE_PCT = 0.05  # máx 5% del capital en riesgo simultáneo

# src/executor.py
def total_exposure_pct(self, capital):
    open_risk = sum(
        (t.entry_price - t.stop_loss) * t.quantity
        for t in self._open_trades.values() if t
    )
    return open_risk / capital
```

**Tiempo estimado:** 2h

---

### ✅ 4.4 Watchdog externo — **COMPLETADO** (commit `5a12e7a`)

**Qué:** Servicio externo que verifica que el bot esté vivo y operando, y alerta si se cuelga.

**Opciones:**
- **UptimeRobot** (free): ping cada 5min a `/api/health`
- **GitHub Action cron** que hace POST a un endpoint del bot
- **Healthchecks.io**: el bot hace ping cada ciclo, alerta si falta

**Implementación con healthchecks.io:**
```python
# bot.py al final de run_cycle
import requests
requests.get(f"https://hc-ping.com/{HC_UUID}", timeout=5)
```

**Tiempo estimado:** 1h

---

### ⏳ 4.5 Backups automáticos de la DB

**Qué:** Snapshot diario del SQLite a S3 / Backblaze / GitHub.

**Cómo:**
```bash
# Cron en Fly: cada noche
sqlite3 data/trades.db ".backup data/backup-$(date +%F).db"
# Subir a S3 con boto3 o rclone
```

**Tiempo estimado:** 2h

---

## 🔒 Fase 5 — Antes de pasar a LIVE (CRÍTICO)

### Checklist obligatorio

- [x] **Fase 1 completa** (fees + slippage ✅ + walk-forward ✅)
- [ ] **Fase 4 completa** (reconciliación ✅ + circuit breaker ✅ + watchdog ✅ + cap exposición ⏳ + backups ⏳)
- [ ] **2FA o IP allowlist** en el dashboard
- [ ] **Binance API keys** con:
  - ✅ Read enabled
  - ✅ Spot trade enabled
  - ❌ **Withdraw DESACTIVADO** (verificar 2 veces)
  - ✅ IP allowlist con la IP fija de Fly
- [ ] **Cap inicial**: empezar con USD 100-500, no más
- [ ] **Order confirmation**: orders LIVE requieren `--confirm` flag o review manual
- [ ] **Telegram alerts** en cada trade
- [ ] **Drawdown stop**: si bajamos del 90% inicial, kill-switch automático
- [ ] **Mínimo 30 días** corriendo sin errores en testnet/paper antes de live

### Cambios técnicos para LIVE

```python
# src/config.py
TRADING_MODE = "live"  # cambiar después de TODO lo anterior
```

```bash
# fly.io
fly secrets set BINANCE_API_KEY=... BINANCE_SECRET=... --app tomas-bot-trading
```

**Tiempo estimado:** 1-2 semanas de testing + verificaciones

---

## 📊 Fase 6 — Mejoras del dashboard (prioridad BAJA)

### 6.1 Gráficos en tiempo real

- Equity curve interactivo (Chart.js o Plotly)
- Drawdown histórico
- PnL por símbolo / día / hora
- Distribución de retornos

### 6.2 Vista de ML

- Probabilidades del modelo en tiempo real
- Feature importances
- Confusion matrix de últimos N trades

### 6.3 Alertas configurables desde UI

- Webhook a Discord/Slack
- Threshold de PnL para notificar
- Símbolos en watch-list

**Tiempo estimado total:** 10h

---

## 📝 Tareas técnicas pequeñas (quick wins)

| Estado | Tarea | Archivo | Esfuerzo |
|---|---|---|---|
| ✅ | Migrar `@app.on_event("startup")` → `lifespan` | `src/api.py` | 30min |
| ✅ | Fix `watch_log` rotation con inode tracking | `src/api.py` | 1h |
| ✅ | UNIQUE index en `Candle(symbol, timestamp)` + migración | `src/database.py` | 1h |
| ✅ | Sanitizar `innerHTML` con `escapeHtml()` en frontend | `static/app.js` | 1h |
| ✅ | Rate limiting con `slowapi` | `src/api.py` | 1h |
| ⏳ | Audit log de pause/resume/orders | `src/database.py` + `src/api.py` | 2h |
| ⏳ | Tests para `executor.close_trade` con mock | `tests/test_executor.py` (nuevo) | 2h |
| ⏳ | Tests para `alpaca_fetcher` | `tests/test_alpaca.py` (nuevo) | 2h |

---

## 🗓️ Cronograma sugerido

### ✅ Semana 1 — COMPLETADA
- ✅ Fase 1.1: Fees + slippage en backtest
- ✅ Fase 4.1: Reconciliación
- ✅ Fase 4.4: Watchdog externo
- ✅ Quick wins: lifespan, inode tracking, rate limiting, UNIQUE index, escapeHtml

### ✅ Semana 2 — COMPLETADA
- ✅ Fase 1.2: Walk-forward validation
- ✅ Fase 2.1: ATR-based stops
- ✅ Fase 4.2: Circuit breaker

### ⏳ Semana 3 — PRÓXIMA
- Fase 2.2: Multi-timeframe (4h confirmación, 30m entrada)
- Fase 3.1: Logging features completas → `data/features.jsonl`
- Fase 4.3: Cap exposición global

### Semana 4
- Fase 2.3: Régimen de mercado (trending / ranging / choppy)
- Fase 3.2: ML meta-classifier (training)
- Quick wins restantes (test_executor, test_alpaca, audit log)

### Semana 5-6
- Fase 3.2: ML integration + backtest comparativo
- Fase 4.5: Backups automáticos DB
- Stress test en testnet

### Semana 7-8
- Fase 5: Pre-LIVE checklist completo
- Soft launch con USD 100

---

## 🚨 Reglas de oro

1. **Nunca pasar a LIVE sin haber completado Fase 1, 4 y 5 al 100%.**
2. **Cada cambio de estrategia → backtest + walk-forward antes de deploy.**
3. **Empezar siempre con capital mínimo en LIVE** (USD 100-500).
4. **Si algo se siente raro → pausar el bot, investigar.** El kill-switch existe por algo.
5. **Cualquier cambio en `config.py` que afecte risk → revisar 2 veces.**
6. **Backup de la DB antes de cualquier cambio de schema.**
7. **Rotar API keys cada 90 días.**
8. **Revisar logs todos los días los primeros 30 días en LIVE.**

---

## 📚 Recursos

- [Binance Spot API docs](https://binance-docs.github.io/apidocs/spot/en/)
- [Alpaca Trading API docs](https://docs.alpaca.markets/)
- [pandas-ta indicators](https://github.com/twopirllc/pandas-ta)
- [LightGBM tutorial](https://lightgbm.readthedocs.io/)
- [Backtesting best practices](https://www.quantstart.com/articles/Backtesting-Common-Sense-Tips/)

---

## 🤝 Contacto / Notas

- **Repo:** https://github.com/tomasduh/trading_bot
- **App:** https://tomas-bot-trading.fly.dev
- **Última revisión:** ver `git log -1`
- **Tests:** `python -m pytest tests/ -v`
- **Backtest rápido:** `python -m src.backtest BTC/USDT 30m 90`
- **Optimizer:** `python -m src.optimizer --days 90`

---

_Documento vivo — se actualiza automáticamente al finalizar cada sprint._

**Progreso global:** 8/18 tareas completadas (Fases 1 ✅, 2.1 ✅, 4.1 ✅, 4.2 ✅, 4.4 ✅ + 5 quick wins ✅)
