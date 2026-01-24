#!/usr/bin/env python3
"""Nockchain Hashrate Monitor - Telegram Bot."""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated, InlineQueryResultArticle, InputTextMessageContent, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    InlineQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode, ChatMemberStatus
from uuid import uuid4
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    TELEGRAM_BOT_TOKEN,
    ALERT_CHAT_IDS,
    PROOFRATE_ALERT_FLOOR,
    PROOFRATE_ALERT_CEILING,
    MONITOR_INTERVAL_MINUTES,
)
from scraper import get_metrics, get_tip, get_24h_volume, MiningMetrics

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Persistence files
SUBSCRIBERS_FILE = Path(__file__).parent / "subscribers.json"
GROUP_CHATS_FILE = Path(__file__).parent / "group_chats.json"


def load_json_set(filepath: Path, key: str) -> set[int]:
    """Load a set of IDs from a JSON file."""
    if filepath.exists():
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
                ids = data.get(key, [])
                if not isinstance(ids, list):
                    logger.warning(f"Invalid {key} format in {filepath.name}, expected list")
                    return set()
                return set(ids)
        except (json.JSONDecodeError, IOError, TypeError) as e:
            logger.error(f"Failed to load {filepath.name}: {e}")
    return set()


def save_json_set(filepath: Path, key: str, data: set[int]) -> None:
    """Save a set of IDs to a JSON file."""
    try:
        with open(filepath, "w") as f:
            json.dump({key: list(data)}, f)
    except IOError as e:
        logger.error(f"Failed to save {filepath.name}: {e}")


def load_subscribers() -> set[int]:
    """Load subscribers from disk."""
    return load_json_set(SUBSCRIBERS_FILE, "chat_ids")


def save_subscribers() -> None:
    """Save subscribers to disk."""
    save_json_set(SUBSCRIBERS_FILE, "chat_ids", subscribed_chats)


def load_group_chats() -> set[int]:
    """Load group chats from disk."""
    return load_json_set(GROUP_CHATS_FILE, "group_ids")


def save_group_chats() -> None:
    """Save group chats to disk."""
    save_json_set(GROUP_CHATS_FILE, "group_ids", group_chats)


