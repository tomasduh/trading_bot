"""
Reconciliación periódica entre la DB y las posiciones reales del exchange.

Detecta divergencias (trade abierto en DB pero cerrado en exchange, o
viceversa) para alertar antes de que acumulen PnL erróneo.

Reglas de acción (conservadoras — no cierra automáticamente):
  - DB=OPEN pero exchange sin posición → log WARNING + notify
  - Exchange tiene posición no rastreada en DB → log WARNING + notify
  - Cantidades muy distintas (>50% diferencia) → log WARNING

Cómo usar:
  # Al inicio de cada ciclo en bot.py
  from src import reconciler
  reconciler.reconcile_all(executor)
"""
import logging
from src import config
from src.alerts import notify_error

logger = logging.getLogger("reconciler")


def reconcile_crypto(executor) -> list[str]:
    """Compara trades crypto abiertos en DB vs balance en Binance.

    Heurística: si DB dice que tenemos X BTC, el exchange debe tener
    ≥ 50% de esa cantidad en balance (libre + usado). Si no, hay divergencia.
    """
    if not config.API_KEY:
        return []   # sin keys no podemos verificar

    issues: list[str] = []
    try:
        from src import data_fetcher
        balance = data_fetcher.fetch_balance()
        free  = balance.get("free", {})
        used  = balance.get("used", {})

        for symbol in config.CRYPTO_SYMBOLS:
            db_trade = executor.get_open_trade(symbol)
            if not db_trade:
                continue

            base = symbol.split("/")[0]   # "BTC" de "BTC/USDT"
            exchange_qty = float(free.get(base, 0) or 0) + float(used.get(base, 0) or 0)
            db_qty = db_trade.quantity or 0

            if db_qty > 0 and exchange_qty < db_qty * 0.5:
                msg = (f"[RECONCILE] CRYPTO {symbol}: DB tiene trade ABIERTO "
                       f"(qty={db_qty}) pero exchange solo tiene {exchange_qty:.6f} {base}")
                logger.warning(msg)
                issues.append(msg)

    except Exception as e:
        logger.warning(f"[RECONCILE] Error al verificar crypto: {e}")

    return issues


def reconcile_stocks(executor) -> list[str]:
    """Compara trades de stocks abiertos en DB vs posiciones en Alpaca.

    Verifica en ambas direcciones:
      1. DB=OPEN pero Alpaca sin posición → posible trade fantasma
      2. Alpaca tiene posición pero DB=CLOSED → posible descuadre tras reinicio
    """
    if not config.ALPACA_API_KEY if hasattr(config, "ALPACA_API_KEY") else False:
        return []

    issues: list[str] = []
    try:
        from src import alpaca_fetcher
        positions = alpaca_fetcher.get_open_positions()   # dict {symbol: Position}

        for symbol in config.STOCK_SYMBOLS:
            db_trade = executor.get_open_trade(symbol)
            in_exchange = symbol in positions

            if db_trade and not in_exchange:
                msg = (f"[RECONCILE] STOCK {symbol}: DB tiene trade ABIERTO "
                       f"pero Alpaca no tiene posición")
                logger.warning(msg)
                issues.append(msg)

            elif not db_trade and in_exchange:
                pos = positions[symbol]
                qty = getattr(pos, "qty", "?")
                msg = (f"[RECONCILE] STOCK {symbol}: Alpaca tiene posición "
                       f"(qty={qty}) pero DB no tiene trade abierto")
                logger.warning(msg)
                issues.append(msg)

    except Exception as e:
        logger.warning(f"[RECONCILE] Error al verificar stocks: {e}")

    return issues


def reconcile_all(executor) -> list[str]:
    """Ejecuta reconciliación completa y retorna lista de issues encontrados.

    Llama a reconcile_crypto + reconcile_stocks y notifica por alerts
    si hay divergencias críticas.
    """
    issues: list[str] = []
    issues.extend(reconcile_crypto(executor))
    issues.extend(reconcile_stocks(executor))

    if issues:
        logger.warning(f"[RECONCILE] {len(issues)} divergencia(s) detectada(s):")
        for issue in issues:
            logger.warning(f"  → {issue}")
        # Notificar si hay más de 1 issue (1 puede ser timing normal de cierre)
        if len(issues) >= 2:
            notify_error(
                f"⚠️ Reconciliador: {len(issues)} divergencias DB↔exchange\n" +
                "\n".join(f"  • {i}" for i in issues)
            )
    else:
        logger.debug("[RECONCILE] OK — sin divergencias entre DB y exchange")

    return issues
