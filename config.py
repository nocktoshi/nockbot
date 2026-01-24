"""Configuration for Nockchain Hashrate Monitor Bot."""
import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Token (required)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# NockBlocks API Key (required)
NOCKBLOCKS_API_KEY = os.getenv("NOCKBLOCKS_API_KEY", "")

# Chat IDs to send automatic alerts to (comma-separated)
ALERT_CHAT_IDS = [
    int(cid.strip()) 
    for cid in os.getenv("ALERT_CHAT_IDS", "").split(",") 
    if cid.strip()
]

# Alert thresholds - notify when proofrate goes outside this range (in MP/s)
PROOFRATE_ALERT_FLOOR = float(os.getenv("PROOFRATE_ALERT_FLOOR", "1.0"))
PROOFRATE_ALERT_CEILING = float(os.getenv("PROOFRATE_ALERT_CEILING", "2.0"))

# Monitoring interval in minutes
MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "60"))

# NockBlocks URL
NOCKBLOCKS_METRICS_URL = "https://nockblocks.com/metrics?tab=mining"
