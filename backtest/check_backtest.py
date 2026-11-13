import csv
from decimal import Decimal as D
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import backtest
import main_test
from backtest import price_range, run_combinations
from config_test import (
    BUY_PRICE_MAX, BUY_PRICE_MIN, BUY_PRICE_STEP, INITIAL_USDT, KLINE_PAGE_SIZE, MAKER_FEE_PCT,
    SELL_PRICE_MAX, SELL_PRICE_MIN, SELL_PRICE_STEP, SYMBOL,
)
from report import save_results

INITIAL = D(str(INITIAL_USDT))
BUY, SELL = D("0.995"), D("1.005")
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
    # These are the balance expectations from the original single-pair checks.
    buys = price_range(BUY, BUY, 0.0005)
    sells = price_range(SELL, SELL, 0.0005)
    for ranges, expected_cycles, expected_equity in (
        ((BUY_TOUCH, NO_TOUCH, BUY_TOUCH), 0, INITIAL),
        ((BUY_TOUCH, SELL_TOUCH), 0, AFTER_SELL / MID),
        ((BOTH_TOUCH,), 0, AFTER_SELL / MID),
        ((BOTH_TOUCH, BOTH_TOUCH), 1, AFTER_BUY),
        ((BOTH_TOUCH,) * 4, 2, AFTER_BUY ** 2 / INITIAL),
        ((SELL_TOUCH, BUY_TOUCH, (SELL, MID, D("1.0025"))), 1,
         AFTER_BUY * SELL * (1 - RATE) / D("1.0025")),
    ):
        results = run_combinations(candles(*ranges), buys, sells)
        assert len(results) == 1
        result = results[0]
        ending = result["ending_equity_usdt"]
        assert result["completed_cycles"] == expected_cycles
        assert abs(ending - expected_equity) < D("1e-22")
        assert result["net_profit_usdt"] == ending - INITIAL
        assert result["net_profit_pct"] == (ending / INITIAL - 1) * 100


def check_ranges():
    assert price_range(0.1, 0.4, 0.1) == [D("0.1"), D("0.2"), D("0.3"), D("0.4")]
    assert price_range(0.1, 0.4, 0.2) == [D("0.1"), D("0.3"), D("0.4")]
    assert price_range(BUY, BUY, 0.0005) == [BUY]
    for settings in ((1, 1, 0), (1, 2, -1), (2, 1, 1), (1, 2, float("nan"))):
        try:
            price_range(*settings)
        except ValueError:
            continue
        raise AssertionError(f"Invalid price range accepted: {settings}")

    buys = price_range(BUY_PRICE_MIN, BUY_PRICE_MAX, BUY_PRICE_STEP)
    sells = price_range(SELL_PRICE_MIN, SELL_PRICE_MAX, SELL_PRICE_STEP)
    results = run_combinations(candles(NO_TOUCH), buys, sells)
    assert len(buys) == len(sells) == 10 and len(results) == 100
    assert [(row["buy_price"], row["sell_price"]) for row in results] == [
        (buy, sell) for buy in buys for sell in sells
    ]
    assert all(row["ending_equity_usdt"] == INITIAL for row in results)


def check_ranking():
    buys = [BUY, D("0.9995"), D("1.0005")]
    sells = [D("1.0005"), D("1.003"), SELL, D("1.006")]
    data = candles(BOTH_TOUCH, (MID, buys[1], MID), (MID, D("0.997"), D("0.997")))
    with patch.object(backtest, "run_backtest", wraps=backtest.run_backtest) as simulate:
        results = run_combinations(data, buys, sells)
    assert len(results) == simulate.call_count == 11
    first_prices, first_close, *_ = simulate.call_args_list[0].args
    for call in simulate.call_args_list:
        prices, close, buy, sell, initial, rate = call.args
        assert prices is first_prices and close == first_close == D("0.997")
        assert buy < sell and initial == INITIAL and rate == RATE

    for row in results:
        assert run_combinations(data, [row["buy_price"]], [row["sell_price"]]) == [row]
    best = results[0]
    assert best["buy_price"] == BUY and best["sell_price"] == SELL
    assert best["completed_cycles"] == 0
    assert best["ending_equity_usdt"] == AFTER_SELL / D("0.997")
    assert results[-1]["net_profit_pct"] < 0 < best["net_profit_pct"]
    assert any(row["net_profit_pct"] == 0 for row in results)
    return data, results, buys, sells


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
    def __init__(self, html):
        super().__init__()
        self.tables = []
        self.in_body = False
        self.cell = None
        assert "url(" not in html.lower() and "@import" not in html.lower()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        assert tag not in {"script", "link", "iframe"}
        assert not {"src", "href", "srcset"}.intersection(attrs), f"External resource in <{tag}>"
        if tag == "tbody":
            self.tables.append([])
            self.in_body = True
        elif self.in_body and tag == "tr":
            self.tables[-1].append([])
        elif self.in_body and tag in {"td", "th"}:
            self.cell = ["", attrs]
            self.tables[-1][-1].append(self.cell)

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self.cell = None
        elif tag == "tbody":
            self.in_body = False

    def handle_data(self, data):
        if self.cell is not None:
            self.cell[0] += data


