"""
OCO de protección en Binance (ccxt, endpoint orderList/oco) y Alpaca (order_class=oco),
con clientes falsos: verifica la forma de las órdenes y la lectura de su estado.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src import data_fetcher, alpaca_fetcher
from src.data_fetcher import DataFetchError
from alpaca.trading.enums import OrderClass, OrderStatus, OrderType, TimeInForce


# ── Binance ───────────────────────────────────────────────────────────────────

class FakeBinance:
    def __init__(self, order_list=None, orders=None, cancel_error=None):
        self.placed = None
        self.order_list = order_list or {}
        self.orders = orders or {}
        self.cancel_error = cancel_error
        self.cancelled = None

    def load_markets(self):
        return {}

    def market(self, symbol):
        return {"base": symbol.split("/")[0]}

    def market_id(self, symbol):
        return symbol.replace("/", "")

    def amount_to_precision(self, symbol, qty):
        return f"{qty:.1f}"

    def price_to_precision(self, symbol, price):
        return f"{price:.4f}"

    def privatePostOrderListOco(self, params):
        self.placed = params
        return {"orderListId": 4242}

    def privateGetOrderList(self, params):
        return self.order_list

    def privateGetOrder(self, params):
        return self.orders[params["orderId"]]

    def privateDeleteOrderList(self, params):
        self.cancelled = params
        if self.cancel_error:
            raise self.cancel_error
        self.order_list = {"listOrderStatus": "ALL_DONE",
                           "orders": [{"orderId": k} for k in self.orders]}
        for o in self.orders.values():
            o["status"] = "CANCELED"
        return {}


@pytest.fixture
def binance(monkeypatch):
    def install(fake, free=None):
        monkeypatch.setattr(data_fetcher, "_get_exchange", lambda: fake)
        monkeypatch.setattr(data_fetcher, "fetch_balance", lambda: {"free": free or {}})
        return fake
    return install


def test_binance_oco_is_limit_maker_tp_plus_market_stop(binance):
    fake = binance(FakeBinance(), free={"XRP": 1225.2})

    oid = data_fetcher.place_protection("XRP/USDT", 1225.2, stop_price=1.2687, take_profit=1.3464)

    assert oid == "4242"
    assert fake.placed == {
        "symbol": "XRPUSDT", "side": "SELL", "quantity": "1225.2",
        "aboveType": "LIMIT_MAKER", "abovePrice": "1.3464",
        "belowType": "STOP_LOSS", "belowStopPrice": "1.2687",
    }


def test_binance_oco_quantity_capped_to_real_balance(binance):
    """Si el fee de la compra se cobró en XRP, no se puede proteger más de lo que hay."""
    fake = binance(FakeBinance(), free={"XRP": 1223.9})
    data_fetcher.place_protection("XRP/USDT", 1225.2, 1.2687, 1.3464)
    assert fake.placed["quantity"] == "1223.9"


def test_binance_executing_list_is_open(binance):
    binance(FakeBinance(order_list={"listOrderStatus": "EXECUTING", "orders": []}))
    assert data_fetcher.get_protection_status("XRP/USDT", "4242") == {"state": "open"}


def test_binance_filled_stop_reports_average_price(binance):
    binance(FakeBinance(
        order_list={"listOrderStatus": "ALL_DONE", "orders": [{"orderId": 1}, {"orderId": 2}]},
        orders={
            1: {"type": "STOP_LOSS", "status": "FILLED",
                "executedQty": "1000.0", "cummulativeQuoteQty": "1265.0"},
            2: {"type": "LIMIT_MAKER", "status": "EXPIRED",
                "executedQty": "0", "cummulativeQuoteQty": "0"},
        }))
    st = data_fetcher.get_protection_status("XRP/USDT", "4242")
    assert st == {"state": "filled", "reason": "STOP_LOSS", "exit_price": pytest.approx(1.265)}


def test_binance_filled_take_profit(binance):
    binance(FakeBinance(
        order_list={"listOrderStatus": "ALL_DONE", "orders": [{"orderId": 2}]},
        orders={2: {"type": "LIMIT_MAKER", "status": "FILLED",
                    "executedQty": "10", "cummulativeQuoteQty": "13.464"}}))
    st = data_fetcher.get_protection_status("XRP/USDT", "4242")
    assert st["reason"] == "TAKE_PROFIT"


def test_binance_cancel_releases_and_reports_canceled(binance):
    fake = binance(FakeBinance(
        order_list={"listOrderStatus": "EXECUTING", "orders": [{"orderId": 1}]},
        orders={1: {"type": "STOP_LOSS", "status": "NEW", "executedQty": "0",
                    "cummulativeQuoteQty": "0"}}))
    assert data_fetcher.cancel_protection("XRP/USDT", "4242") == {"state": "canceled"}
    assert fake.cancelled == {"symbol": "XRPUSDT", "orderListId": "4242"}


def test_binance_cancel_of_already_filled_oco_returns_fill(binance):
    binance(FakeBinance(
        cancel_error=DataFetchError("binance {\"code\":-2011,\"msg\":\"Unknown order sent.\"}"),
        order_list={"listOrderStatus": "ALL_DONE", "orders": [{"orderId": 2}]},
        orders={2: {"type": "LIMIT_MAKER", "status": "FILLED",
                    "executedQty": "10", "cummulativeQuoteQty": "13.5"}}))
    st = data_fetcher.cancel_protection("XRP/USDT", "4242")
    assert st["state"] == "filled" and st["exit_price"] == pytest.approx(1.35)


# ── Alpaca ────────────────────────────────────────────────────────────────────

def _order(type_, status, price=None):
    return SimpleNamespace(type=type_, status=status, filled_avg_price=price)


def test_alpaca_oco_request_shape(monkeypatch):
    sent = []
    monkeypatch.setattr(alpaca_fetcher.trading_client, "submit_order",
                        lambda req: sent.append(req) or SimpleNamespace(id="abc-123"))

    oid = alpaca_fetcher.place_protection("AAPL", 47, stop_price=334.7012, take_profit=355.1888)

    assert oid == "abc-123"
    req = sent[0]
    assert req.order_class == OrderClass.OCO and req.time_in_force == TimeInForce.GTC
    assert req.side.value == "sell" and req.qty == 47
    assert req.stop_loss.stop_price == 334.70 and req.stop_loss.limit_price is None  # stop a mercado
    assert req.take_profit.limit_price == 355.19


@pytest.mark.parametrize("orders, expected", [
    ([_order(OrderType.LIMIT, OrderStatus.FILLED, "355.2"), _order(OrderType.STOP, OrderStatus.CANCELED)],
     {"state": "filled", "reason": "TAKE_PROFIT", "exit_price": 355.2}),
    ([_order(OrderType.LIMIT, OrderStatus.CANCELED), _order(OrderType.STOP, OrderStatus.FILLED, "333.9")],
     {"state": "filled", "reason": "STOP_LOSS", "exit_price": 333.9}),
    ([_order(OrderType.LIMIT, OrderStatus.NEW), _order(OrderType.STOP, OrderStatus.HELD)],
     {"state": "open"}),
    ([_order(OrderType.LIMIT, OrderStatus.CANCELED), _order(OrderType.STOP, OrderStatus.CANCELED)],
     {"state": "canceled"}),
])
def test_alpaca_oco_status(orders, expected):
    assert alpaca_fetcher._oco_status(orders) == expected


def test_alpaca_cancel_waits_until_released(monkeypatch):
    states = iter([OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED])
    monkeypatch.setattr(alpaca_fetcher.trading_client, "cancel_order_by_id", lambda oid: None)

    def get_order_by_id(oid, filter=None):
        s = next(states)
        return SimpleNamespace(type=OrderType.LIMIT, status=s, filled_avg_price=None,
                               legs=[SimpleNamespace(type=OrderType.STOP, status=s,
                                                     filled_avg_price=None)])
    monkeypatch.setattr(alpaca_fetcher.trading_client, "get_order_by_id", get_order_by_id)
    monkeypatch.setattr(alpaca_fetcher.time, "sleep", lambda s: None)

    assert alpaca_fetcher.cancel_protection("AAPL", "abc-123") == {"state": "canceled"}
