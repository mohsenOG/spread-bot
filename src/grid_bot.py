import json
import os
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import config
from kucoin_rest import KuCoinError, KuCoinREST

WAIT_SELL = "WAIT_SELL"
WAIT_BUY = "WAIT_BUY"
ZERO = Decimal(0)
INITIAL = Decimal(str(config.INITIAL_USDT))
BUY = Decimal(str(config.BUY_PRICE))
SELL = Decimal(str(config.SELL_PRICE))
MAKER_RATE = Decimal(str(config.MAKER_FEE_PCT)) / 100
STATE_FILE = Path(__file__).resolve().parent.parent / ".grid_bot.json"


@contextmanager
def lock_state(path: Path):
    # Keep the lock file in place. The OS releases the lock when the process exits.
    with path.with_suffix(".lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Cannot lock {path.name}. Check for another live bot.") from exc
        yield


class GridBot:
    def __init__(self, api: KuCoinREST, path: Path = STATE_FILE):
        self.api = api
        self.path = path
        self.market = [config.SYMBOL, str(BUY), str(SELL)]
        if config.SYMBOL != "USDT-USDC" or not ZERO < BUY < SELL or INITIAL <= ZERO:
            raise ValueError("Set USDT-USDC, a positive allocation, and 0 < BUY_PRICE < SELL_PRICE.")

        self.state = {
            "market": self.market, "account_id": None, "side": WAIT_SELL, "order": None,
            "remaining": None, "usdt": "0", "usdc": "0", "cycles": 0,
            "initial_usdt": "0", "realized": "0", "other_fees": {},
            "realized_other_fees": {},
        }
        if api.live and path.exists():
            with path.open(encoding="utf-8") as file:
                saved = json.load(file)
            if saved["market"] != self.market or saved["side"] not in (WAIT_SELL, WAIT_BUY):
                raise ValueError(f"Saved state in {path.name} has different settings or an invalid side.")
            if self.state.keys() - saved.keys() or not saved["account_id"]:
                raise ValueError(f"Saved state in {path.name} is incomplete.")
            self.state = saved

        self.status = "Awaiting reconciliation" if self.state["order"] else "Idle"

    @property
    def side(self) -> str:
        return "sell" if self.state["side"] == WAIT_SELL else "buy"

    @property
    def price(self) -> Decimal:
        return SELL if self.side == "sell" else BUY

    def save(self, **changes):
        state = self.state | changes
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.path)
        self.state = state

    def set_status(self, message: str):
        if message != self.status:
            self.status = message
            print(message, flush=True)

    def poll(self):
        if not self.api.live:
            return

        accounts = self.api.spot_accounts()
        account_id = accounts.get("USDT", {}).get("id")
        if self.state["account_id"] and self.state["account_id"] != account_id:
            raise KuCoinError("Cannot match the saved USDT trading account.")
        if self.state["order"]:
            self.reconcile()
            return

        available_usdt = Decimal(accounts.get("USDT", {}).get("available", "0"))
        if self.state["account_id"]:
            initial = {}
        else:
            if not account_id or available_usdt <= ZERO:
                self.set_status("Waiting for available USDT in the Spot trading account.")
                return
            allocation = min(INITIAL, available_usdt)
            initial = {"account_id": account_id, "initial_usdt": str(allocation),
                       "usdt": str(allocation)}
        self.submit(accounts, initial)

    def submit(self, accounts: dict, initial: dict):
        rules = self.api.symbol_rules(config.SYMBOL)
        if (not rules or rules["enableTrading"] is not True
                or (rules["baseCurrency"], rules["quoteCurrency"]) != ("USDT", "USDC")):
            raise KuCoinError(f"Spot trading is unavailable for {config.SYMBOL}.")
        tick = Decimal(rules["priceIncrement"])
        if BUY % tick or SELL % tick:
            raise ValueError(f"Both fixed prices must be multiples of {tick}.")
        rates = self.api.fee_rates(config.SYMBOL)
        maker = max(ZERO, Decimal(rates["makerFeeRate"]))
        if maker >= 1 or SELL * (1 - maker) ** 2 <= BUY:
            raise ValueError("The fixed spread is too small for the account maker fee.")
        # KuCoin can reserve the taker fee when it accepts a limit order.
        reserve_rate = max(maker, Decimal(rates["takerFeeRate"]))
        side = self.side
        price = self.price
        currency = "USDT" if side == "sell" else "USDC"
        available = Decimal(accounts.get(currency, {}).get("available", "0"))
        allocated = Decimal(initial.get(currency.lower(), self.state[currency.lower()]))
        budget = min(available, allocated)
        fee_factor = 1 + reserve_rate if rules["feeCurrency"] == currency else Decimal(1)
        unit_cost = fee_factor if side == "sell" else price * fee_factor
        step = Decimal(rules["baseIncrement"])
        size = min(budget / unit_cost, Decimal(rules["baseMaxSize"]),
                   Decimal(rules["quoteMaxSize"]) / price)
        size = size // step * step

        if self.state["remaining"] is not None:
            remaining = Decimal(self.state["remaining"])
            if size < remaining:
                self.set_status(f"Waiting for enough available {currency} to retry {remaining:f} USDT.")
                return
            size = remaining

        minimum = max(Decimal(rules["minFunds"]), Decimal(rules["quoteMinSize"]))
        if size < Decimal(rules["baseMinSize"]) or size * price < minimum:
            self.set_status(f"{side.upper()} size is below KuCoin's minimum. Waiting for reconciliation."
                            if self.state["remaining"] else
                            f"Waiting for enough {currency} to meet KuCoin's order minimum.")
            return
        if size % step:
            self.set_status(f"Remaining size must be a multiple of {step}. Reconciliation required.")
            return
        if self.api.open_orders(config.SYMBOL):
            self.set_status(f"Waiting for existing {config.SYMBOL} Spot orders to close.")
            return

        order = {"client_oid": uuid4().hex, "id": None, "size": str(size)}
        # Persist intent first. An interrupted POST may still have created an order.
        self.save(**initial, order=order)
        try:
            order_id = self.api.place_order(config.SYMBOL, side, price, size, order["client_oid"])
        except KuCoinError as exc:
            if not exc.order_rejected:
                self.set_status(f"Submission unconfirmed. Checking client ID {order['client_oid']}.")
                raise
            self.save(order=None)
            self.set_status(f"{side.upper()} rejected. Retrying after the poll delay. {exc}")
            return

        self.save(order=order | {"id": order_id})
        self.set_status(f"{side.upper()} submitted, {size:f} USDT @ {price:.4f}, ID {order_id}.")

    def reconcile(self):
        saved = self.state["order"]
        details = self.api.order(config.SYMBOL, saved["client_oid"], saved["id"])
        if not details:
            raise KuCoinError(f"Order lookup returned no data for {saved['client_oid']}. "
                              "Saved order requires reconciliation.")
        order_id = details["id"]
        size = Decimal(saved["size"])
        dealt = Decimal(details["dealSize"])
        side = self.side
        if (not order_id or saved["id"] not in (None, order_id)
                or details["clientOid"] != saved["client_oid"] or details["symbol"] != config.SYMBOL
                or details["side"] != side or details["type"] != "limit"
                or details["postOnly"] is not True or Decimal(details["price"]) != self.price
                or Decimal(details["size"]) != size or not ZERO <= dealt <= size
                or not isinstance(details["active"], bool)):
            raise KuCoinError(f"Order {order_id} does not match the saved order.")

        if not saved["id"]:
            self.save(order=saved | {"id": order_id})
        if details["active"]:
            if dealt:
                self.set_status(f"{side.upper()} partially filled, {dealt:f} of {size:f} USDT.")
            else:
                self.set_status(f"{side.upper()} active.")
            return

        complete = dealt == size
        if not complete and not (details["cancelExist"] is True
                                 or Decimal(details["cancelledSize"]) == size - dealt):
            raise KuCoinError(f"Order {order_id} is inactive with an unconfirmed cancellation.")
        self.set_status(f"{side.upper()} {'filled' if complete else 'canceled'}, "
                        f"{dealt:f} of {size:f} USDT. Reconciling fills.")
        if not complete and not dealt:
            self.save(order=None)
            self.set_status(f"{side.upper()} canceled without fills. Retrying after the poll delay.")
            return
        self.settle(order_id, dealt, Decimal(details["dealFunds"]), size - dealt)

    def settle(self, order_id: str, dealt: Decimal, funds: Decimal, remaining: Decimal):
        fills = self.api.fills(config.SYMBOL, order_id)
        if (any(fill["orderId"] != order_id for fill in fills)
                or sum(Decimal(fill["size"]) for fill in fills) != dealt
                or sum(Decimal(fill["funds"]) for fill in fills) != funds):
            raise KuCoinError(f"Waiting for all fills for order {order_id}.")

        side = self.side
        direction = 1 if side == "buy" else -1
        balances = {"USDT": Decimal(self.state["usdt"]) + direction * dealt,
                    "USDC": Decimal(self.state["usdc"]) - direction * funds}
        other_fees = self.state["other_fees"].copy()
        for fill in fills:
            if fill["liquidity"] != "maker" or Decimal(fill.get("tax", "0")):
                raise KuCoinError(f"Order {order_id} has a taker fill or tax. Reconciliation required.")
            fee = Decimal(fill["fee"])
            if not fee:
                continue
            currency = fill["feeCurrency"]
            if not currency:
                raise KuCoinError(f"Order {order_id} has a fee without a currency.")
            if currency in balances:
                balances[currency] -= fee
            else:
                other_fees[currency] = str(Decimal(other_fees.get(currency, "0")) + fee)

        changes = {"order": None, "usdt": str(balances["USDT"]), "usdc": str(balances["USDC"]),
                   "other_fees": other_fees}
        if remaining:
            self.save(**changes, remaining=str(remaining))
            self.set_status(f"{side.upper()} cancellation reconciled. Retrying {remaining:f} USDT "
                            "at the same price after the poll delay.")
            return
        changes["remaining"] = None
        changes["side"] = WAIT_BUY if side == "sell" else WAIT_SELL
        if side == "buy":
            changes["cycles"] = self.state["cycles"] + 1
            changes["realized_other_fees"] = other_fees
            changes["realized"] = str(balances["USDT"] - Decimal(self.state["initial_usdt"]))
        self.save(**changes)
        self.set_status(f"{side.upper()} completed. State {self.state['side']}, "
                        f"cycles {self.state['cycles']}, allocated USDT {balances['USDT']:,.4f}.")
