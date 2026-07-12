import os
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.environ["BINANCE_API_KEY"]
API_SECRET = os.environ["BINANCE_API_SECRET"]

INTERVAL_ENTRY = os.getenv("INTERVAL_ENTRY", "15m")
INTERVAL_TREND = os.getenv("INTERVAL_TREND", "4h")
SCAN_LIMIT     = int(os.getenv("SCAN_LIMIT",  "40"))

LEVERAGE         = int(os.getenv("LEVERAGE",     "10"))
MARGIN_USDT      = float(os.getenv("MARGIN_USDT", "50"))
SL_ATR_MULT      = float(os.getenv("SL_ATR_MULT", "1.5"))
TP1_RR           = float(os.getenv("TP1_RR",      "1.5"))
TP2_RR           = float(os.getenv("TP2_RR",      "3.0"))
MIN_SCORE        = int(os.getenv("MIN_SCORE",    "3"))
AUTO_TRADE       = os.getenv("AUTO_TRADE",    "false").lower() == "true"
TESTNET          = os.getenv("TESTNET",       "true").lower()  == "true"
CAPITAL_PCT      = float(os.getenv("CAPITAL_PCT",  "0.002"))    # 0.2% per trade
