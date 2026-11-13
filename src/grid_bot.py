import json
import os
import time
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
            "realized_other_fees": {}, "usdc_cost": "0", "recovery": None,
        }
        if api.live and path.exists():
            with path.open(encoding="utf-8") as file:
                saved = json.load(file)
            if saved["market"] != self.market or saved["side"] not in (WAIT_SELL, WAIT_BUY):
                raise ValueError(f"Saved state in {path.name} has different settings or an invalid side.")
            if (self.state.keys() - {"usdc_cost", "recovery"} - saved.keys()
                    or not saved["account_id"]):
                raise ValueError(f"Saved state in {path.name} is incomplete.")
            # Older state recorded realized profit only at the end of a BUY.
            if "usdc_cost" not in saved and Decimal(saved["usdc"]) > ZERO:
                saved["usdc_cost"] = str(max(ZERO, Decimal(saved["initial_usdt"])
                                             + Decimal(saved["realized"]) - Decimal(saved["usdt"])))
            self.state.update(saved)

        self.account_verified = False
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

        accounts = None
        if not self.account_verified or not self.state["order"]:
            accounts = self.api.spot_accounts()
            account_id = accounts.get("USDT", {}).get("id")
            if self.state["account_id"] and self.state["account_id"] != account_id:
                raise KuCoinError("Cannot match the saved USDT trading account.")
            self.account_verified = True
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
        recovery = self.state["recovery"]
        cap = INITIAL if side == "sell" else INITIAL * SELL
        budget = min(allocated, cap)
        if recovery:
            budget = min(budget, Decimal(recovery["amount"]))
        # Reserve fees in the spend currency. Settlement uses the actual fee currency.
        fee_factor = 1 + reserve_rate
        unit_cost = fee_factor if side == "sell" else price * fee_factor
        step = Decimal(rules["baseIncrement"])
        max_size = min(Decimal(rules["baseMaxSize"]), Decimal(rules["quoteMaxSize"]) / price)
        target = min(budget / unit_cost, max_size) // step * step
        size = min(available / unit_cost, target) // step * step

        # A recovery order uses only the funds produced by the previous partial order.
        # Legacy states can already have a remainder queued under the old behavior.
        required = Decimal(self.state["remaining"]) if self.state["remaining"] else target
        minimum = max(Decimal(rules["minFunds"]), Decimal(rules["quoteMinSize"]))
        if recovery and (Decimal(recovery["size"]) < Decimal(rules["baseMinSize"])
                         or Decimal(recovery["funds"]) < minimum):
            raise KuCoinError(f"Partial fill is below KuCoin's order minimum for the {side.upper()} recovery.")
        if recovery or self.state["remaining"]:
            if required > target:
                raise KuCoinError(f"{side.upper()} remainder exceeds the allowed allocation. "
                                  "Check the saved order before restarting.")
            if required < Decimal(rules["baseMinSize"]) or required * price < minimum:
                raise KuCoinError(f"{side.upper()} recovery is below KuCoin's order minimum. "
                                  "Check the saved allocation before restarting.")
            if size < required:
                self.set_status(f"Waiting for enough available {currency} to place the {side.upper()}.")
                return
            size = required
        if size < Decimal(rules["baseMinSize"]) or size * price < minimum:
            self.set_status(f"Waiting for enough {currency} to meet KuCoin's order minimum.")
            return
        if size % step:
            raise KuCoinError(f"Order size must be a multiple of {step}. Reconciliation required.")
        if self.api.open_orders(config.SYMBOL):
            self.set_status(f"Waiting for existing {config.SYMBOL} Spot orders to close.")
            return

        order = {"client_oid": uuid4().hex, "id": None, "size": str(size)}
        # Persist intent first. An interrupted POST may still have created an order.
        self.save(**initial, order=order)
        try:
            order_id = self.api.place_order(config.SYMBOL, side, price, size, order["client_oid"])
        except KuCoinError as exc:
            if exc.not_sent or exc.order_rejected:
                self.save(order=None)
            if exc.order_rejected:
                self.set_status(f"{side.upper()} rejected. {exc}")
                return
            if exc.retryable and not exc.not_sent:
                self.set_status(f"Submission unconfirmed. Checking client ID {order['client_oid']}.")
            raise

        self.save(order=order | {"id": order_id})
        self.set_status(f"{side.upper()} submitted, {size:f} USDT @ {price:.4f}, ID {order_id}.")

    def reconcile(self):
        saved = self.state["order"]
        try:
            details = self.api.order(config.SYMBOL, saved["client_oid"], saved["id"])
        except KuCoinError as exc:
            if not exc.order_missing:
                raise
            details = None
        if details is None:
            # A missing lookup can mean an order is still being indexed or has aged out.
            orders = self.api.open_orders(config.SYMBOL)
            details = next((order for order in orders if order["clientOid"] == saved["client_oid"]), None)
            if details is None:
                self.set_status(f"Cannot verify saved order {saved['client_oid']}. "
                                "Trading is paused until it can be reconciled.")
                return
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

        if dealt < Decimal(saved.get("observed_size", "0")):
            self.set_status(f"Waiting for current execution details for order {order_id}.")
            return
        updates = {"id": order_id}
        now = time.time()
        if details["active"] and dealt > ZERO:
            updates["observed_size"] = str(dealt)
            if dealt < size and saved.get("partial_since") is None:
                updates["partial_since"] = now
        order = saved | updates
        if order != saved:
            self.save(order=order)

        funds = Decimal(details["dealFunds"])
        if details["active"]:
            partial_since = order.get("partial_since")
            requested_at = order.get("cancel_requested_at")
            if (dealt < size and partial_since is not None
                    and (requested_at is not None or now - partial_since >= config.PARTIAL_FILL_TIMEOUT_SECONDS)):
                self.cancel_partial(order, now)
            elif ZERO < dealt < size:
                confirmed = order.get("execution")
                if (not confirmed or Decimal(confirmed["size"]) != dealt
                        or Decimal(confirmed["funds"]) != funds):
                    execution = self.execution(order_id, dealt, funds)
                    if execution:
                        self.save(order=order | {"execution": execution})
                self.set_status(f"{side.upper()} partially filled, {dealt:f} of {size:f} USDT.")
            else:
                self.set_status(f"{side.upper()} active.")
            return

        complete = dealt == size
        if not complete and not (details["cancelExist"] is True
                                 or Decimal(details["cancelledSize"]) == size - dealt):
            self.set_status(f"Waiting for cancellation confirmation for order {order_id}.")
            return
        if not dealt:
            if funds:
                self.set_status(f"Waiting for consistent final fills for order {order_id}.")
                return
            self.save(order=None)
            self.set_status(f"{side.upper()} canceled without fills. Retrying at the fixed price.")
            return
        execution = self.execution(order_id, dealt, funds)
        if execution is None:
            self.set_status(f"Waiting for final fills for order {order_id}.")
            return
        self.settle(execution, partial=not complete)

    def cancel_partial(self, order: dict, now: float):
        self.set_status(f"{self.side.upper()} cancellation pending for order {order['id']}.")
        requested_at = order.get("cancel_requested_at")
        if requested_at is not None and now - requested_at < 30:
            return
        # This poll confirmed that the same exchange order is still active.
        self.save(order=order | {"cancel_requested_at": now})
        try:
            self.api.cancel_order(config.SYMBOL, order["id"])
        except KuCoinError as exc:
            if exc.retryable or (not exc.order_missing and exc.code not in {"200002", "102428"}):
                raise

    def execution(self, order_id: str, dealt: Decimal, funds: Decimal) -> dict | None:
        fills = self.api.fills(config.SYMBOL, order_id)
        fees = {"USDT": ZERO, "USDC": ZERO}
        size = value = ZERO
        for fill in fills:
            if fill["orderId"] != order_id or fill["liquidity"] != "maker" or Decimal(fill.get("tax", "0")):
                raise KuCoinError(f"Unexpected fill for order {order_id}. Reconciliation required.")
            fee = Decimal(fill["fee"])
            currency = fill["feeCurrency"]
            if fee and currency not in fees:
                raise KuCoinError(f"Order {order_id} reported a {currency!r} fee. "
                                  "Disable KCS fee payment and reconcile the order before restarting.")
            if fee:
                fees[currency] += fee
            size += Decimal(fill["size"])
            value += Decimal(fill["funds"])
        if size != dealt or value != funds:
            return None
        return {"size": str(size), "funds": str(value),
                "fees": {currency: str(fee) for currency, fee in fees.items()}}

    def settle(self, execution: dict, *, partial: bool):
        side = self.side
        direction = 1 if side == "buy" else -1
        dealt, funds = (Decimal(execution[key]) for key in ("size", "funds"))
        net_usdt = direction * dealt - Decimal(execution["fees"]["USDT"])
        net_usdc = -direction * funds - Decimal(execution["fees"]["USDC"])
        usdt = Decimal(self.state["usdt"]) + net_usdt
        usdc = Decimal(self.state["usdc"]) + net_usdc
        cost = Decimal(self.state["usdc_cost"])
        previous = self.state["recovery"]
        changes = {"order": None, "remaining": None, "usdt": str(usdt), "usdc": str(usdc),
                   "recovery": None, "side": WAIT_BUY if side == "sell" else WAIT_SELL}
        if usdt < ZERO or usdc < ZERO or direction * net_usdt <= ZERO or -direction * net_usdc <= ZERO:
            raise KuCoinError("Final fills exceed the saved allocation. Reconciliation required.")
        if partial:
            amount = net_usdc if side == "sell" else net_usdt
            changes["recovery"] = {"amount": str(amount), "size": str(dealt), "funds": str(funds)}
        if side == "sell":
            cost -= net_usdt
            if partial:
                changes["recovery"]["cost"] = str(-net_usdt)
        else:
            budget = Decimal(previous["amount"]) if previous else Decimal(self.state["usdc"])
            if previous and -net_usdc > budget:
                raise KuCoinError("Final BUY fills exceed the recovery budget. Reconciliation required.")
            basis = Decimal(previous["cost"]) if previous else cost
            spent_basis = basis * (-net_usdc / budget)
            cost -= spent_basis
            changes["realized"] = str(Decimal(self.state["realized"]) + net_usdt - spent_basis)
            changes["cycles"] = self.state["cycles"] + 1
        changes["usdc_cost"] = str(cost)
        self.save(**changes)
        outcome = "partial cancellation reconciled" if partial else "completed"
        self.set_status(f"{side.upper()} {outcome}. State {self.state['side']}, "
                        f"cycles {self.state['cycles']}, allocated USDT {usdt:,.4f}.")
