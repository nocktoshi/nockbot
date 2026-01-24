# ⛏️ Nockbot

A Telegram bot that monitors the hashrate (proofrate) of the Nockchain network using data from [NockBlocks](https://nockblocks.com).

## Features

- 📊 **Real-time Metrics** - Get current proofrate, difficulty, block time, and epoch progress
- 🔔 **Smart Alerts** - Subscribe to notifications when proofrate drops below threshold
- ⏰ **Automatic Monitoring** - Periodic checks with configurable intervals
- 📈 **Network Stats** - Track difficulty adjustments and epoch progress

## Quick Start

### 1. Create a Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token (looks like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`)

### 2. Install Dependencies

```bash
cd nockbot
pip install -r requirements.txt
```

### 3. Configure the Bot

```bash
# Copy the example config
cp env.example .env

# Edit with your credentials
nano .env  # or use your preferred editor
```

Required settings in `.env`:
- `TELEGRAM_BOT_TOKEN` - Get from [@BotFather](https://t.me/BotFather) on Telegram
- `NOCKBLOCKS_API_KEY` - Your NockBlocks API key

### 4. Run the Bot

```bash
python bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message and quick actions |
| `/hashrate` | Get current mining metrics |
| `/proofrate` | Same as /hashrate |
| `/subscribe` | Subscribe to proofrate alerts |
| `/unsubscribe` | Stop receiving alerts |
| `/status` | Check bot and monitoring status |
| `/help` | Show all available commands |

## Metrics Displayed

- **Difficulty** - Current network difficulty (e.g., `2^31.1`)
- **Proofrate** - Network hash rate (e.g., `1.57 MP/s`)
- **Avg Block Time** - Average time between blocks
- **Epoch Progress** - Progress through current difficulty epoch
- **Blocks to Adjustment** - Blocks remaining until difficulty adjustment
- **Est. Time to Adjustment** - Estimated time until next adjustment
- **Next Adjustment Ratio** - Expected difficulty change

## Alert System

The bot monitors the network proofrate and sends alerts when:
- ⚠️ Proofrate drops below the configured threshold
- ✅ Proofrate recovers above the threshold

Configure the threshold in your `.env` file using `PROOFRATE_ALERT_THRESHOLD`.

## Running as a Service

### Using systemd (Linux)

Create `/etc/systemd/system/nockbot.service`:

```ini
[Unit]
Description=nockbot Telegram Bot
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/hashbot
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable nockbot
sudo systemctl start nockbot
```

### Using Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
```

```bash
docker build -t nockbot .
docker run -d --env-file .env nockbot
```

## Data Source

All metrics are sourced from [NockBlocks](https://nockblocks.com/metrics?tab=mining) by SWPSCo.

## License

MIT
