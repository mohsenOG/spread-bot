import csv
from decimal import Decimal as D
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import main_test
from backtest import WAIT_BUY, WAIT_SELL, run_backtest
from config_test import BUY_PRICE, INITIAL_USDT, KLINE_PAGE_SIZE, MAKER_FEE_PCT, SELL_PRICE, SYMBOL
from report import save_results

INITIAL = D(str(INITIAL_USDT))
BUY, SELL = D(str(BUY_PRICE)), D(str(SELL_PRICE))
RATE = D(str(MAKER_FEE_PCT)) / 100
START = pd.Timestamp("2024-01-01 00:00", tz="UTC")
MID = (BUY + SELL) / 2


def candles(*ranges: tuple, times=None) -> pd.DataFrame:
    if times is None:
        times = pd.date_range(START, periods=len(ranges), freq="min")
    return pd.DataFrame([
        {"timestamp": time, "open": MID, "high": high, "low": low, "close": close, "volume": 1}
        for time, (high, low, close) in zip(times, ranges, strict=True)
    ])


BUY_TOUCH = (MID, BUY, MID)
SELL_TOUCH = (SELL, MID, MID)
BOTH_TOUCH = (SELL, BUY, MID)
NO_TOUCH = (MID, MID, MID)
AFTER_SELL = INITIAL * SELL * (1 - RATE)
AFTER_BUY = AFTER_SELL / (1 + RATE) / BUY


