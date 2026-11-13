"""Smoke checks with simulated KuCoin responses. Run python src/check_grid_bot.py."""

from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests

import config
import main
from grid_bot import BUY, INITIAL, SELL, GridBot
from kucoin_rest import KuCoinError, KuCoinREST

D = Decimal


class Exchange:
    live = True

    def __init__(self):
        self.orders = []
        self.active = False
        self.fills_so_far = []
        self.cancellations = 0
        self.post_error = self.cancel_error = None
        self.hide_order = False

    def spot_accounts(self):
        return {"USDT": {"id": "account", "available": str(INITIAL * 10)},
                "USDC": {"available": str(INITIAL * 10)}}

    def symbol_rules(self, symbol):
        return {"enableTrading": True, "baseCurrency": "USDT", "quoteCurrency": "USDC",
                "priceIncrement": "0.0001", "feeCurrency": "USDC", "baseIncrement": "0.01",
                "baseMinSize": "0.01", "quoteMinSize": "1", "minFunds": "1",
                "baseMaxSize": "100000", "quoteMaxSize": "100000"}

    def fee_rates(self, symbol):
        return {"makerFeeRate": "0", "takerFeeRate": "0"}

    def open_orders(self, symbol):
        return [self.orders[-1]] if self.active and not self.hide_order else []

    def place_order(self, symbol, side, price, size, client_oid):
        assert not self.active
        order_id = str(len(self.orders) + 1)
        self.orders.append({"symbol": symbol, "side": side, "price": str(price), "size": str(size),
                            "clientOid": client_oid, "id": order_id, "type": "limit", "postOnly": True})
        self.active = True
        self.fills_so_far = []
        if self.post_error:
            raise self.post_error
        return order_id

    def order(self, symbol, client_oid, order_id):
        if self.hide_order:
            raise KuCoinError("Order does not exist", "400100")
        order = self.orders[-1]
        assert client_oid == order["clientOid"] and order_id in (None, order["id"])
        size = sum(D(fill["size"]) for fill in self.fills_so_far)
        funds = sum(D(fill["funds"]) for fill in self.fills_so_far)
        return order | {"active": self.active, "dealSize": str(size), "dealFunds": str(funds),
                        "cancelExist": not self.active, "cancelledSize": str(D(order["size"]) - size)}

    def fills(self, symbol, order_id):
        return self.fills_so_far.copy()

    def cancel_order(self, symbol, order_id):
        assert self.active and order_id == self.orders[-1]["id"]
        self.cancellations += 1
        if self.cancel_error:
            raise self.cancel_error

    def fill(self, size=None, *, fee="0", currency="USDC", close=False):
        order = self.orders[-1]
        size = D(order["size"] if size is None else size)
        self.fills_so_far.append({"orderId": order["id"], "size": str(size),
                                  "funds": str(size * D(order["price"])), "liquidity": "maker",
                                  "fee": fee, "feeCurrency": currency})
        self.active = not close


def expect_error(call, message):
    try:
        call()
    except KuCoinError as error:
        assert message in str(error), error
    else:
        raise AssertionError(f"Expected {message}")


def partial_sell(path):
    api = Exchange()
    bot = GridBot(api, path)
    bot.poll()
    api.fill("300", fee="0.3")
    with patch("grid_bot.time.time", return_value=100):
        bot.poll()
    assert bot.state["order"]["execution"]["fees"]["USDC"] == "0.3"
    assert bot.state["order"]["partial_since"] == 100
    with patch("grid_bot.time.time", return_value=100 + config.PARTIAL_FILL_TIMEOUT_SECONDS - 1):
        bot.poll()
    assert api.cancellations == 0
    api.cancel_error = KuCoinError("Cancellation timed out", retryable=True)
    with patch("grid_bot.time.time", return_value=100 + config.PARTIAL_FILL_TIMEOUT_SECONDS):
        bot = GridBot(api, path)
        expect_error(bot.poll, "Cancellation timed out")
    assert api.cancellations == 1 and len(api.orders) == 1
    bot = GridBot(api, path)
    with patch("grid_bot.time.time", return_value=101 + config.PARTIAL_FILL_TIMEOUT_SECONDS):
        bot.poll()
    assert api.cancellations == 1 and len(api.orders) == 1
    api.fill("100", fee="0.1", close=True)
    with patch.object(api, "fills", return_value=api.fills_so_far[:1]):
        bot.poll()
    assert len(api.orders) == 1 and bot.state["order"]["id"] == "1"
    assert D(bot.state["usdt"]) == INITIAL
    bot.poll()
    net = D(400) * SELL - D("0.4")
    assert D(bot.state["recovery"]["amount"]) == net
    assert D(bot.state["usdt"]) == INITIAL - D(400)
    bot = GridBot(api, path)
    bot.poll()
    bought = net / BUY // D("0.01") * D("0.01")
    assert D(api.orders[-1]["size"]) == bought
    api.fill(close=True, fee="0.4", currency="USDT")
    bot.poll()
    assert bot.state["recovery"] is None
    assert D(bot.state["usdt"]) == INITIAL - D(400) + bought - D("0.4")
    assert bot.state["cycles"] == 1 and D(bot.state["realized"]) > 0
    bot.poll()
    assert api.orders[-1]["side"] == "sell" and D(api.orders[-1]["size"]) == INITIAL


