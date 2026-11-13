import base64
import hmac
import json
import os
import time
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import unquote_plus, urlencode

import requests


class KuCoinError(RuntimeError):
    def __init__(self, message: str, code: str = "", retryable: bool = False, *,
                 retry_after: float = 0, not_sent: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.not_sent = not_sent

    @property
    def order_rejected(self) -> bool:
        if self.retryable:
            return False
        if self.code == "400100":
            return any(reason in str(self).lower() for reason in
                       ("account.available.amount", "insufficient balance", "post-only", "post only"))
        return self.code in {"200001", "200003", "200004", "400600", "600203"}

    @property
    def order_missing(self) -> bool:
        message = str(self).lower()
        return self.code == "400100" and any(reason in message for reason in
                                             ("order does not exist", "order not exist", "order not found"))


def rate_limit_delay(headers) -> float:
    delays = [0.0]
    reset = headers.get("gw-ratelimit-reset")
    if reset and reset.isdecimal():
        delays.append(int(reset) / 1000)
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            if retry_after.isdecimal():
                delays.append(int(retry_after))
            else:
                delays.append(parsedate_to_datetime(retry_after).timestamp() - time.time())
        except (ValueError, OverflowError):
            pass
    return max(delays)


class KuCoinREST:
    BASE_URL = "https://api.kucoin.com"
    ORDERS = "/api/v1/hf/orders"

    def __init__(self, session: requests.Session, *, live: bool = False):
        self.session = session
        self.live = live
        self.rate_limited_until = 0.0
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
                 private: bool = False):
        remaining = self.rate_limited_until - time.monotonic()
        if remaining > 0:
            raise KuCoinError("Waiting for KuCoin's rate limit reset.", retryable=True,
                              retry_after=remaining, not_sent=True)
        endpoint = f"{path}?{urlencode(params)}" if params else path
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        headers = {"Content-Type": "application/json"}
        if private:
            if not self.live:
                raise ValueError("Private KuCoin requests require live mode.")
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
        except requests.exceptions.SSLError as exc:
            raise KuCoinError(f"{method} {path}: TLS verification failed.") from exc
        except requests.ConnectTimeout as exc:
            raise KuCoinError(f"{method} {path}: ConnectTimeout", retryable=True, not_sent=True) from exc
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            raise KuCoinError(f"{method} {path}: {type(exc).__name__}", retryable=True) from exc

        try:
            data = response.json()
        except ValueError:
            data = None
        status = response.status_code
        code = str(data.get("code", "")) if isinstance(data, dict) else ""
        limited = status == 429 or code in {"429", "429000"}
        auth_denied = status in (401, 403) or code in {
            "400001", "400002", "400003", "400004", "400005", "400006", "400007",
        }
        delay = rate_limit_delay(response.headers) if limited or status == 503 else 0
        if delay:
            self.rate_limited_until = time.monotonic() + delay
        if response.ok and code == "200000" and "data" in data:
            return data["data"]
        message = data.get("msg", "Invalid response") if isinstance(data, dict) else "Invalid response"
        duplicate = method == "POST" and (
            code == "102426" or (code == "400100" and "duplicate" in str(message).lower())
        )
        retryable = not auth_denied and (
            limited or status >= 500 or status == 408 or duplicate
            or code in {"500000", "230005"} or (response.ok and code in {"", "200000"})
        )
        rejected_by_gateway = auth_denied or (limited and status < 500)
        raise KuCoinError(f"{method} {path}: HTTP {status}, {code} {message}", code, retryable,
                          retry_after=delay, not_sent=rejected_by_gateway)

    def klines(self, symbol: str, interval: str, start: int, end: int) -> list:
        """Fetch up to 1,500 candles for a Unix-second time window."""
        return self._request("GET", "/api/v1/market/candles", params={
            "symbol": symbol, "type": interval, "startAt": start, "endAt": end,
        })

    def symbol_rules(self, symbol: str) -> dict:
        return self._request("GET", f"/api/v2/symbols/{symbol}")

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
        orders = []
        page = 1
        while True:
            data = self._request("GET", f"{self.ORDERS}/active/page", private=True,
                                 params={"symbol": symbol, "pageNum": page, "pageSize": 20})
            if (not isinstance(data, dict) or not isinstance(data.get("items"), list)
                    or data.get("currentPage") != page):
                raise KuCoinError("Invalid response for open Spot orders.")
            orders.extend(data["items"])
            if page >= data["totalPage"]:
                return orders
            page += 1

    def place_order(self, symbol: str, side: str, price: Decimal, size: Decimal,
                    client_oid: str) -> str:
        data = self._request("POST", self.ORDERS, private=True, payload={
            "symbol": symbol, "side": side, "type": "limit", "timeInForce": "GTC",
            "postOnly": True, "price": format(price, "f"), "size": format(size, "f"),
            "clientOid": client_oid,
        })
        if (not isinstance(data, dict) or not data.get("orderId")
                or data.get("clientOid", client_oid) != client_oid):
            raise KuCoinError("KuCoin did not return an order ID.", retryable=True)
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
