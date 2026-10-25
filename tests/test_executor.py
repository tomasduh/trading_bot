"""
Tests de TradeExecutor con DB temporal y exchange simulado (nunca toca data/trades.db).
"""
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src import config, executor as executor_mod
from src.database import Base, Trade
from src.executor import TradeExecutor, _buy_fill


def _executor_with(open_trades: dict) -> TradeExecutor:
    ex = TradeExecutor.__new__(TradeExecutor)   # salta _load_open_trades (DB)
    ex._open_trades = open_trades
    ex._close_failures = {}
    ex._protect_failures = set()
    return ex


def test_legacy_open_symbols_only_returns_unconfigured_open_trades():
    """Un símbolo quitado de config con trade abierto debe seguir gestionándose."""
    assert "TSLA" not in config.STOCK_SYMBOLS
    assert "GOOGL" not in config.STOCK_SYMBOLS
    ex = _executor_with({
        "AAPL":     Trade(symbol="AAPL", side="BUY", status="OPEN"),  # en config
        "TSLA":     Trade(symbol="TSLA", side="BUY", status="OPEN"),  # quitado, abierto
        "GOOGL":    None,                                              # quitado, sin trade
        "ADA/USDT": Trade(symbol="ADA/USDT", side="BUY", status="OPEN"),
    })
    assert ex.legacy_open_symbols(stocks=True) == ["TSLA"]
    assert ex.legacy_open_symbols(stocks=False) == ["ADA/USDT"]


def _trade(symbol, entry, sl, qty):
    return Trade(symbol=symbol, side="BUY", status="OPEN",
                 entry_price=entry, stop_loss=sl, quantity=qty)


def test_exposure_is_measured_per_market():
    """Caso visto en prod: ~$70k en stocks bloqueaban entradas crypto con $7.9k USDT."""
    ex = _executor_with({
        "AAPL":     _trade("AAPL", 350.0, 343.0, 200),        # $70k nocional, riesgo $1,400
        "BTC/USDT": _trade("BTC/USDT", 80_000.0, 78_400.0, 0.025),  # $2k nocional, riesgo $40
    })
    crypto = ex.total_exposure_pct(7_930.0, stocks=False)
    stocks = ex.total_exposure_pct(30_000.0, stocks=True)

    assert crypto == pytest.approx(40 / (7_930 + 2_000))
    assert stocks == pytest.approx(1_400 / (30_000 + 70_000))
    assert crypto < config.MAX_TOTAL_EXPOSURE_PCT       # crypto ya no queda bloqueado


def test_exposure_does_not_inflate_as_cash_gets_invested():
    """El capital es cash libre + nocional abierto: invertir no cambia el denominador."""
    ex = _executor_with({"ETH/USDT": _trade("ETH/USDT", 2_000.0, 1_960.0, 1.0)})
    # 10k de cuenta: 8k libres + 2k invertidos → riesgo $40 sobre $10k
    assert ex.total_exposure_pct(8_000.0, stocks=False) == pytest.approx(40 / 10_000)
    assert ex.total_exposure_pct(8_000.0, stocks=True) == 0.0


# ── Órdenes reales simuladas ──────────────────────────────────────────────────

@pytest.fixture
def live(monkeypatch, tmp_path):
    """DB temporal + credenciales 'presentes' + exchange y alertas falsos."""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(executor_mod, "get_session", lambda: Session(engine))
    monkeypatch.setattr(executor_mod, "_has_api_credentials_for", lambda s: True)

    exchange = SimpleNamespace(orders=[], calls=[], fail=False, response={},
                               protections=[], protect_fail=False,
                               status={"state": "open"}, cancel_result={"state": "canceled"})

    def create_market_order(symbol, side, qty):
        exchange.orders.append((symbol, side, qty))
        exchange.calls.append("sell" if side == "sell" else "buy")
        if exchange.fail:
            raise RuntimeError("insufficient balance")
        return dict(exchange.response)

    def place_protection(symbol, qty, stop_price, take_profit):
        exchange.calls.append("protect")
        if exchange.protect_fail:
            raise RuntimeError("stop would trigger immediately")
        exchange.protections.append((symbol, qty, stop_price, take_profit))
        return f"oco-{len(exchange.protections)}"

    def get_protection_status(symbol, order_id):
        return dict(exchange.status)

    def cancel_protection(symbol, order_id):
        exchange.calls.append("cancel")
        return dict(exchange.cancel_result)

    exchange.create_market_order = create_market_order
    exchange.place_protection = place_protection
    exchange.get_protection_status = get_protection_status
    exchange.cancel_protection = cancel_protection
    monkeypatch.setattr(executor_mod, "_get_fetcher_for", lambda s: exchange)

    sent = SimpleNamespace(errors=[], infos=[])
    monkeypatch.setattr(executor_mod.alerts, "notify_error", sent.errors.append)
    monkeypatch.setattr(executor_mod.alerts, "notify_info", sent.infos.append)

    ex = _executor_with({})
    return SimpleNamespace(ex=ex, exchange=exchange, sent=sent, engine=engine)


