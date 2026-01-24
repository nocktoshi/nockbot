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

# Alert threshold - notify when proofrate drops below this (in MP/s)
PROOFRATE_ALERT_THRESHOLD = float(os.getenv("PROOFRATE_ALERT_THRESHOLD", "1.0"))

# Monitoring interval in minutes
MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "5"))

# NockBlocks URL
NOCKBLOCKS_METRICS_URL = "https://nockblocks.com/metrics?tab=mining"
