# KuCoin Spot fixed grid bot

Install the dependencies from the repository root.

```bash
python -m pip install -r requirements.txt
```

## Historical backtest

```bash
python backtest/main_test.py
python backtest/check_backtest.py
```

Choose a UTC date range to download one-minute KuCoin candles, or select an existing CSV. The end date is inclusive. For the current UTC date, the download stops at the last completed minute. Downloads reuse a saved range when it covers the selection. The public API needs no credentials.

History CSVs are saved in `backtest/history/`. Open the HTML file printed by the program directly in a browser. It and the cycle, equity, and summary CSVs are saved in `backtest/results/`. Reports cover the first available candle through the last candle's close. Missing minutes stay missing. The checks use synthetic candles and temporary files.

The backtest reads the allocation, fixed prices, and maker fee from `src/config.py`. It assumes full hypothetical price-touch fills, uses fractional quantities, and deducts fees in USDC on both sides. Historical candles cannot establish execution, queue priority, or post-only acceptance. An open USDC balance is valued at the final close. Monthly returns use marked equity. Annualized return compounds the overall return over a 365-day year.
