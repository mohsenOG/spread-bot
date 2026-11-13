import sys
from pathlib import Path

BACKTEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKTEST_DIR.parent / "src"))

from config import BUY_PRICE, INITIAL_USDT, SELL_PRICE, SYMBOL

MAKER_FEE_PCT = 0.10
HISTORY_DIR = BACKTEST_DIR / "history"
RESULTS_DIR = BACKTEST_DIR / "results"
KLINE_INTERVAL = "1min"
KLINE_PAGE_SIZE = 1500
