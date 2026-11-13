import sys
from pathlib import Path

BACKTEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKTEST_DIR.parent / "src"))

from config import INITIAL_USDT, SYMBOL

BUY_PRICE_MIN = 0.9950
BUY_PRICE_MAX = 0.9995
BUY_PRICE_STEP = 0.0005

SELL_PRICE_MIN = 1.0005
SELL_PRICE_MAX = 1.0050
SELL_PRICE_STEP = 0.0005

MAKER_FEE_PCT = 0.10
HISTORY_DIR = BACKTEST_DIR / "history"
RESULTS_DIR = BACKTEST_DIR / "results"
KLINE_INTERVAL = "1min"
KLINE_PAGE_SIZE = 1500
