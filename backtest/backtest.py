from decimal import Decimal, InvalidOperation, localcontext

import pandas as pd

from config_test import INITIAL_USDT, MAKER_FEE_PCT, SYMBOL

MINUTE = pd.Timedelta(minutes=1)
CANDLE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


def price_range(minimum: float | Decimal, maximum: float | Decimal, step: float | Decimal) -> list[Decimal]:
    try:
        low, high, increment = (Decimal(str(value)) for value in (minimum, maximum, step))
    except InvalidOperation as exc:
        raise ValueError("Price range values must be numbers.") from exc
    if not all(value.is_finite() for value in (low, high, increment)) or not (
            0 < low <= high and increment > 0):
        raise ValueError("Price ranges require 0 < minimum <= maximum and a positive step.")

    prices = []
    with localcontext() as context:
        # Keep the increment exact even when it has more decimal places than the endpoints.
        context.prec = max(context.prec, max(high.adjusted(), increment.adjusted())
                           - min(value.as_tuple().exponent for value in (low, high, increment)) + 2)
        price = low
        while price < high:
            prices.append(price)
            price += increment
    prices.append(high)
    return prices


def validate_candles(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        raise ValueError("No candles found in the selected range.")
    if set(CANDLE_COLUMNS) - set(data.columns):
        raise ValueError("History must contain " + ", ".join(CANDLE_COLUMNS) + ".")
    data = data[list(CANDLE_COLUMNS)].copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True).astype("datetime64[ns, UTC]")
    if data["timestamp"].isna().any() or data["timestamp"].ne(data["timestamp"].dt.floor("min")).any():
        raise ValueError("Each candle must have a timestamp on a UTC minute boundary.")

    prices = data[["open", "high", "low", "close"]].apply(pd.to_numeric)
    volume = pd.to_numeric(data["volume"])
    if (prices.isna().any().any() or not (prices.gt(0) & prices.lt(float("inf"))).all().all()
            or not (volume.ge(0) & volume.lt(float("inf"))).all()
            or prices["high"].lt(prices[["open", "close", "low"]].max(axis=1)).any()
            or prices["low"].gt(prices[["open", "close"]].min(axis=1)).any()):
        raise ValueError("History contains invalid OHLCV values.")
    data[prices.columns] = prices
    data["volume"] = volume
    return (data.drop_duplicates("timestamp", keep="last")
            .sort_values("timestamp").reset_index(drop=True))


def run_backtest(prices: list[tuple[Decimal, Decimal]], close: Decimal,
                 buy: Decimal, sell: Decimal, initial: Decimal, rate: Decimal) -> dict:
    usdt, usdc = initial, Decimal(0)
    waiting_for_sell = True
    cycles = 0
    buy_divisor = 1 + rate
    for high, low in prices:
        if waiting_for_sell and high >= sell:
            gross_usdc = usdt * sell
            usdt, usdc = Decimal(0), gross_usdc - gross_usdc * rate
            waiting_for_sell = False
        elif not waiting_for_sell and low <= buy:
            # KuCoin quotes fees for this pair in USDC. The BUY budget includes its fee.
            spend_usdc = usdc / buy_divisor
            usdt, usdc = spend_usdc / buy, Decimal(0)
            cycles += 1
            waiting_for_sell = True

    ending = usdt + usdc / close
    return {
        "buy_price": buy, "sell_price": sell, "completed_cycles": cycles,
        "ending_equity_usdt": ending, "net_profit_usdt": ending - initial,
        "net_profit_pct": (ending / initial - 1) * 100,
    }


def run_combinations(candles: pd.DataFrame, buys: list[Decimal], sells: list[Decimal]) -> list[dict]:
    pairs = [(buy, sell) for buy in buys for sell in sells if buy < sell]
    if not pairs:
        raise ValueError("The price ranges contain no combinations with BUY < SELL.")

    initial, fee_pct = (Decimal(str(value)) for value in (INITIAL_USDT, MAKER_FEE_PCT))
    if SYMBOL != "USDT-USDC" or not (initial.is_finite() and fee_pct.is_finite()
                                    and initial > 0 and 0 <= fee_pct < 100):
        raise ValueError("Expected USDT-USDC, positive capital and 0 <= fee < 100%.")
    rate = fee_pct / 100
    candles = validate_candles(candles)
    prices = [(Decimal(str(high)), Decimal(str(low))) for high, low in
              candles[["high", "low"]].itertuples(index=False, name=None)]
    close = Decimal(str(candles["close"].iloc[-1]))
    start, end = candles["timestamp"].iloc[0], candles["timestamp"].iloc[-1] + MINUTE
    results = []
    for buy, sell in pairs:
        result = run_backtest(prices, close, buy, sell, initial, rate)
        result.update(initial_usdt=initial, start_utc=start, end_utc=end)
        results.append(result)
    return sorted(results, key=lambda row: (-row["ending_equity_usdt"], row["buy_price"], row["sell_price"]))