def check_outputs(directory: Path, data, results, buys, sells):
    report, csv_file = save_results(results, buys, sells, directory)
    assert report.name.startswith(f"{SYMBOL}_20240101T0000Z_20240101T0003Z")
    with csv_file.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        assert len(rows) == len(results)
        assert set(reader.fieldnames) == {
            "buy_price", "sell_price", "ending_equity_usdt", "net_profit_usdt",
            "net_profit_pct", "completed_cycles",
        }
    for saved, calculated in zip(rows, results, strict=True):
        assert all(D(value) == calculated[key] for key, value in saved.items())

    html = report.read_text(encoding="utf-8")
    heatmap, top_five = DashboardParser(html).tables
    assert all(label in html for label in ("Best BUY price", "Best SELL price", "Best net profit",
                                           "Completed cycles", "Profitability heatmap", "Top 5 combinations"))
    results_by_pair = {(row["buy_price"], row["sell_price"]): row for row in results}
    for buy, heatmap_row in zip(buys, heatmap, strict=True):
        assert D(heatmap_row[0][0]) == buy
        for sell, (text, attrs) in zip(sells, heatmap_row[1:], strict=True):
            result = results_by_pair.get((buy, sell))
            if result is None:
                assert text == "" and attrs["class"] == "unavailable"
                continue
            percent = f"{result['net_profit_pct']:+,.2f}%"
            assert text == percent
            assert attrs.get("class") == ("best" if result is results[0] else None)
            assert attrs["title"].splitlines() == [
                f"BUY {buy:.4f}", f"SELL {sell:.4f}",
                f"Net profit {result['net_profit_usdt']:+,.2f} USDT ({percent})",
                f"Completed cycles {result['completed_cycles']:,}",
            ]
            hue = 151 if result["net_profit_pct"] > 0 else 8 if result["net_profit_pct"] < 0 else 210
            assert attrs["style"].startswith(f"background:hsl({hue} ")
    for cells, row in zip(top_five, results[:5], strict=True):
        assert [text for text, _ in cells] == [
            f"{row['buy_price']:.4f}", f"{row['sell_price']:.4f}", f"{row['net_profit_pct']:+,.2f}%",
            f"{row['net_profit_usdt']:+,.2f}", str(row["completed_cycles"]),
        ]

    small = run_combinations(data, [BUY], [SELL, sells[-1]])
    report, _ = save_results(small, [BUY], [SELL, sells[-1]], directory)
    assert len(DashboardParser(report.read_text(encoding="utf-8")).tables[1]) == 2

    single = run_combinations(data, [BUY], [SELL])
    report, csv_file = save_results(single, [BUY], [SELL], directory)
    html = report.read_text(encoding="utf-8")
    assert DashboardParser(html).tables == []
    assert all(label in html for label in ("BUY price", "SELL price", "Initial balance", "Final equity",
                                           "Net profit", "Completed cycles"))
    assert f"{single[0]['ending_equity_usdt']:,.2f} USDT" in html
    with csv_file.open(encoding="utf-8", newline="") as file:
        assert len(list(csv.DictReader(file))) == 1

    with (patch.multiple(main_test, BUY_PRICE_MIN=BUY, BUY_PRICE_MAX=BUY,
                         SELL_PRICE_MIN=SELL, SELL_PRICE_MAX=SELL),
          patch.object(main_test, "select_history", return_value=data) as select,
          patch.object(main_test, "save_results", return_value=(report, csv_file)) as save,
          patch("builtins.print") as output):
        assert main_test.main() == 0
    select.assert_called_once_with()
    save.assert_called_once_with(single, [BUY], [SELL])
    summary = output.call_args.args[0]
    assert "Combinations tested: 1\n" in summary and "BEST COMBINATION" not in summary


def main():
    check_simulation()
    check_ranges()
    data, results, buys, sells = check_ranking()
    with TemporaryDirectory() as directory:
        check_history(Path(directory))
        check_outputs(Path(directory), data, results, buys, sells)
    print("Backtest checks passed. Ranges, simulation, ranking, pagination, cache, CSV and offline HTML verified.")


if __name__ == "__main__":
    main()