def partial_buy(path):
    api = Exchange()
    bot = GridBot(api, path)
    bot.poll()
    api.fill(close=True)
    bot.poll()
    bot.poll()
    api.fill("400", fee="0.4", currency="USDT")
    with patch("grid_bot.time.time", return_value=100):
        bot.poll()
    with patch("grid_bot.time.time", return_value=100 + config.PARTIAL_FILL_TIMEOUT_SECONDS):
        bot.poll()
    assert api.cancellations == 1 and len(api.orders) == 2
    api.active = False
    bot.poll()
    idle_usdc = INITIAL * SELL - D(400) * BUY
    assert D(bot.state["usdc"]) == idle_usdc
    bot.poll()
    assert D(api.orders[-1]["size"]) == D("399.60")
    api.fill(close=True)
    bot.poll()
    assert bot.state["recovery"] is None
    bot.poll()
    budget = min(idle_usdc + D("399.60") * SELL, INITIAL * SELL)
    assert D(api.orders[-1]["size"]) == budget / BUY // D("0.01") * D("0.01")


def safety(directory):
    api = Exchange()
    bot = GridBot(api, directory / "zero.json")
    bot.poll()
    api.active = False
    bot.poll()
    bot.poll()
    assert len(api.orders) == 2 and all(order["side"] == "sell" for order in api.orders)
    assert D(api.orders[-1]["price"]) == SELL and D(api.orders[-1]["size"]) == INITIAL

    api = Exchange()
    api.post_error = KuCoinError("Submission timed out", retryable=True)
    path = directory / "timeout.json"
    expect_error(GridBot(api, path).poll, "Submission timed out")
    bot = GridBot(api, path)
    api.hide_order = True
    bot.poll()
    assert bot.state["order"]["id"] is None and len(api.orders) == 1
    api.hide_order = False
    bot.poll()
    assert bot.state["order"]["id"] == "1" and len(api.orders) == 1

    api = Exchange()
    bot = GridBot(api, directory / "fee.json")
    bot.poll()
    api.fill("50", fee="0.1", currency="KCS", close=True)
    expect_error(bot.poll, "KCS")
    assert bot.state["order"]["id"] == "1" and len(api.orders) == 1

    api = Exchange()
    bot = GridBot(api, directory / "minimum.json")
    bot.poll()
    api.fill("0.01", close=True)
    bot.poll()
    expect_error(bot.poll, "below KuCoin's order minimum")
    assert bot.side == "buy" and len(api.orders) == 1


def spend_currency_fee(path):
    api = Exchange()
    api.fee_rates = lambda symbol: {"makerFeeRate": "0.001", "takerFeeRate": "0.001"}
    bot = GridBot(api, path)
    bot.poll()
    size = D(api.orders[-1]["size"])
    fee = size * D("0.001")
    assert size * D("1.001") <= INITIAL
    api.fill(close=True, fee=str(fee), currency="USDT")
    bot.poll()
    assert D(bot.state["usdt"]) == INITIAL - size - fee


def recovery(directory):
    # Drive the real REST error classifier and polling loop with a simulated clock.
    class Response:
        def __init__(self, status, code, *, headers=None, data=None):
            self.status_code, self.code, self.data = status, code, data
            self.ok = status == 200
            self.headers = headers or {}

        def json(self):
            return {"code": self.code, "data": self.data}

    replies = [
        requests.ConnectionError("Wi-Fi offline"), Response(503, "500000"),
        requests.Timeout("Timeout"), Response(503, "500000"), Response(503, "500000"),
        Response(429, "429000", headers={"gw-ratelimit-reset": "45000"}),
        Response(200, "200000", data=[]), Response(403, "400007"),
    ]
    clock = [0]
    calls = []

    class Session:
        def request(self, method, url, **kwargs):
            assert method == "GET" and url.startswith("https://api.kucoin.com/api/v1/accounts?")
            assert kwargs["timeout"] == (3, 5)
            calls.append(clock[0])
            response = replies.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    def sleep(seconds):
        clock[0] += seconds

    env = {"KUCOIN_API_KEY": "smoke", "KUCOIN_API_SECRET": "smoke",
           "KUCOIN_API_PASSPHRASE": "smoke"}
    messages = StringIO()
    with (patch.dict("os.environ", env), patch("main.time.monotonic", side_effect=lambda: clock[0]),
          patch("main.time.sleep", side_effect=sleep), patch("main.market_report"), redirect_stdout(messages)):
        api = KuCoinREST(Session(), live=True)
        expect_error(lambda: main.run(api, GridBot(api, directory / "offline.json")), "400007")
    assert calls == [0, 2, 6, 14, 30, 60, 105, 115]
    assert messages.getvalue().count("connectivity interrupted") == 1
    assert messages.getvalue().count("connectivity restored") == 1


def report_fee():
    api = Mock(spec=KuCoinREST)
    api.live = False
    api.klines.return_value = []
    api.spot_accounts.return_value = {}
    api.fee_rates.return_value = {"makerFeeRate": "0.000123"}
    bot = GridBot(api)

    with redirect_stdout(StringIO()) as messages, patch("main.time.sleep", side_effect=KeyboardInterrupt):
        try:
            main.run(api, bot)
        except KeyboardInterrupt:
            pass
    assert "Preview." in messages.getvalue() and "maker fee" not in messages.getvalue().lower()
    api.fee_rates.assert_not_called()

    api.live = True
    with redirect_stdout(StringIO()) as messages:
        main.market_report(api, bot)
    assert "Maker fee:           0.0123%" in messages.getvalue()
    api.fee_rates.assert_called_once_with(config.SYMBOL)

    api.fee_rates.side_effect = KuCoinError("Fee lookup failed.")
    with redirect_stdout(StringIO()) as messages:
        main.market_report(api, bot)
    assert "Maker fee:           Unavailable (Fee lookup failed.)" in messages.getvalue()


if __name__ == "__main__":
    report_fee()
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        partial_sell(directory / "sell.json")
        partial_buy(directory / "buy.json")
        safety(directory)
        spend_currency_fee(directory / "base-fee.json")
        recovery(directory)
    print("Grid bot smoke checks passed.")
