# KuCoin Spot fixed grid bot

Install the dependencies from the repository root.

```bash
python -m pip install -r requirements.txt
```

## Live bot

Run `python src/main.py`. Preview mode is enabled by default. Before enabling live trading, disable **Pay Fees with KCS** in your KuCoin account settings. The bot accounts for USDT and USDC fees from fills and stops if it detects a fee in another currency.

The first detected partial fill starts the timeout set by `PARTIAL_FILL_TIMEOUT_SECONDS`. The timer includes time spent offline. After KuCoin confirms cancellation, the next order uses only the net proceeds of the partial fill. When that recovery order fills, normal sizing resumes from funds already allocated to the bot, subject to the configured allocation and available balances.

Keep the local `.grid_bot.json` state file across restarts. If KuCoin cannot verify a saved order, the bot continues checking without placing a replacement. Orders beyond KuCoin's history window can require manual reconciliation. A stop caused by an unexpected fee, a permission error, or an invalid order size can leave an existing order on KuCoin. Check its status before restarting.

Run `python src/check_grid_bot.py` for the live bot smoke checks with simulated REST responses.

## Historical backtest

```bash
python backtest/main_test.py
python backtest/check_backtest.py
```

Choose a UTC date range to download one-minute KuCoin candles, or select an existing CSV. The end date is inclusive. For the current UTC date, the download stops at the last completed minute. Downloads reuse a saved range when it covers the selection. The public API needs no credentials.

History CSVs are saved in `backtest/history/`. Open the HTML file printed by the program directly in a browser. It and the cycle, equity, and summary CSVs are saved in `backtest/results/`. Reports cover the first available candle through the last candle's close. Missing minutes stay missing. The checks use synthetic candles and temporary files.

The backtest reads the allocation and fixed prices from `src/config.py` and `MAKER_FEE_PCT` from `backtest/config_test.py`. It assumes full hypothetical price-touch fills, uses fractional quantities, and deducts fees in USDC on both sides. Historical candles cannot establish execution, queue priority, or post-only acceptance. An open USDC balance is valued at the final close. Monthly returns use marked equity. Annualized return compounds the overall return over a 365-day year.