# Global state
last_metrics: Optional[MiningMetrics] = None
floor_alert_triggered = False
ceiling_alert_triggered = False
subscribed_chats: set[int] = load_subscribers()
group_chats: set[int] = load_group_chats()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    keyboard = [
        [InlineKeyboardButton("📊 Get Hashrate", callback_data="hashrate")],
        [InlineKeyboardButton("🔔 Subscribe to Alerts", callback_data="subscribe")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⛏️ <b>Nockbot</b>\n\n"
        "I track the proofrate and mining metrics for the Nockchain network.\n\n"
        "<b>Commands:</b>\n"
        "• /hashrate - Get current mining metrics\n"
        "• /proofrate - Same as /hashrate\n"
        "• /tip - Get latest block info\n"
        "• /volume - Get 24h transaction volume\n"
        "• /subscribe - Get alerts when proofrate changes\n"
        "• /unsubscribe - Stop receiving alerts\n"
        "• /status - Check bot and monitoring status\n\n"
        "Data sourced from <a href='https://nockblocks.com'>NockBlocks</a>",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(
        "⛏️ <b>Nockbot - Help</b>\n\n"
        "<b>Available Commands:</b>\n\n"
        "📊 <b>/hashrate</b> or <b>/proofrate</b>\n"
        "Get current network mining metrics including:\n"
        "• Current difficulty\n"
        "• Network proofrate (hashrate)\n"
        "• Average block time\n"
        "• Epoch progress\n\n"
        "🧊 <b>/tip</b>\n"
        "Get the latest block info:\n"
        "• Block height and epoch\n"
        "• Timestamp and age\n"
        "• Block hash\n\n"
        "💰 <b>/volume</b>\n"
        "Get 24-hour transaction volume:\n"
        "• Total NOCK transferred\n"
        "• Transaction count\n"
        "• Block count\n\n"
        "🔔 <b>/subscribe</b>\n"
        "Subscribe to automatic alerts (sent to your DMs):\n"
        f"• Proofrate drops below {PROOFRATE_ALERT_FLOOR} MP/s\n"
        f"• Proofrate rises above {PROOFRATE_ALERT_CEILING} MP/s\n\n"
        "🔕 <b>/unsubscribe</b>\n"
        "Stop receiving automatic alerts\n\n"
        "📡 <b>/status</b>\n"
        "Check bot status and subscriber count\n\n"
        "🔗 Data sourced from <a href='https://nockblocks.com/metrics?tab=mining'>NockBlocks</a>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def hashrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /hashrate and /proofrate commands."""
    # Send "typing" action while fetching
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    metrics = await get_metrics()
    
    if metrics:
        global last_metrics
        last_metrics = metrics
        await update.message.reply_text(
            metrics.format_message(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            "❌ <b>Error fetching metrics</b>\n\n"
            "Could not retrieve data from NockBlocks. Please try again later.\n\n"
            "🔗 <a href='https://nockblocks.com/metrics?tab=mining'>Check NockBlocks directly</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /subscribe command - subscribes the user (not the chat)."""
    user_id = update.effective_user.id
    subscribed_chats.add(user_id)
    save_subscribers()
    
    await update.message.reply_text(
        "🔔 <b>Subscribed to Alerts!</b>\n\n"
        "You will receive notifications when:\n"
        f"• Proofrate drops below {PROOFRATE_ALERT_FLOOR} MP/s\n"
        f"• Proofrate rises above {PROOFRATE_ALERT_CEILING} MP/s\n"
        f"• Metrics are checked every {MONITOR_INTERVAL_MINUTES} minutes\n\n"
        "Alerts will be sent to your DMs.\n"
        "Use /unsubscribe to stop receiving alerts.",
        parse_mode=ParseMode.HTML,
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /unsubscribe command - unsubscribes the user."""
    user_id = update.effective_user.id
    subscribed_chats.discard(user_id)
    save_subscribers()
    
    await update.message.reply_text(
        "🔕 <b>Unsubscribed from Alerts</b>\n\n"
        "You will no longer receive automatic notifications.\n"
        "Use /subscribe to re-enable alerts.",
        parse_mode=ParseMode.HTML,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    global last_metrics
    
    status_text = "📡 <b>Bot Status</b>\n\n"
    status_text += f"• Monitoring: <code>Active</code>\n"
    status_text += f"• Check Interval: <code>{MONITOR_INTERVAL_MINUTES} min</code>\n"
    status_text += f"• Alert Floor: <code>{PROOFRATE_ALERT_FLOOR} MP/s</code>\n"
    status_text += f"• Alert Ceiling: <code>{PROOFRATE_ALERT_CEILING} MP/s</code>\n"
    status_text += f"• Subscribers: <code>{len(subscribed_chats)}</code>\n"
    status_text += f"• Group Chats: <code>{len(group_chats)}</code>\n\n"
    
    if last_metrics:
        status_text += f"<b>Last Known Metrics:</b>\n"
        status_text += f"• Proofrate: <code>{last_metrics.proofrate}</code>\n"
        status_text += f"• Block: <code>{last_metrics.latest_block}</code>\n"
    else:
        status_text += "<i>No metrics cached yet. Use /hashrate to fetch.</i>"
    
    await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)


async def tip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tip command - show latest block."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    block = await get_tip()
    
    if block:
        height = block.get("height", "N/A")
        timestamp = block.get("timestamp", 0)
        digest = block.get("digest", "N/A")
        epoch = block.get("epochCounter", "N/A")
        
        # Format timestamp
        if timestamp:
            from datetime import datetime, timezone
            import time
            
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            
            # Calculate time ago using current UTC time
            seconds_ago = int(time.time() - timestamp)
            if seconds_ago < 0:
                ago_str = "just now"
            elif seconds_ago < 60:
                ago_str = f"{seconds_ago}s ago"
            elif seconds_ago < 3600:
                ago_str = f"{seconds_ago // 60}m {seconds_ago % 60}s ago"
            else:
                ago_str = f"{seconds_ago // 3600}h {(seconds_ago % 3600) // 60}m ago"
        else:
            time_str = "N/A"
            ago_str = ""
        
        await update.message.reply_text(
            f"🧊 <b>Latest Block</b>\n\n"
            f"├ Height: <code>{height}</code>\n"
            f"├ Epoch: <code>{epoch}</code>\n"
            f"├ Time: <code>{time_str}</code>\n"
            f"├ Age: <code>{ago_str}</code>\n"
            f"└ Hash: <code>{digest[:16]}...</code>\n\n"
            f"🔗 <a href='https://nockblocks.com/block/{height}'>View on NockBlocks</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            "❌ Could not fetch latest block. Try again later.",
            parse_mode=ParseMode.HTML,
        )


async def volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /volume command - show 24h transaction volume."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    data = await get_24h_volume()
    
    if data:
        vol = data['volume_nock']
        tx_count = data['tx_count']
        block_count = data['block_count']
        
        # Format volume nicely
        if vol >= 1000:
            vol_str = f"{vol:,.0f}"
        else:
            vol_str = f"{vol:,.2f}"
        
        await update.message.reply_text(
            f"💰 <b>24h Transaction Volume</b>\n\n"
            f"├ Volume: <code>{vol_str} NOCK</code>\n"
            f"├ Transactions: <code>{tx_count}</code>\n"
            f"└ Blocks: <code>{block_count}</code>\n\n"
            f"🔗 <a href='https://nockblocks.com/metrics'>View on NockBlocks</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            "❌ Could not fetch volume data. Try again later.",
            parse_mode=ParseMode.HTML,
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "hashrate":
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        metrics = await get_metrics()
        if metrics:
            global last_metrics
            last_metrics = metrics
            await query.message.reply_text(
                metrics.format_message(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await query.message.reply_text(
                "❌ Could not fetch metrics. Try again later.",
                parse_mode=ParseMode.HTML,
            )
    
    elif query.data == "subscribe":
        subscribed_chats.add(update.effective_user.id)
        save_subscribers()
        await query.message.reply_text(
            f"🔔 Subscribed! You'll get alerts when proofrate drops below {PROOFRATE_ALERT_FLOOR} MP/s or rises above {PROOFRATE_ALERT_CEILING} MP/s.",
            parse_mode=ParseMode.HTML,
        )
    
    elif query.data == "help":
        await query.message.reply_text(
            "Use /help to see all available commands.",
            parse_mode=ParseMode.HTML,
        )


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline queries - allows users to query the bot from any chat."""
    query = update.inline_query.query.lower().strip()
    results = []
    
    # Always show available options
    if not query or query in "hashrate" or query in "proofrate" or query in "metrics":
        metrics = await get_metrics()
        if metrics:
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title="📊 Mining Metrics",
                    description=f"Proofrate: {metrics.proofrate} | Difficulty: {metrics.difficulty}",
                    input_message_content=InputTextMessageContent(
                        metrics.format_message(),
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    ),
                )
            )
    
    if not query or query in "tip" or query in "block" or query in "latest":
        block = await get_tip()
        if block:
            from datetime import datetime, timezone
            height = block.get("height", "N/A")
            timestamp = block.get("timestamp", 0)
            digest = block.get("digest", "N/A")
            epoch = block.get("epochCounter", "N/A")
            
            if timestamp:
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                time_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                time_str = "N/A"
            
            message = (
                f"🧊 <b>Latest Block</b>\n\n"
                f"├ Height: <code>{height}</code>\n"
                f"├ Epoch: <code>{epoch}</code>\n"
                f"├ Time: <code>{time_str}</code>\n"
                f"└ Hash: <code>{digest[:16]}...</code>\n\n"
                f"🔗 <a href='https://nockblocks.com/block/{height}'>View on NockBlocks</a>"
            )
            
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title="🧊 Latest Block",
                    description=f"Block #{height} | Epoch {epoch}",
                    input_message_content=InputTextMessageContent(
                        message,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    ),
                )
            )
    
    if not query or query in "volume" or query in "transactions" or query in "24h":
        data = await get_24h_volume()
        if data:
            vol = data['volume_nock']
            tx_count = data['tx_count']
            block_count = data['block_count']
            vol_str = f"{vol:,.0f}" if vol >= 1000 else f"{vol:,.2f}"
            
            message = (
                f"💰 <b>24h Transaction Volume</b>\n\n"
                f"├ Volume: <code>{vol_str} NOCK</code>\n"
                f"├ Transactions: <code>{tx_count}</code>\n"
                f"└ Blocks: <code>{block_count}</code>\n\n"
                f"🔗 <a href='https://nockblocks.com/metrics'>View on NockBlocks</a>"
            )
            
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title="💰 24h Volume",
                    description=f"{vol_str} NOCK | {tx_count} transactions",
                    input_message_content=InputTextMessageContent(
                        message,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    ),
                )
            )
    
    await update.inline_query.answer(results, cache_time=60)


