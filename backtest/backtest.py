from dataclasses import dataclass
from decimal import MAX_EMAX, MIN_EMIN, Decimal, localcontext

import pandas as pd

from config_test import BUY_PRICE, INITIAL_USDT, MAKER_FEE_PCT, SELL_PRICE, SYMBOL

WAIT_SELL = "WAIT_SELL"
WAIT_BUY = "WAIT_BUY"
MINUTE = pd.Timedelta(minutes=1)
CANDLE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
CYCLE_COLUMNS = (
    "cycle", "sell_time_utc", "sell_price", "buy_time_utc", "buy_price",
    "sold_usdt", "received_usdc", "bought_usdt", "sell_fee_usdc", "buy_fee_usdc",
    "fees_usdt", "profit_usdt", "return_pct", "holding_seconds",
)


@dataclass
class BacktestResult:
    summary: dict
    cycles: pd.DataFrame
    equity: pd.DataFrame
    months: list[dict]


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


def monthly_performance(equity: pd.DataFrame, initial: Decimal) -> list[dict]:
    times = equity["candle_time_utc"]
    periods = equity.groupby(times.dt.strftime("%Y-%m")).agg(
        closing_equity=("equity_usdt", "last"), completed_cycles=("cycle_completed", "sum"),
    )
    months = pd.date_range(times.iloc[0].normalize().replace(day=1), times.iloc[-1], freq="MS")
    opening = initial
    performance = []
    for date in months:
        month = date.strftime("%Y-%m")
        closing, cycles = periods.loc[month] if month in periods.index else (opening, 0)
        performance.append({
            "month": month, "completed_cycles": int(cycles), "opening_equity_usdt": opening,
            "closing_equity_usdt": closing, "profit_usdt": closing - opening,
            "return_pct": (closing / opening - 1) * 100,
        })
        opening = closing
    return performance


def run_backtest(candles: pd.DataFrame) -> BacktestResult:
    candles = validate_candles(candles)
    initial, buy, sell, fee_pct = map(lambda value: Decimal(str(value)),
                                      (INITIAL_USDT, BUY_PRICE, SELL_PRICE, MAKER_FEE_PCT))
    if SYMBOL != "USDT-USDC" or not (initial > 0 and 0 < buy < sell and 0 <= fee_pct < 100):
        raise ValueError("Expected USDT-USDC, positive capital, 0 < BUY < SELL and 0 <= fee < 100%.")
    rate = fee_pct / 100
    usdt, usdc = initial, Decimal(0)
    state = WAIT_SELL
    cycles, marks = [], []
    sell_time = None

    for candle in candles.itertuples(index=False):
        timestamp = candle.timestamp
        close = Decimal(str(candle.close))
        trade = ""
        if state == WAIT_SELL and Decimal(str(candle.high)) >= sell:
            sold_usdt = usdt
            gross_usdc = sold_usdt * sell
            sell_fee = gross_usdc * rate
            usdt, usdc = Decimal(0), gross_usdc - sell_fee
            sell_time, state, trade = timestamp, WAIT_BUY, "SELL"
        elif state == WAIT_BUY and Decimal(str(candle.low)) <= buy:
            received_usdc = usdc
            # KuCoin quotes fees for this pair in USDC. The BUY budget includes its fee.
            spend_usdc = received_usdc / (1 + rate)
            buy_fee = received_usdc - spend_usdc
            usdt, usdc = spend_usdc / buy, Decimal(0)
            profit = usdt - sold_usdt
            cycles.append({
                "cycle": len(cycles) + 1, "sell_time_utc": sell_time, "sell_price": sell,
                "buy_time_utc": timestamp, "buy_price": buy, "sold_usdt": sold_usdt,
                "received_usdc": received_usdc, "bought_usdt": usdt, "sell_fee_usdc": sell_fee,
                "buy_fee_usdc": buy_fee, "fees_usdt": sell_fee / sell + buy_fee / buy,
                "profit_usdt": profit, "return_pct": profit / sold_usdt * 100,
                "holding_seconds": int((timestamp - sell_time).total_seconds()),
            })
            sell_time, state, trade = None, WAIT_SELL, "BUY"

        marks.append((timestamp, close, usdt, usdc, usdt + usdc / close,
                      state, trade, int(trade == "BUY")))

    equity = pd.DataFrame(marks, columns=[
        "candle_time_utc", "close_usdc_per_usdt", "available_usdt", "available_usdc",
        "equity_usdt", "state", "trade", "cycle_completed",
    ])
    start = marks[0][0]
    end = marks[-1][0] + MINUTE
    seconds = int((end - start).total_seconds())
    ending = marks[-1][4]
    realized = (sold_usdt if sell_time is not None else usdt) - initial
    net = ending - initial
    growth = ending / initial
    with localcontext() as context:
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        annualized = (growth ** (Decimal(365 * 86400) / seconds) - 1) * 100
    holds = [cycle["holding_seconds"] for cycle in cycles]
    summary = {
        "symbol": SYMBOL, "start_utc": start, "end_utc": end, "buy_price": buy, "sell_price": sell,
        "maker_fee_pct": fee_pct, "initial_usdt": initial, "ending_equity_usdt": ending,
        "net_profit_usdt": net, "realized_profit_usdt": realized, "unrealized_profit_usdt": net - realized,
        "total_return_pct": (growth - 1) * 100, "annualized_return_pct": annualized,
        "completed_cycles": len(cycles), "average_holding_seconds": sum(holds) / len(holds) if holds else None,
        "max_holding_seconds": max(holds, default=None),
        "state": state, "ending_usdt": usdt, "ending_usdc": usdc,
        "final_close_usdc_per_usdt": close, "open_sell_time_utc": sell_time,
        "open_sell_fee_usdc": sell_fee if sell_time is not None else None,
        "open_position_value_usdt": usdc / close,
        "open_holding_seconds": int((end - sell_time).total_seconds()) if sell_time is not None else None,
        "candles": len(marks), "missing_minutes": seconds // 60 - len(marks),
    }
    return BacktestResult(summary, pd.DataFrame(cycles, columns=CYCLE_COLUMNS), equity,
                          monthly_performance(equity, initial))
