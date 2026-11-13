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

History CSVs are saved in `backtest/history/`. The program saves a ranked combinations CSV and an offline HTML report in `backtest/results/`. Open the HTML file printed by the program in a browser. One valid pair shows a summary. Multiple valid pairs show the winner, a profitability heatmap, and the top five pairs.

Set the `BUY_PRICE_*` and `SELL_PRICE_*` ranges in `backtest/config_test.py`. Both endpoints are included, even when a step does not land on the maximum. Set equal minimum and maximum prices on each side to test one pair. The backtest skips pairs where BUY is greater than or equal to SELL. It ranks by final USDT equity, then by ascending BUY and SELL price for ties.

Each pair starts with `INITIAL_USDT` from `src/config.py` and uses the same one-minute candles. The backtest assumes full price-touch fills with fractional quantities and deducts `MAKER_FEE_PCT` in USDC on each side. It values any remaining USDC at the final candle close.