async def track_chat_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track when bot is added or removed from group chats."""
    result = update.my_chat_member
    if not result:
        return
    
    chat = result.chat
    # Only track group chats (not private chats)
    if chat.type not in ["group", "supergroup"]:
        return
    
    new_status = result.new_chat_member.status
    
    if new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]:
        # Bot was added to group
        if chat.id not in group_chats:
            group_chats.add(chat.id)
            save_group_chats()
            logger.info(f"Bot added to group: {chat.title} ({chat.id})")
    elif new_status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        # Bot was removed from group
        if chat.id in group_chats:
            group_chats.discard(chat.id)
            save_group_chats()
            logger.info(f"Bot removed from group: {chat.title} ({chat.id})")


async def check_and_alert(app: Application) -> None:
    """Periodic task to check metrics and send alerts."""
    global last_metrics, floor_alert_triggered, ceiling_alert_triggered
    
    logger.info("Checking metrics...")
    metrics = await get_metrics()
    
    if not metrics:
        logger.warning("Failed to fetch metrics")
        return
    
    last_metrics = metrics
    logger.info(f"Current proofrate: {metrics.proofrate} ({metrics.proofrate_value:.3f} MP/s)")
    
    # Check if we need to alert - combine user subscribers, config chat IDs, and group chats
    all_recipients = subscribed_chats.union(set(ALERT_CHAT_IDS)).union(group_chats)
    
    if not all_recipients:
        return
    
    # Alert if proofrate drops below floor
    if metrics.proofrate_value < PROOFRATE_ALERT_FLOOR and not floor_alert_triggered:
        floor_alert_triggered = True
        alert_msg = (
            f"🔴 <b>Low Proofrate Alert!</b>\n\n"
            f"Network proofrate has dropped below {PROOFRATE_ALERT_FLOOR} MP/s\n\n"
            f"Current: <code>{metrics.proofrate}</code>\n"
            f"Difficulty: <code>{metrics.difficulty}</code>\n\n"
            f"🔗 <a href='https://nockblocks.com/metrics?tab=mining'>View Details</a>"
        )
        for chat_id in all_recipients:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=alert_msg,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Failed to send floor alert to {chat_id}: {e}")
    
    # Floor recovery
    elif metrics.proofrate_value >= PROOFRATE_ALERT_FLOOR and floor_alert_triggered:
        floor_alert_triggered = False
        recovery_msg = (
            f"✅ <b>Proofrate Recovered!</b>\n\n"
            f"Network proofrate is back above {PROOFRATE_ALERT_FLOOR} MP/s\n\n"
            f"Current: <code>{metrics.proofrate}</code>\n"
            f"Difficulty: <code>{metrics.difficulty}</code>"
        )
        for chat_id in all_recipients:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=recovery_msg,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Failed to send floor recovery alert to {chat_id}: {e}")
    
    # Alert if proofrate rises above ceiling
    if metrics.proofrate_value > PROOFRATE_ALERT_CEILING and not ceiling_alert_triggered:
        ceiling_alert_triggered = True
        alert_msg = (
            f"🚀 <b>High Proofrate Alert!</b>\n\n"
            f"Network proofrate has risen above {PROOFRATE_ALERT_CEILING} MP/s\n\n"
            f"Current: <code>{metrics.proofrate}</code>\n"
            f"Difficulty: <code>{metrics.difficulty}</code>\n\n"
            f"🔗 <a href='https://nockblocks.com/metrics?tab=mining'>View Details</a>"
        )
        for chat_id in all_recipients:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=alert_msg,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Failed to send ceiling alert to {chat_id}: {e}")
    
    # Ceiling recovery
    elif metrics.proofrate_value <= PROOFRATE_ALERT_CEILING and ceiling_alert_triggered:
        ceiling_alert_triggered = False
        recovery_msg = (
            f"📉 <b>Proofrate Normalized</b>\n\n"
            f"Network proofrate is back below {PROOFRATE_ALERT_CEILING} MP/s\n\n"
            f"Current: <code>{metrics.proofrate}</code>\n"
            f"Difficulty: <code>{metrics.difficulty}</code>"
        )
        for chat_id in all_recipients:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=recovery_msg,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Failed to send ceiling recovery alert to {chat_id}: {e}")


def main() -> None:
    """Run the bot."""
    from config import NOCKBLOCKS_API_KEY
    
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Error: TELEGRAM_BOT_TOKEN not set!")
        print("Please set your bot token in .env file or environment variable.")
        print("\nGet a token from @BotFather on Telegram.")
        return
    
    if not NOCKBLOCKS_API_KEY:
        print("❌ Error: NOCKBLOCKS_API_KEY not set!")
        print("Please set your NockBlocks API key in .env file or environment variable.")
        return
    
    # Create application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("hashrate", hashrate))
    app.add_handler(CommandHandler("proofrate", hashrate))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("tip", tip))
    app.add_handler(CommandHandler("volume", volume))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChatMemberHandler(track_chat_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    
    # Set up periodic monitoring
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_and_alert,
        'interval',
        minutes=MONITOR_INTERVAL_MINUTES,
        args=[app],
        id='metrics_check',
        name='Check Nockchain metrics',
    )
    
    async def on_startup(app: Application) -> None:
        """Start the scheduler and set bot commands when the bot starts."""
        scheduler.start()
        logger.info(f"Scheduler started. Checking every {MONITOR_INTERVAL_MINUTES} minutes.")
        
        # Set bot commands via API
        commands = [
            BotCommand("start", "Start the bot and see options"),
            BotCommand("hashrate", "Get current mining metrics"),
            BotCommand("proofrate", "Get current mining metrics"),
            BotCommand("tip", "Get latest block info"),
            BotCommand("volume", "Get 24h transaction volume"),
            BotCommand("subscribe", "Subscribe to proofrate alerts"),
            BotCommand("unsubscribe", "Stop receiving alerts"),
            BotCommand("status", "Check bot status"),
            BotCommand("help", "Show help message"),
        ]
        await app.bot.set_my_commands(commands)
        logger.info("Bot commands registered.")
    
    async def on_shutdown(app: Application) -> None:
        """Stop the scheduler when the bot stops."""
        scheduler.shutdown()
        logger.info("Scheduler stopped.")
    
    app.post_init = on_startup
    app.post_shutdown = on_shutdown
    
    # Run the bot
    print("🚀 Starting Nockbot...")
    print(f"📊 Monitoring interval: {MONITOR_INTERVAL_MINUTES} minutes")
    print(f"⚠️  Alert floor: {PROOFRATE_ALERT_FLOOR} MP/s")
    print(f"⚠️  Alert ceiling: {PROOFRATE_ALERT_CEILING} MP/s")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