def _open(live, symbol="BTC/USDT", entry=100.0, qty=1.0, protect_id=None) -> Trade:
    with Session(live.engine) as s:
        t = Trade(symbol=symbol, side="BUY", status="OPEN", entry_price=entry,
                  quantity=qty, stop_loss=entry * 0.98, take_profit=entry * 1.04,
                  protect_order_id=protect_id)
        s.add(t)
        s.commit()
        s.refresh(t)
        s.expunge(t)
    live.ex._open_trades[symbol] = t
    return t


def _db_trade(live, trade_id) -> Trade:
    with Session(live.engine) as s:
        return s.get(Trade, trade_id)


def test_failed_sell_keeps_trade_open_and_alerts_once(live):
    t = _open(live)
    live.exchange.fail = True

    assert live.ex.close_trade("BTC/USDT", 98.0, "STOP_LOSS") is None
    assert live.ex.close_trade("BTC/USDT", 97.5, "STOP_LOSS") is None

    assert live.ex.has_open_trade("BTC/USDT")
    assert _db_trade(live, t.id).status == "OPEN"
    assert len(live.sent.errors) == 1          # no spamea en cada reintento

    live.exchange.fail = False
    live.exchange.response = {"average": 97.0}
    closed = live.ex.close_trade("BTC/USDT", 97.5, "STOP_LOSS")
    assert closed is not None and closed.status == "CLOSED"
    assert not live.ex.has_open_trade("BTC/USDT")
    assert live.sent.infos                     # avisa que se recuperó


def test_close_records_fill_price_and_crypto_fees(live):
    t = _open(live, entry=100.0, qty=1.0)
    live.exchange.response = {"average": 104.0}

    closed = live.ex.close_trade("BTC/USDT", 103.0, "TAKE_PROFIT")

    assert closed.exit_price == 104.0          # fill real, no el precio de la vela
    fees = (100.0 + 104.0) * 1.0 * config.FEE_PCT["BTC/USDT"]
    assert closed.pnl_usdt == pytest.approx(4.0 - fees)
    assert _db_trade(live, t.id).exit_price == 104.0


def test_stock_close_has_no_fee(live):
    _open(live, symbol="AAPL", entry=200.0, qty=2.0)
    live.exchange.response = {"average": 202.0, "filled": 2.0}

    closed = live.ex.close_trade("AAPL", 201.0, "SIGNAL")

    assert closed.pnl_usdt == pytest.approx(4.0)


def test_open_uses_fill_price_for_entry_and_stops(live):
    live.exchange.response = {"average": 101.0, "filled": 1.0}
    signal = SimpleNamespace(price=100.0, atr=0.0, reason="test")

    trade = live.ex.open_trade("AAPL", signal, capital_usdt=100_000)

    assert trade.entry_price == 101.0
    assert trade.stop_loss == pytest.approx(101.0 * (1 - config.STOP_LOSS_PCT))
    assert trade.take_profit == pytest.approx(101.0 * (1 + config.TAKE_PROFIT_PCT))


def test_buy_fill_deducts_fee_charged_in_base_asset():
    order = {"average": 100.5, "filled": 0.01, "fee": {"cost": 0.00001, "currency": "BTC"}}
    price, qty = _buy_fill("BTC/USDT", order, 100.0, 0.01)
    assert price == 100.5
    assert qty == pytest.approx(0.00999)