def check_simulation():
    # A buy touch on the first candle leaves the starting allocation in USDT.
    result = run_backtest(candles(BUY_TOUCH, NO_TOUCH, BUY_TOUCH))
    assert list(result.equity["state"]) == [WAIT_SELL] * 3
    assert result.equity["trade"].eq("").all() and result.cycles.empty
    assert result.summary["ending_equity_usdt"] == INITIAL

    result = run_backtest(candles(BUY_TOUCH, SELL_TOUCH))
    assert list(result.equity["trade"]) == ["", "SELL"]
    assert result.summary["state"] == WAIT_BUY
    assert result.summary["ending_usdt"] == 0
    assert result.summary["ending_usdc"] == AFTER_SELL
    assert result.summary["open_sell_time_utc"] == START + pd.Timedelta(minutes=1)
    assert result.summary["completed_cycles"] == 0

    result = run_backtest(candles(BOTH_TOUCH, BOTH_TOUCH))
    assert list(result.equity["trade"]) == ["SELL", "BUY"]
    cycle = result.cycles.iloc[0]
    assert cycle["sell_time_utc"] == START and cycle["buy_time_utc"] > START
    assert cycle["sell_price"] == SELL and cycle["buy_price"] == BUY
    assert cycle["holding_seconds"] == 60
    assert cycle["sell_fee_usdc"] == INITIAL * SELL * RATE
    assert abs(cycle["buy_fee_usdc"] - cycle["bought_usdt"] * BUY * RATE) < D("1e-22")
    assert cycle["bought_usdt"] == AFTER_BUY
    assert cycle["profit_usdt"] == AFTER_BUY - INITIAL
    assert result.summary["ending_equity_usdt"] == AFTER_BUY
    growth = AFTER_BUY / INITIAL
    assert result.summary["total_return_pct"] == (growth - 1) * 100
    assert result.summary["annualized_return_pct"] == (growth ** (365 * 86400 // 120) - 1) * 100

    result = run_backtest(candles(BOTH_TOUCH, BOTH_TOUCH, BOTH_TOUCH, BOTH_TOUCH))
    assert list(result.equity["trade"]) == ["SELL", "BUY", "SELL", "BUY"]
    assert result.summary["completed_cycles"] == 2
    assert result.cycles.iloc[1]["sold_usdt"] == AFTER_BUY
    assert abs(result.summary["ending_equity_usdt"] - AFTER_BUY ** 2 / INITIAL) < D("1e-22")


def check_months_and_open_position():
    times = pd.to_datetime([
        "2024-01-31T23:57Z", "2024-01-31T23:58Z",
        "2024-01-31T23:59Z", "2024-02-01T00:00Z",
    ], utc=True)
    final_close = MID + (SELL - MID) / 2
    result = run_backtest(candles(SELL_TOUCH, BUY_TOUCH, SELL_TOUCH, (final_close, MID, final_close),
                                  times=times))
    summary = result.summary
    held = AFTER_BUY * SELL * (1 - RATE)
    marked = held / final_close
    assert summary["completed_cycles"] == 1 and summary["state"] == WAIT_BUY
    assert abs(summary["ending_usdc"] - held) < D("1e-22")
    assert abs(summary["open_position_value_usdt"] - marked) < D("1e-22")
    assert abs(summary["ending_equity_usdt"] - marked) < D("1e-22")
    assert summary["open_holding_seconds"] == 120
    assert summary["realized_profit_usdt"] == AFTER_BUY - INITIAL
    assert abs(summary["unrealized_profit_usdt"] - (marked - AFTER_BUY)) < D("1e-22")
    january, february = result.months
    assert january["month"] == "2024-01" and january["completed_cycles"] == 1
    assert february["month"] == "2024-02" and february["completed_cycles"] == 0
    assert february["profit_usdt"] == february["closing_equity_usdt"] - january["closing_equity_usdt"]
    assert february["return_pct"] == (marked / january["closing_equity_usdt"] - 1) * 100
    assert abs(sum(month["profit_usdt"] for month in result.months) - summary["net_profit_usdt"]) < D("1e-22")
    return result


def check_history(directory: Path):
    class LocalKlines:
        calls = []

        def klines(self, symbol, interval, start, end):
            assert symbol == SYMBOL and interval == "1min"
            self.calls.append((start, end))
            first = [str(start), str(MID), str(MID), str(MID), str(MID), "1", "1"]
            last = [str(end - 59), *first[1:]]
            return [last, first, first]

    api = LocalKlines()
    page_seconds = KLINE_PAGE_SIZE * 60
    end = START + pd.Timedelta(seconds=page_seconds + 60)
    data = main_test.download_history(api, START, end)
    start_second = int(START.timestamp())
    assert api.calls == [(start_second, start_second + page_seconds - 1),
                         (start_second + page_seconds, start_second + page_seconds + 59)]
    assert len(data) == 3 and data["timestamp"].is_monotonic_increasing
    assert data["timestamp"].is_unique
    fmt = main_test.HISTORY_DATE_FORMAT
    path = directory / f"{main_test.HISTORY_PREFIX}{START:{fmt}}_{end:{fmt}}.csv"
    data.to_csv(path, index=False)
    with patch.object(main_test, "download_history", side_effect=AssertionError("Cache was bypassed")):
        cached = main_test.history_for_range(START, end - pd.Timedelta(minutes=1), directory)
    pd.testing.assert_frame_equal(cached, data.iloc[:2])


class DashboardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons = 0
        self.chart = False

    def handle_starttag(self, tag, attrs):
        assert not {"src", "href"}.intersection(dict(attrs)), f"External resource in <{tag}>"
        self.buttons += tag == "button"
        self.chart |= tag == "polyline"


def check_outputs(directory: Path, result):
    report = save_results(result, directory)
    assert report.name.startswith(f"{SYMBOL}_20240131T2357Z_20240201T0001Z")
    prefix = report.stem.removesuffix("_report")
    outputs = {}
    for kind in ("cycles", "equity", "summary"):
        with (directory / f"{prefix}_{kind}.csv").open(encoding="utf-8", newline="") as file:
            outputs[kind] = list(csv.DictReader(file))
    assert len(outputs["cycles"]) == 1 and len(outputs["equity"]) == 4
    assert D(outputs["cycles"][0]["profit_usdt"]) == result.cycles.iloc[0]["profit_usdt"]
    assert D(outputs["equity"][-1]["equity_usdt"]) == result.summary["ending_equity_usdt"]
    assert outputs["summary"][0]["completed_cycles"] == "1"
    assert D(outputs["summary"][0]["ending_usdc"]) == result.summary["ending_usdc"]

    html = report.read_text(encoding="utf-8")
    parser = DashboardParser()
    parser.feed(html)
    assert parser.buttons == 9 and parser.chart
    for label in ("Hypothetical price-touch fills", "2024-02", "Unrealized P/L", "Time waiting for BUY"):
        assert label in html
    assert f"{result.summary['ending_equity_usdt']:,.2f} USDT" in html

    no_trades = run_backtest(candles(NO_TOUCH))
    empty_report = save_results(no_trades, directory)
    assert "No completed cycles." in empty_report.read_text(encoding="utf-8")
    assert "No open position." in empty_report.read_text(encoding="utf-8")
    empty_cycles = directory / empty_report.name.replace("_report.html", "_cycles.csv")
    with empty_cycles.open(encoding="utf-8", newline="") as file:
        assert next(csv.reader(file))[0] == "cycle"


def main():
    check_simulation()
    result = check_months_and_open_position()
    with TemporaryDirectory() as directory:
        check_history(Path(directory))
        check_outputs(Path(directory), result)
    print("Backtest checks passed. Simulation, pagination, cache, CSVs, and offline HTML verified.")


if __name__ == "__main__":
    main()
