#!/usr/bin/env python3
"""Nockchain Hashrate Monitor - Telegram Bot."""
import asyncio
import logging
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    TELEGRAM_BOT_TOKEN,
    ALERT_CHAT_IDS,
    PROOFRATE_ALERT_THRESHOLD,
    MONITOR_INTERVAL_MINUTES,
)
from scraper import get_metrics, MiningMetrics

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Global state
last_metrics: Optional[MiningMetrics] = None
alert_triggered = False
subscribed_chats: set[int] = set()


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
        "🔔 <b>/subscribe</b>\n"
        "Subscribe to automatic alerts when:\n"
        f"• Proofrate drops below {PROOFRATE_ALERT_THRESHOLD} MP/s\n"
        "• Significant network changes occur\n\n"
        "🔕 <b>/unsubscribe</b>\n"
        "Stop receiving automatic alerts\n\n"
        "📡 <b>/status</b>\n"
        "Check bot status and last update time\n\n"
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
    """Handle /subscribe command."""
    chat_id = update.effective_chat.id
    subscribed_chats.add(chat_id)
    
    await update.message.reply_text(
        "🔔 <b>Subscribed to Alerts!</b>\n\n"
        "You will receive notifications when:\n"
        f"• Proofrate drops below {PROOFRATE_ALERT_THRESHOLD} MP/s\n"
        "• Proofrate recovers above threshold\n"
        f"• Metrics are checked every {MONITOR_INTERVAL_MINUTES} minutes\n\n"
        "Use /unsubscribe to stop receiving alerts.",
        parse_mode=ParseMode.HTML,
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /unsubscribe command."""
    chat_id = update.effective_chat.id
    subscribed_chats.discard(chat_id)
    
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
    status_text += f"• Alert Threshold: <code>{PROOFRATE_ALERT_THRESHOLD} MP/s</code>\n"
    status_text += f"• Subscribers: <code>{len(subscribed_chats)}</code>\n\n"
    
    if last_metrics:
        status_text += f"<b>Last Known Metrics:</b>\n"
        status_text += f"• Proofrate: <code>{last_metrics.proofrate}</code>\n"
        status_text += f"• Block: <code>{last_metrics.latest_block}</code>\n"
    else:
        status_text += "<i>No metrics cached yet. Use /hashrate to fetch.</i>"
    
    await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)


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
        subscribed_chats.add(update.effective_chat.id)
        await query.message.reply_text(
            f"🔔 Subscribed! You'll get alerts when proofrate drops below {PROOFRATE_ALERT_THRESHOLD} MP/s.",
            parse_mode=ParseMode.HTML,
        )
    
    elif query.data == "help":
        await query.message.reply_text(
            "Use /help to see all available commands.",
            parse_mode=ParseMode.HTML,
        )


async def check_and_alert(app: Application) -> None:
    """Periodic task to check metrics and send alerts."""
    global last_metrics, alert_triggered
    
    logger.info("Checking metrics...")
    metrics = await get_metrics()
    
    if not metrics:
        logger.warning("Failed to fetch metrics")
        return
    
    last_metrics = metrics
    logger.info(f"Current proofrate: {metrics.proofrate} ({metrics.proofrate_value:.3f} MP/s)")
    
    # Check if we need to alert
    all_subscribers = subscribed_chats.union(set(ALERT_CHAT_IDS))
    
    if not all_subscribers:
        return
    
    # Alert if proofrate drops below threshold
    if metrics.proofrate_value < PROOFRATE_ALERT_THRESHOLD and not alert_triggered:
        alert_triggered = True
        alert_msg = (
            f"🚨 <b>Proofrate Alert!</b>\n\n"
            f"Network proofrate has dropped below {PROOFRATE_ALERT_THRESHOLD} MP/s\n\n"
            f"Current: <code>{metrics.proofrate}</code>\n"
            f"Difficulty: <code>{metrics.difficulty}</code>\n\n"
            f"🔗 <a href='https://nockblocks.com/metrics?tab=mining'>View Details</a>"
        )
        for chat_id in all_subscribers:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=alert_msg,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Failed to send alert to {chat_id}: {e}")
    
    # Alert recovery
    elif metrics.proofrate_value >= PROOFRATE_ALERT_THRESHOLD and alert_triggered:
        alert_triggered = False
        recovery_msg = (
            f"✅ <b>Proofrate Recovered!</b>\n\n"
            f"Network proofrate is back above {PROOFRATE_ALERT_THRESHOLD} MP/s\n\n"
            f"Current: <code>{metrics.proofrate}</code>\n"
            f"Difficulty: <code>{metrics.difficulty}</code>"
        )
        for chat_id in all_subscribers:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=recovery_msg,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Failed to send recovery alert to {chat_id}: {e}")


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
    app.add_handler(CallbackQueryHandler(button_callback))
    
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
        """Start the scheduler when the bot starts."""
        scheduler.start()
        logger.info(f"Scheduler started. Checking every {MONITOR_INTERVAL_MINUTES} minutes.")
    
    async def on_shutdown(app: Application) -> None:
        """Stop the scheduler when the bot stops."""
        scheduler.shutdown()
        logger.info("Scheduler stopped.")
    
    app.post_init = on_startup
    app.post_shutdown = on_shutdown
    
    # Run the bot
    print("🚀 Starting Nockbot...")
    print(f"📊 Monitoring interval: {MONITOR_INTERVAL_MINUTES} minutes")
    print(f"⚠️  Alert threshold: {PROOFRATE_ALERT_THRESHOLD} MP/s")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