def test_buy_fill_keeps_qty_when_fee_paid_in_bnb_or_missing():
    order = {"average": 100.5, "filled": 0.01, "fee": {"cost": 0.0001, "currency": "BNB"}}
    assert _buy_fill("BTC/USDT", order, 100.0, 0.01) == (100.5, 0.01)
    assert _buy_fill("BTC/USDT", {}, 100.0, 0.01) == (100.0, 0.01)   # sin datos → fallback


# ── Protección OCO en el exchange ─────────────────────────────────────────────

def test_open_places_oco_at_fill_based_levels(live):
    live.exchange.response = {"average": 101.0, "filled": 3.0}
    signal = SimpleNamespace(price=100.0, atr=0.0, reason="test")

    trade = live.ex.open_trade("AAPL", signal, capital_usdt=100_000)

    assert live.exchange.calls == ["buy", "protect"]
    (sym, qty, sl, tp), = live.exchange.protections
    assert (sym, qty) == ("AAPL", trade.quantity)
    assert sl == pytest.approx(101.0 * (1 - config.STOP_LOSS_PCT))
    assert tp == pytest.approx(101.0 * (1 + config.TAKE_PROFIT_PCT))
    assert _db_trade(live, trade.id).protect_order_id == "oco-1"


def test_sync_records_exchange_fill_without_selling(live):
    t = _open(live, entry=100.0, qty=1.0, protect_id="oco-9")
    live.exchange.status = {"state": "filled", "reason": "STOP_LOSS", "exit_price": 97.9}

    closed = live.ex.sync_protection("BTC/USDT")

    assert closed.exit_reason == "STOP_LOSS" and closed.exit_price == 97.9
    assert live.exchange.orders == []                 # el exchange ya vendió
    db = _db_trade(live, t.id)
    assert db.status == "CLOSED" and db.protect_order_id is None


def test_open_oco_is_left_alone(live):
    _open(live, protect_id="oco-9")
    assert live.ex.sync_protection("BTC/USDT") is None
    assert live.ex.has_open_trade("BTC/USDT")


def test_close_cancels_oco_before_selling(live):
    _open(live, protect_id="oco-9")
    live.exchange.response = {"average": 99.5}

    closed = live.ex.close_trade("BTC/USDT", 99.6, "SIGNAL")

    assert live.exchange.calls == ["cancel", "sell"]
    assert closed.exit_reason == "SIGNAL" and closed.exit_price == 99.5


def test_close_uses_exchange_fill_when_oco_won_the_race(live):
    _open(live, protect_id="oco-9")
    live.exchange.cancel_result = {"state": "filled", "reason": "TAKE_PROFIT", "exit_price": 104.1}

    closed = live.ex.close_trade("BTC/USDT", 104.0, "TAKE_PROFIT")

    assert live.exchange.calls == ["cancel"]           # no vende dos veces
    assert closed.exit_price == 104.1


def test_close_fails_safely_if_oco_cannot_be_cancelled(live):
    t = _open(live, protect_id="oco-9")
    live.exchange.cancel_result = {"state": "open"}

    assert live.ex.close_trade("BTC/USDT", 97.0, "STOP_LOSS") is None

    assert live.exchange.orders == []
    assert _db_trade(live, t.id).status == "OPEN"
    assert _db_trade(live, t.id).protect_order_id == "oco-9"
    assert len(live.sent.errors) == 1


def test_externally_cancelled_oco_is_replaced(live):
    t = _open(live, protect_id="oco-9")
    live.exchange.status = {"state": "canceled"}

    assert live.ex.sync_protection("BTC/USDT") is None
    assert _db_trade(live, t.id).protect_order_id is None

    live.ex.ensure_protection("BTC/USDT")
    assert _db_trade(live, t.id).protect_order_id == "oco-1"


def test_protection_failure_alerts_once_then_recovers(live):
    t = _open(live)
    live.exchange.protect_fail = True

    live.ex.ensure_protection("BTC/USDT")
    live.ex.ensure_protection("BTC/USDT")
    assert len(live.sent.errors) == 1
    assert _db_trade(live, t.id).protect_order_id is None

    live.exchange.protect_fail = False
    live.ex.ensure_protection("BTC/USDT")
    assert _db_trade(live, t.id).protect_order_id == "oco-1"
    assert live.sent.infos                            # avisa que ya quedó protegida
