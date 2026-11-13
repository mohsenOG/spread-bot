import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from backtest import MINUTE, run_backtest, validate_candles
from config_test import BUY_PRICE, HISTORY_DIR, KLINE_INTERVAL, KLINE_PAGE_SIZE, SELL_PRICE, SYMBOL
from kucoin_rest import KuCoinError, KuCoinREST
from report import format_holding, format_percent, save_results

HISTORY_PREFIX = f"{SYMBOL}_{KLINE_INTERVAL}_"
HISTORY_DATE_FORMAT = "%Y%m%dT%H%MZ"


def history_files(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"{HISTORY_PREFIX}*.csv"), reverse=True)


def load_history(path: Path) -> pd.DataFrame:
    return validate_candles(pd.read_csv(path, float_precision="round_trip"))


def cached_history(directory: Path, start: pd.Timestamp, end: pd.Timestamp) -> Path | None:
    for path in history_files(directory):
        try:
            first, last = pd.to_datetime(
                path.stem.removeprefix(HISTORY_PREFIX).split("_"),
                format=HISTORY_DATE_FORMAT, utc=True,
            )
        except ValueError:
            continue
        if first <= start < end <= last:
            return path
    return None


def download_history(api: KuCoinREST, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    start_second, end_second = int(start.timestamp()), int(end.timestamp())
    page_seconds = KLINE_PAGE_SIZE * 60
    rows = []
    for page_start in range(start_second, end_second, page_seconds):
        page_end = min(end_second, page_start + page_seconds)
        for attempt in range(3):
            try:
                page = api.klines(SYMBOL, KLINE_INTERVAL, page_start, page_end - 1)
                break
            except KuCoinError as exc:
                if not exc.retryable or attempt == 2:
                    raise
                time.sleep(2 ** attempt)

        if not isinstance(page, list) or any(not isinstance(row, list) or len(row) != 7 for row in page):
            raise ValueError("KuCoin returned an invalid Kline page.")
        # KuCoin can omit minutes with no trades. Preserve the gap and continue paging.
        rows.extend(row for row in page if page_start <= int(row[0]) < page_end)

    data = pd.DataFrame(rows, columns=["timestamp", "open", "close", "high", "low", "volume", "turnover"])
    data["timestamp"] = pd.to_datetime(pd.to_numeric(data["timestamp"]), unit="s", utc=True)
    return validate_candles(data)


def history_for_range(start: pd.Timestamp, end: pd.Timestamp, directory: Path = HISTORY_DIR) -> pd.DataFrame:
    cached = cached_history(directory, start, end)
    if cached:
        print(f"Using cached history {cached.name}")
        candles = load_history(cached)
        return candles[candles["timestamp"].ge(start) & candles["timestamp"].lt(end)]

    print(f"Downloading {SYMBOL} {KLINE_INTERVAL} candles for {start:%Y-%m-%d} through "
          f"{end - MINUTE:%Y-%m-%d} UTC...", flush=True)
    with requests.Session() as session:
        candles = download_history(KuCoinREST(session), start, end)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{HISTORY_PREFIX}{start:{HISTORY_DATE_FORMAT}}_{end:{HISTORY_DATE_FORMAT}}.csv"
    temporary = path.with_suffix(".tmp")
    candles.to_csv(temporary, index=False)
    temporary.replace(path)
    print(f"History saved to {path}")
    return candles


def select_history() -> pd.DataFrame:
    while True:
        choice = input("Choose: ").strip()
        if choice == "1":
            while True:
                try:
                    start = pd.Timestamp(date.fromisoformat(input("Start date (YYYY-MM-DD UTC): ").strip()), tz="UTC")
                    last_day = pd.Timestamp(date.fromisoformat(input("End date (inclusive, YYYY-MM-DD UTC): ").strip()), tz="UTC")
                    now = pd.Timestamp.now(tz="UTC").floor("min")
                    if start > last_day or last_day > now.normalize():
                        raise ValueError("Use an ordered date range up to today in UTC.")
                    end = min(last_day + pd.Timedelta(days=1), now)
                    if start >= end:
                        raise ValueError("The selected range has no completed minutes.")
                except ValueError as exc:
                    print(f"Invalid date range. {exc}")
                    continue
                return history_for_range(start, end)

        if choice == "2":
            files = history_files(HISTORY_DIR)
            if not files:
                print(f"No {SYMBOL} history CSVs are available. Choose 1 to download history.")
                continue
            for index, path in enumerate(files, start=1):
                print(f"{index}. {path.name}")
            while True:
                try:
                    index = int(input("Select history: "))
                    if not 1 <= index <= len(files):
                        raise ValueError
                except ValueError:
                    print(f"Enter a number from 1 to {len(files)}.")
                    continue
                return load_history(files[index - 1])

        print("Enter 1 or 2.")


def main() -> int:
    print(f"=== FIXED GRID BACKTEST ===\n\nSymbol: {SYMBOL}\nBuy:    {BUY_PRICE:.4f}"
          f"\nSell:   {SELL_PRICE:.4f}\n\n1. Download new history\n2. Use existing history\n")
    try:
        result = run_backtest(select_history())
        path = save_results(result)
    except (ValueError, OSError, KuCoinError) as exc:
        print(f"Backtest failed. {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nBacktest stopped.")
        return 1

    summary = result.summary
    print(f"\n=== BACKTEST RESULTS ===\n"
          f"Initial USDT:       {summary['initial_usdt']:,.2f}\n"
          f"Final Equity:       {summary['ending_equity_usdt']:,.2f} USDT\n"
          f"Completed Cycles:   {summary['completed_cycles']:,}\n"
          f"Net Profit:         {summary['net_profit_usdt']:+,.2f} USDT\n"
          f"Total Return:       {format_percent(summary['total_return_pct'])}\n"
          f"Annualized Return:  {format_percent(summary['annualized_return_pct'])}\n"
          f"Average Holding:    {format_holding(summary['average_holding_seconds'])}\n"
          f"Open Position:      {summary['ending_usdc']:,.2f} USDC"
          f"{' (waiting to buy)' if summary['open_sell_time_utc'] is not None else ''}\n")
    if summary["missing_minutes"]:
        print(f"{summary['missing_minutes']:,} minutes between the first and last candles have no data.")
    print("Fills are hypothetical price touches.")
    print(f"HTML report saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
