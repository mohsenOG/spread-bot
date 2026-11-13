import time
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal, DecimalException

import requests

import config
from grid_bot import BUY, INITIAL, SELL, STATE_FILE, GridBot, lock_state
from kucoin_rest import KuCoinError, KuCoinREST


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def market_report(api: KuCoinREST, bot: GridBot, *, connected: bool = True):
    print("\n========== HOURLY MARKET REPORT ==========")
    print(f"Time:                {utc_now()}")
    print(f"Symbol:              {config.SYMBOL}")
    try:
        end = int(time.time()) // 60 * 60
        start = end - 3600
        rows = api.klines(config.SYMBOL, config.MARKET_REPORT_KLINE_INTERVAL, start, end - 1)
        candles = [row for row in rows if start <= int(row[0]) < end]
        if not candles:
            raise ValueError("No completed 1-minute candles in the last hour.")
        # Classic Spot candles are start time, open, close, high, low, volume, turnover.
        close = Decimal(max(candles, key=lambda row: int(row[0]))[2])
        high = max(Decimal(row[3]) for row in candles)
        low = min(Decimal(row[4]) for row in candles)
        if not 0 < low <= close <= high:
            raise ValueError("Invalid candle prices.")
        print(f"Current price:       {close:.4f} USDC")
        print(f"Last 1h High:        {high:.4f}")
        print(f"Last 1h Low:         {low:.4f}")
        print(f"Last 1h Range:       {(high - low) / low:.2%}")
    except (KuCoinError, ValueError, TypeError, LookupError, ArithmeticError) as exc:
        print(f"Market data error:   {exc}")

    print(f"Fixed SELL price:    {SELL:.4f}")
    print(f"Fixed BUY price:     {BUY:.4f}")
    print(f"Bot state:           {bot.state['side']}")
    order = bot.state["order"]
    active = f"{bot.side.upper()} @ {bot.price:.4f}" if order else "None"
    print(f"Active order:        {active}")
    print(f"Order ID:            {(order['id'] or order['client_oid'] + ' (client ID)') if order else '-'}")
    print(f"Status:              {bot.status}")
    balance_fields = (("USDT", "available", "Available USDT"),
                      ("USDC", "available", "Available USDC"),
                      ("USDT", "balance", "Current USDT"))
    amounts = ["Unavailable" if api.live else "Unavailable in preview"] * len(balance_fields)
    if api.live and connected:
        try:
            accounts = api.spot_accounts()
            amounts = [f"{Decimal(accounts.get(currency, {}).get(field, '0')):,.4f}"
                       for currency, field, _ in balance_fields]
        except (KuCoinError, KeyError, TypeError, ArithmeticError) as exc:
            print(f"Balance check error: {exc}")
    for (_, _, label), amount in zip(balance_fields, amounts):
        print(f"{label + ':':<21}{amount}")
    if api.live:
        fee = "Unavailable"
        if connected:
            try:
                rate = Decimal(api.fee_rates(config.SYMBOL)["makerFeeRate"])
                if not rate.is_finite():
                    raise ValueError("Invalid maker fee rate.")
                fee = f"{rate:%}"
            except (KuCoinError, KeyError, TypeError, ValueError, ArithmeticError, requests.RequestException) as exc:
                fee = f"Unavailable ({exc})"
        print(f"Maker fee:           {fee}")
    print(f"Completed cycles:    {bot.state['cycles']}")
    print(f"Realized profit:     {Decimal(bot.state['realized']):+,.4f} USDT")
    for currency, fee in sorted(bot.state["realized_other_fees"].items()):
        print(f"Separate cycle fees: {Decimal(fee):f} {currency}")
    print("==========================================", flush=True)


def run(api: KuCoinREST, bot: GridBot):
    if not api.live:
        bot.status = (f"Preview. Would place a post-only SELL for up to {INITIAL:,.2f} USDT "
                      f"@ {SELL:.4f} USDC.")

    next_report = next_poll = time.monotonic()
    backoff = 0
    while True:
        now = time.monotonic()
        if now >= next_poll:
            try:
                bot.poll()
            except KuCoinError as exc:
                if not exc.retryable:
                    raise
                bot.account_verified = False
                if not backoff:
                    print(f"{utc_now()} KuCoin connectivity interrupted. {exc} Retrying.", flush=True)
                backoff = min(30, backoff * 2 or 2)
                delay = max(backoff, exc.retry_after)
            else:
                if backoff:
                    print(f"{utc_now()} KuCoin connectivity restored.", flush=True)
                backoff = 0
                delay = config.POLL_INTERVAL_SECONDS
            next_poll = time.monotonic() + delay
        now = time.monotonic()
        if now >= next_report:
            next_report = now + config.MARKET_REPORT_INTERVAL_SECONDS
            market_report(api, bot, connected=not backoff)
        time.sleep(max(0, min(next_poll, next_report) - time.monotonic()))


def main() -> int:
    try:
        if min(config.POLL_INTERVAL_SECONDS, config.MARKET_REPORT_INTERVAL_SECONDS,
               config.PARTIAL_FILL_TIMEOUT_SECONDS) <= 0:
            raise ValueError("Poll, report, and partial-fill timeout intervals must be positive.")
        if config.MARKET_REPORT_KLINE_INTERVAL != "1min":
            raise ValueError("The hourly report requires 1min candles.")
        with requests.Session() as session:
            api = KuCoinREST(session, live=config.LIVE_TRADING)
            with lock_state(STATE_FILE) if api.live else nullcontext():
                run(api, GridBot(api))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except (ValueError, RuntimeError, KeyError, TypeError, OSError, DecimalException, requests.RequestException) as exc:
        print(f"{utc_now()} Bot stopped: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
