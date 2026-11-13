import base64
import hmac
import json
import os
import time
from decimal import Decimal
from urllib.parse import unquote_plus, urlencode

import requests


class KuCoinError(RuntimeError):
    def __init__(self, message: str, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    @property
    def order_rejected(self) -> bool:
        if self.retryable:
            return False
        if self.code == "400100":
            # Some order validation failures share this code with other errors.
            message = str(self).lower()
            return not any(word in message for word in ("duplicate", "clientoid", "client order id"))
        return self.code in {
            "200001", "200003", "200004", "200009", "400200",
            "400500", "400600", "400700", "411100", "600203",
        }


class KuCoinREST:
    BASE_URL = "https://api.kucoin.com"
    ORDERS = "/api/v1/hf/orders"

    def __init__(self, session: requests.Session, *, live: bool = False):
        self.session = session
        self.live = live
        if live:
            names = ("KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_API_PASSPHRASE")
            self.key, secret, passphrase = (os.environ.get(name, "") for name in names)
            if not all((self.key, secret, passphrase)):
                raise ValueError(f"Set {', '.join(names)} before enabling live trading.")
            self.secret = secret.encode()
            self.passphrase = self._sign(passphrase)
            self.version = os.environ.get("KUCOIN_API_KEY_VERSION", "2")

    def _sign(self, value: str) -> str:
        digest = hmac.digest(self.secret, value.encode(), "sha256")
        return base64.b64encode(digest).decode("ascii")

    def _request(self, method: str, path: str, *, params=None, payload=None,
                 private: bool = False, attempts: int = 1):
        endpoint = f"{path}?{urlencode(params)}" if params else path
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        headers = {"Content-Type": "application/json"}
        if private and not self.live:
            raise ValueError("Private KuCoin requests require live mode.")

        for attempt in range(attempts):
            if private:
                timestamp = str(time.time_ns() // 1_000_000)
                headers.update({
                    "KC-API-KEY": self.key,
                    "KC-API-KEY-VERSION": self.version,
                    "KC-API-TIMESTAMP": timestamp,
                    "KC-API-PASSPHRASE": self.passphrase,
                    "KC-API-SIGN": self._sign(f"{timestamp}{method}{unquote_plus(endpoint)}{body}"),
                })
            try:
                response = self.session.request(
                    method, self.BASE_URL + endpoint, data=body or None,
                    headers=headers, timeout=(3, 5), allow_redirects=False,
                )
            except requests.RequestException as exc:
                error = KuCoinError(f"{method} {path}: {type(exc).__name__}", retryable=True)
            else:
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and response.ok and data.get("code") == "200000":
                    if "data" in data:
                        return data["data"]
                code = str(data.get("code", "")) if isinstance(data, dict) else ""
                message = data.get("msg", "Invalid response") if isinstance(data, dict) else "Invalid response"
                status = response.status_code
                retryable = status >= 500 or status in (408, 429) or code in {"429000", "500000", "230005"}
                error = KuCoinError(f"{method} {path}: HTTP {status}, {code} {message}",
                                    code, retryable)
            if not error.retryable or attempt + 1 == attempts:
                raise error
            time.sleep(1)

    def klines(self, symbol: str, interval: str, start: int, end: int) -> list:
        """Fetch up to 1,500 candles for a Unix-second time window."""
        return self._request("GET", "/api/v1/market/candles", params={
            "symbol": symbol, "type": interval, "startAt": start, "endAt": end,
        })

    def symbol_rules(self, symbol: str) -> dict:
        return self._request("GET", f"/api/v2/symbols/{symbol}", attempts=2)

    def spot_accounts(self) -> dict:
        accounts = self._request("GET", "/api/v1/accounts", params={"type": "trade"},
                                 private=True)
        return {account["currency"]: account for account in accounts if account["type"] == "trade"}

    def fee_rates(self, symbol: str) -> dict:
        rates = self._request("GET", "/api/v1/trade-fees", params={"symbols": symbol},
                              private=True)
        for rate in rates:
            if rate["symbol"] == symbol:
                return rate
        raise KuCoinError(f"No Spot fee rates returned for {symbol}.")

    def open_orders(self, symbol: str) -> list:
        orders = self._request("GET", f"{self.ORDERS}/active", params={"symbol": symbol},
                               private=True)
        if not isinstance(orders, list):
            raise KuCoinError("Invalid response for open Spot orders.")
        return orders

    def place_order(self, symbol: str, side: str, price: Decimal, size: Decimal,
                    client_oid: str) -> str:
        data = self._request("POST", self.ORDERS, private=True, payload={
            "symbol": symbol, "side": side, "type": "limit", "timeInForce": "GTC",
            "postOnly": True, "price": format(price, "f"), "size": format(size, "f"),
            "clientOid": client_oid,
        })
        if not isinstance(data, dict) or not data.get("orderId"):
            raise KuCoinError("KuCoin did not return an order ID.")
        return data["orderId"]

    def order(self, symbol: str, client_oid: str, order_id: str | None) -> dict:
        path = order_id or f"client-order/{client_oid}"
        return self._request("GET", f"{self.ORDERS}/{path}", params={"symbol": symbol},
                             private=True)

    def fills(self, symbol: str, order_id: str) -> list:
        end = time.time_ns() // 1_000_000
        params = {"symbol": symbol, "orderId": order_id, "limit": 100,
                  "startAt": end - 7 * 24 * 60 * 60 * 1000, "endAt": end}
        fills = {}
        while True:
            page = self._request("GET", "/api/v1/hf/fills", params=params, private=True)
            items = page["items"]
            fills.update((item["id"], item) for item in items)
            cursor = page.get("lastId")
            if len(items) < params["limit"] or not cursor:
                return list(fills.values())
            if cursor == params.get("lastId"):
                raise KuCoinError(f"Fill pagination stalled for order {order_id}.")
            params["lastId"] = cursor

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        """Request cancellation. Check order status to confirm it."""
        return self._request("DELETE", f"{self.ORDERS}/{order_id}", params={"symbol": symbol},
                             private=True)
