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

    exchange = SimpleNamespace(orders=[], fail=False, response={})

    def create_market_order(symbol, side, qty):
        exchange.orders.append((symbol, side, qty))
        if exchange.fail:
            raise RuntimeError("insufficient balance")
        return dict(exchange.response)
    exchange.create_market_order = create_market_order
    monkeypatch.setattr(executor_mod, "_get_fetcher_for", lambda s: exchange)

    sent = SimpleNamespace(errors=[], infos=[])
    monkeypatch.setattr(executor_mod.alerts, "notify_error", sent.errors.append)
    monkeypatch.setattr(executor_mod.alerts, "notify_info", sent.infos.append)

    ex = _executor_with({})
    return SimpleNamespace(ex=ex, exchange=exchange, sent=sent, engine=engine)


def _open(live, symbol="BTC/USDT", entry=100.0, qty=1.0) -> Trade:
    with Session(live.engine) as s:
        t = Trade(symbol=symbol, side="BUY", status="OPEN", entry_price=entry,
                  quantity=qty, stop_loss=entry * 0.98, take_profit=entry * 1.04)
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
