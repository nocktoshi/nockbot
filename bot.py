#!/usr/bin/env python3
"""Nockbot - Telegram Bot."""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated, 
    InlineQueryResultArticle, InputTextMessageContent, BotCommand, LabeledPrice,
    BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    InlineQueryHandler,
    PreCheckoutQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
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
    SUBSCRIPTION_PRICE_STARS,
    SUBSCRIPTION_DURATION_DAYS,
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

# Lifetime subscriber expiry value (0 = never expires)
LIFETIME_EXPIRY = 0

# Subscriber types
TYPE_USER = "user"
TYPE_GROUP = "group"

def load_subscribers() -> dict[int, dict]:
    """Load all subscribers from disk.
    
    File format: {"subscribers": {"id": {"type", "expiry", "floor", "ceiling"}, ...}}
    """
    if SUBSCRIBERS_FILE.exists():
        try:
            with open(SUBSCRIBERS_FILE, "r") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.get("subscribers", {}).items()}
        except (json.JSONDecodeError, IOError, TypeError, ValueError) as e:
            logger.error(f"Failed to load subscribers: {e}")
    return {}


def save_subscribers() -> None:
    """Save subscribers to disk."""
    try:
        with open(SUBSCRIBERS_FILE, "w") as f:
            json.dump({"subscribers": subscribers}, f)
    except IOError as e:
        logger.error(f"Failed to save subscribers: {e}")


def is_subscription_active(user_id: int) -> bool:
    """Check if a user has an active subscription.
    
    expiry = 0 means lifetime subscription (never expires).
    """
    import time
    sub = subscribers.get(user_id)
    if sub is None:
        return False
    expiry = sub.get("expiry", 0) if isinstance(sub, dict) else sub
    # Lifetime subscribers have expiry = 0
    if expiry == LIFETIME_EXPIRY:
        return True
    return expiry > int(time.time())


def get_subscription_expiry(user_id: int) -> Optional[int]:
    """Get the expiry timestamp for a user's subscription, or None if not subscribed."""
    sub = subscribers.get(user_id)
    if sub is None:
        return None
    return sub.get("expiry") if isinstance(sub, dict) else sub


def get_user_thresholds(user_id: int) -> tuple[float, float]:
    """Get the floor and ceiling thresholds for a user. Returns (floor, ceiling).
    
    Uses custom values if set, otherwise falls back to global defaults.
    """
    sub = subscribers.get(user_id, {})
    if isinstance(sub, dict):
        floor = sub.get("floor") if sub.get("floor") is not None else PROOFRATE_ALERT_FLOOR
        ceiling = sub.get("ceiling") if sub.get("ceiling") is not None else PROOFRATE_ALERT_CEILING
    else:
        floor = PROOFRATE_ALERT_FLOOR
        ceiling = PROOFRATE_ALERT_CEILING
    return (floor, ceiling)


def set_user_thresholds(user_id: int, floor: Optional[float] = None, ceiling: Optional[float] = None) -> None:
    """Set custom thresholds for a user. Pass None to reset to default."""
    if user_id not in subscribers:
        return
    
    sub = subscribers[user_id]
    if not isinstance(sub, dict):
        sub = {"expiry": sub, "floor": None, "ceiling": None}
        subscribers[user_id] = sub
    
    if floor is not None:
        sub["floor"] = floor
    if ceiling is not None:
        sub["ceiling"] = ceiling
    
    save_subscribers()


def activate_subscription(user_id: int, days: int = SUBSCRIPTION_DURATION_DAYS) -> int:
    """Activate or extend a subscription. Returns new expiry timestamp."""
    import time
    
    sub = subscribers.get(user_id, {})
    if isinstance(sub, dict):
        current_expiry = sub.get("expiry", 0)
    else:
        current_expiry = sub
        sub = {"expiry": 0, "floor": None, "ceiling": None}
    
    now = int(time.time())
    
    # If current subscription is still active, extend from expiry; otherwise start from now
    base_time = max(current_expiry, now)
    new_expiry = base_time + (days * 24 * 60 * 60)
    
    sub["expiry"] = new_expiry
    subscribers[user_id] = sub
    save_subscribers()
    return new_expiry


# Global state
last_metrics: Optional[MiningMetrics] = None
floor_alert_triggered = False
ceiling_alert_triggered = False
user_alert_state: dict[int, dict] = {}  # Per-user alert state: {user_id: {"floor_triggered": bool, "ceiling_triggered": bool}}
subscribers = load_subscribers()


def get_group_chats() -> set[int]:
    """Get all group chat IDs from subscribers."""
    return {
        sub_id for sub_id, sub in subscribers.items()
        if sub.get("type") == TYPE_GROUP
    }


def get_user_subscribers() -> dict[int, dict]:
    """Get all user subscribers (not groups) from subscribers."""
    return {
        sub_id: sub for sub_id, sub in subscribers.items()
        if sub.get("type") == TYPE_USER
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    keyboard = [
        [InlineKeyboardButton("📊 Get Proofrate", callback_data="proofrate")],
        [
            InlineKeyboardButton(f"⭐ Subscribe ({SUBSCRIPTION_PRICE_STARS} Stars)", callback_data="subscribe"),
            InlineKeyboardButton("💎 Lifetime (1000 ℕOCK)", url="https://t.me/nocktoshi"),
        ],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⛏️ <b>Nockbot</b>\n\n"
        "I track the proofrate and mining metrics for the Nockchain network.\n\n"
        "<b>📊 Free Commands:</b>\n"
        "• /proofrate - Get current mining metrics\n"
        "• /tip - Get latest block info\n"
        "• /volume - Get 24h transaction volume\n\n"
        "<b>⭐ Premium Alerts:</b>\n"
        "• /subscribe - Get alerts when proofrate changes\n"
        "• /subscription - Check status &amp; set thresholds\n"
        "• /setalerts - Configure floor/ceiling\n\n"
        "<b>💰 Pricing:</b>\n"
        f"• ⭐ {SUBSCRIPTION_PRICE_STARS} Stars = {SUBSCRIPTION_DURATION_DAYS} days\n"
        "• 💎 1000 ℕOCK = LIFETIME (DM @nocktoshi)\n",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(
        "⛏️ <b>Nockbot - Help</b>\n\n"
        "<b>📊 Free Commands:</b>\n\n"
        "<b>/proofrate</b>\n"
        "Get current network mining metrics including:\n"
        "• Current difficulty\n"
        "• Network proofrate (hashrate)\n"
        "• Average block time\n"
        "• Epoch progress\n\n"
        "<b>/tip</b>\n"
        "Get the latest block info:\n"
        "• Block height and epoch\n"
        "• Timestamp and age\n"
        "• Block hash\n\n"
        "<b>/volume</b>\n"
        "Get 24-hour transaction volume:\n"
        "• Total NOCK transferred\n"
        "• Transaction count\n"
        "• Block count\n\n"
        "<b>/status</b>\n"
        "Check bot status and subscriber count\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>⭐ Premium Alerts:</b>\n\n"
        "<b>/subscribe</b>\n"
        "Subscribe for automatic alerts (sent to your DMs)\n\n"
        "<b>/subscription</b>\n"
        "Check your subscription status and thresholds\n\n"
        "<b>/setalerts</b> &lt;floor&gt; &lt;ceiling&gt;\n"
        "Set custom alert thresholds (e.g., /setalerts 0.5 3.0)\n\n"
        "<b>/resetalerts</b>\n"
        "Reset thresholds to defaults\n\n"
        "<b>/unsubscribe</b>\n"
        "Cancel your subscription\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>💰 Pricing:</b>\n"
        f"• ⭐ {SUBSCRIPTION_PRICE_STARS} Stars = {SUBSCRIPTION_DURATION_DAYS} days\n"
        "• 💎 1000 ℕOCK = LIFETIME (DM @nocktoshi)\n",
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
        previous_proofrate = last_metrics.proofrate_value if last_metrics else None
        last_metrics = metrics
        await update.message.reply_text(
            metrics.format_message(previous_proofrate),
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
    """Handle /subscribe command - send payment invoice or show status if already subscribed."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Check if user already has active subscription
    if is_subscription_active(user_id):
        expiry = get_subscription_expiry(user_id)
        
        if expiry == LIFETIME_EXPIRY:
            await update.message.reply_text(
                "✅ <b>You Have a Lifetime Subscription!</b>\n\n"
                "Your subscription never expires.\n\n"
                "You'll receive alerts when:\n"
                f"• Proofrate drops below {PROOFRATE_ALERT_FLOOR} MP/s\n"
                f"• Proofrate rises above {PROOFRATE_ALERT_CEILING} MP/s\n\n"
                "Use /subscription to see your alert thresholds.\n"
                "Use /unsubscribe to cancel your subscription.",
                parse_mode=ParseMode.HTML,
            )
        else:
            from datetime import datetime, timezone
            expiry_dt = datetime.fromtimestamp(expiry, tz=timezone.utc)
            days_left = (expiry - int(datetime.now(timezone.utc).timestamp())) // (24 * 60 * 60)
            
            await update.message.reply_text(
                "✅ <b>You're Already Subscribed!</b>\n\n"
                f"Your subscription is active until:\n"
                f"<code>{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}</code>\n"
                f"({days_left} days remaining)\n\n"
                "You'll receive alerts when:\n"
                f"• Proofrate drops below {PROOFRATE_ALERT_FLOOR} MP/s\n"
                f"• Proofrate rises above {PROOFRATE_ALERT_CEILING} MP/s\n\n"
                "Use /unsubscribe to cancel your subscription.",
                parse_mode=ParseMode.HTML,
            )
        return
    
    # Send invoice directly
    await context.bot.send_invoice(
        chat_id=chat_id,
        title="Nockbot Pro Subscription",
        description=(
            f"📊 {SUBSCRIPTION_DURATION_DAYS}-day subscription\n\n"
            f"✓ 24/7 proofrate monitoring\n"
            f"✓ Custom alert thresholds\n"
            f"✓ Alerts below {PROOFRATE_ALERT_FLOOR} MP/s or above {PROOFRATE_ALERT_CEILING} MP/s\n"
            f"✓ Direct DM notifications\n\n"
            f"💎 Want LIFETIME? Pay 1000 ℕOCK - DM @nocktoshi"
        ),
        payload=f"subscription_{user_id}_{SUBSCRIPTION_DURATION_DAYS}",
        provider_token="",  # Empty for Telegram Stars
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(f"{SUBSCRIPTION_DURATION_DAYS}-day alerts", SUBSCRIPTION_PRICE_STARS)],
    )

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /unsubscribe command - unsubscribes the user."""
    user_id = update.effective_user.id
    
    was_subscribed = user_id in subscribers
    was_lifetime = False
    if was_subscribed:
        was_lifetime = subscribers[user_id].get("expiry") == LIFETIME_EXPIRY
    
    # Remove from subscribers
    if user_id in subscribers:
        del subscribers[user_id]
        save_subscribers()
    
    # Clear alert state so re-subscribing starts fresh
    if user_id in user_alert_state:
        del user_alert_state[user_id]
    
    if was_lifetime:
        await update.message.reply_text(
            "🔕 <b>Lifetime Subscription Cancelled</b>\n\n"
            "Your lifetime subscription has been cancelled.\n"
            "You will no longer receive automatic notifications.\n\n"
            "Use /subscribe to purchase a new subscription.",
            parse_mode=ParseMode.HTML,
        )
    elif was_subscribed:
        await update.message.reply_text(
            "🔕 <b>Unsubscribed from Alerts</b>\n\n"
            "You will no longer receive automatic notifications.\n"
            "Use /subscribe to re-enable alerts.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "ℹ️ You weren't subscribed to alerts.\n"
            "Use /subscribe to enable notifications.",
            parse_mode=ParseMode.HTML,
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    global last_metrics
    
    # Count active subscribers by type
    import time
    now = int(time.time())
    user_lifetime = 0
    user_timed = 0
    group_count = 0
    
    for sub in subscribers.values():
        sub_type = sub.get("type", TYPE_USER)
        expiry = sub.get("expiry", 0) if isinstance(sub, dict) else sub
        
        if sub_type == TYPE_GROUP:
            group_count += 1
        elif expiry == LIFETIME_EXPIRY:
            user_lifetime += 1
        elif expiry > now:
            user_timed += 1
    
    total_users = user_lifetime + user_timed
    
    status_text = "📡 <b>Bot Status</b>\n\n"
    status_text += f"• Monitoring: <code>Active</code>\n"
    status_text += f"• Check Interval: <code>{MONITOR_INTERVAL_MINUTES} min</code>\n"
    status_text += f"• Default Floor: <code>{PROOFRATE_ALERT_FLOOR} MP/s</code>\n"
    status_text += f"• Default Ceiling: <code>{PROOFRATE_ALERT_CEILING} MP/s</code>\n"
    status_text += f"• Subscribers: <code>{total_users}</code> ({user_lifetime} lifetime, {user_timed} timed)\n"
    status_text += f"• Group Chats: <code>{group_count}</code>\n\n"
    
    if last_metrics:
        status_text += f"<b>Last Known Metrics:</b>\n"
        status_text += f"• Proofrate: <code>{last_metrics.proofrate}</code>\n"
        status_text += f"• Block: <code>{last_metrics.latest_block}</code>\n"
    else:
        status_text += "<i>No metrics cached yet. Use /proofrate to fetch.</i>"
    
    await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)


async def subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /subscription command - show subscription status."""
    user_id = update.effective_user.id
    
    if is_subscription_active(user_id):
        from datetime import datetime, timezone
        import time
        expiry = get_subscription_expiry(user_id)
        
        # Get user's custom thresholds
        floor, ceiling = get_user_thresholds(user_id)
        sub = subscribers.get(user_id, {})
        custom_floor = sub.get("floor") if isinstance(sub, dict) else None
        custom_ceiling = sub.get("ceiling") if isinstance(sub, dict) else None
        
        floor_str = f"<code>{floor} MP/s</code>" + (" (custom)" if custom_floor else " (default)")
        ceiling_str = f"<code>{ceiling} MP/s</code>" + (" (custom)" if custom_ceiling else " (default)")
        
        # Check if lifetime subscription
        if expiry == LIFETIME_EXPIRY:
            await update.message.reply_text(
                "✅ <b>Lifetime Subscription Active</b>\n\n"
                f"<b>Status:</b> Active (Lifetime)\n"
                f"<b>Expires:</b> <code>Never</code>\n\n"
                "<b>Alert Thresholds:</b>\n"
                f"• Floor: {floor_str}\n"
                f"• Ceiling: {ceiling_str}\n\n"
                "<b>Commands:</b>\n"
                "• /setalerts &lt;floor&gt; &lt;ceiling&gt; - Set custom thresholds\n"
                "• /resetalerts - Reset to defaults\n"
                "• /unsubscribe - Cancel subscription",
                parse_mode=ParseMode.HTML,
            )
        else:
            expiry_dt = datetime.fromtimestamp(expiry, tz=timezone.utc)
            days_left = (expiry - int(time.time())) // (24 * 60 * 60)
            hours_left = ((expiry - int(time.time())) % (24 * 60 * 60)) // 3600
            
            await update.message.reply_text(
                "✅ <b>Subscription Active</b>\n\n"
                f"<b>Status:</b> Active\n"
                f"<b>Expires:</b> <code>{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}</code>\n"
                f"<b>Time left:</b> {days_left} days, {hours_left} hours\n\n"
                "<b>Alert Thresholds:</b>\n"
                f"• Floor: {floor_str}\n"
                f"• Ceiling: {ceiling_str}\n\n"
                "<b>Commands:</b>\n"
                "• /setalerts &lt;floor&gt; &lt;ceiling&gt; - Set custom thresholds\n"
                "• /resetalerts - Reset to defaults\n"
                "• /unsubscribe - Cancel subscription",
                parse_mode=ParseMode.HTML,
            )
    else:
        # Check if they have an expired subscription
        expiry = get_subscription_expiry(user_id)
        if expiry:
            from datetime import datetime, timezone
            expiry_dt = datetime.fromtimestamp(expiry, tz=timezone.utc)
            await update.message.reply_text(
                "❌ <b>Subscription Expired</b>\n\n"
                f"Your subscription expired on:\n"
                f"<code>{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}</code>\n\n"
                "Use /subscribe to renew.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "ℹ️ <b>No Subscription</b>\n\n"
                "You don't have an active subscription.\n\n"
                "Use /subscribe to get proofrate alerts!",
                parse_mode=ParseMode.HTML,
            )


async def setalerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setalerts command - set custom alert thresholds."""
    user_id = update.effective_user.id
    
    # Check if user has active subscription
    if not is_subscription_active(user_id):
        await update.message.reply_text(
            "❌ <b>Subscription Required</b>\n\n"
            "Custom alert thresholds are a premium feature.\n"
            "Use /subscribe to get started!",
            parse_mode=ParseMode.HTML,
        )
        return
    
    # Parse arguments
    args = context.args
    if not args or len(args) != 2:
        floor, ceiling = get_user_thresholds(user_id)
        await update.message.reply_text(
            "⚙️ <b>Set Custom Alert Thresholds</b>\n\n"
            "<b>Usage:</b> <code>/setalerts &lt;floor&gt; &lt;ceiling&gt;</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/setalerts 0.5 3.0</code>\n"
            "Alert when below 0.5 MP/s or above 3.0 MP/s\n\n"
            f"<b>Your current thresholds:</b>\n"
            f"• Floor: {floor} MP/s\n"
            f"• Ceiling: {ceiling} MP/s\n\n"
            "Use /resetalerts to restore defaults.",
            parse_mode=ParseMode.HTML,
        )
        return
    
    try:
        floor = float(args[0])
        ceiling = float(args[1])
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid values. Please use numbers.\n\n"
            "<b>Example:</b> <code>/setalerts 0.5 3.0</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    
    # Validate ranges
    if floor < 0 or ceiling < 0:
        await update.message.reply_text(
            "❌ Thresholds must be positive numbers.",
            parse_mode=ParseMode.HTML,
        )
        return
    
    if floor >= ceiling:
        await update.message.reply_text(
            "❌ Floor must be less than ceiling.\n\n"
            f"You provided: floor={floor}, ceiling={ceiling}",
            parse_mode=ParseMode.HTML,
        )
        return
    
    # Set thresholds
    set_user_thresholds(user_id, floor=floor, ceiling=ceiling)
    
    # Reset user's alert state to trigger fresh alerts
    if user_id in user_alert_state:
        del user_alert_state[user_id]
    
    await update.message.reply_text(
        "✅ <b>Thresholds Updated!</b>\n\n"
        f"<b>New settings:</b>\n"
        f"• Alert when below: <code>{floor} MP/s</code>\n"
        f"• Alert when above: <code>{ceiling} MP/s</code>\n\n"
        "You'll receive alerts based on these thresholds.",
        parse_mode=ParseMode.HTML,
    )


async def resetalerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resetalerts command - reset to default thresholds."""
    user_id = update.effective_user.id
    
    if not is_subscription_active(user_id):
        await update.message.reply_text(
            "❌ You don't have an active subscription.",
            parse_mode=ParseMode.HTML,
        )
        return
    
    # Reset thresholds to None (will use defaults)
    sub = subscribers.get(user_id, {})
    if isinstance(sub, dict):
        sub["floor"] = None
        sub["ceiling"] = None
        save_subscribers()
    
    # Reset alert state
    if user_id in user_alert_state:
        del user_alert_state[user_id]
    
    await update.message.reply_text(
        "✅ <b>Thresholds Reset!</b>\n\n"
        f"Your alerts will now use the default thresholds:\n"
        f"• Floor: <code>{PROOFRATE_ALERT_FLOOR} MP/s</code>\n"
        f"• Ceiling: <code>{PROOFRATE_ALERT_CEILING} MP/s</code>",
        parse_mode=ParseMode.HTML,
    )


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
    
    if query.data == "hashrate" or query.data == "proofrate":
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        metrics = await get_metrics()
        if metrics:
            global last_metrics
            previous_proofrate = last_metrics.proofrate_value if last_metrics else None
            last_metrics = metrics
            await query.message.reply_text(
                metrics.format_message(previous_proofrate),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await query.message.reply_text(
                "❌ Could not fetch metrics. Try again later.",
                parse_mode=ParseMode.HTML,
            )
    
    elif query.data == "subscribe":
        # Send payment invoice directly
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if is_subscription_active(user_id):
            expiry = get_subscription_expiry(user_id)
            if expiry == LIFETIME_EXPIRY:
                await query.message.reply_text(
                    "✅ You already have a lifetime subscription!",
                    parse_mode=ParseMode.HTML,
                )
            else:
                from datetime import datetime, timezone
                expiry_dt = datetime.fromtimestamp(expiry, tz=timezone.utc)
                await query.message.reply_text(
                    f"✅ You already have an active subscription until {expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}",
                    parse_mode=ParseMode.HTML,
                )
        else:
            await context.bot.send_invoice(
                chat_id=chat_id,
                title="Nockbot Pro Subscription",
                description=(
                    f"📊 {SUBSCRIPTION_DURATION_DAYS}-day subscription\n\n"
                    f"✓ 24/7 proofrate monitoring\n"
                    f"✓ Custom alert thresholds\n"
                    f"✓ Alerts below {PROOFRATE_ALERT_FLOOR} MP/s or above {PROOFRATE_ALERT_CEILING} MP/s\n"
                    f"✓ Direct DM notifications\n\n"
                    f"💎 Want LIFETIME? Pay 1000 ℕOCK - DM @nocktoshi"
                ),
                payload=f"subscription_{user_id}_{SUBSCRIPTION_DURATION_DAYS}",
                provider_token="",  # Empty for Telegram Stars
                currency="XTR",  # Telegram Stars
                prices=[LabeledPrice(f"{SUBSCRIPTION_DURATION_DAYS}-day alerts", SUBSCRIPTION_PRICE_STARS)],
            )
    
    elif query.data == "help":
        await query.message.reply_text(
            "Use /help to see all available commands.",
            parse_mode=ParseMode.HTML,
        )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pre-checkout queries - approve or decline the payment."""
    query = update.pre_checkout_query
    
    # Verify the payload
    if query.invoice_payload.startswith("subscription_"):
        # Payment is valid, approve it
        await query.answer(ok=True)
    else:
        # Invalid payload, decline
        await query.answer(ok=False, error_message="Invalid subscription request")


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle successful payments - activate the subscription."""
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    
    # Parse payload to get duration (if custom durations are added later)
    payload_parts = payment.invoice_payload.split("_")
    days = SUBSCRIPTION_DURATION_DAYS
    if len(payload_parts) >= 3:
        try:
            days = int(payload_parts[2])
        except ValueError:
            pass
    
    # Activate subscription
    new_expiry = activate_subscription(user_id, days)
    
    from datetime import datetime, timezone
    expiry_dt = datetime.fromtimestamp(new_expiry, tz=timezone.utc)
    
    logger.info(f"New subscription: user {user_id}, expires {expiry_dt}, paid {payment.total_amount} Stars")
    
    await update.message.reply_text(
        "🎉 <b>Payment Successful!</b>\n\n"
        f"Thank you for subscribing to Nockbot Pro!\n\n"
        f"<b>Subscription Details:</b>\n"
        f"• Duration: {days} days\n"
        f"• Expires: <code>{expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}</code>\n"
        f"• Stars paid: ⭐ {payment.total_amount}\n\n"
        "<b>You will now receive alerts direct to your DMs when:</b>\n"
        f"• Proofrate drops below {PROOFRATE_ALERT_FLOOR} MP/s\n"
        f"• Proofrate rises above {PROOFRATE_ALERT_CEILING} MP/s\n\n"
        "Use /setalerts <floor> <ceiling> to set your own thresholds\n\n"
        "Use /subscription to check your status anytime.",
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
            previous_proofrate = last_metrics.proofrate_value if last_metrics else None
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title="📊 Mining Metrics",
                    description=f"Proofrate: {metrics.proofrate} | Difficulty: {metrics.difficulty}",
                    input_message_content=InputTextMessageContent(
                        metrics.format_message(previous_proofrate),
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
        if chat.id not in subscribers or subscribers[chat.id].get("type") != TYPE_GROUP:
            subscribers[chat.id] = {
                "type": TYPE_GROUP,
                "expiry": LIFETIME_EXPIRY,
                "floor": None,
                "ceiling": None
            }
            save_subscribers()
            logger.info(f"Bot added to group: {chat.title} ({chat.id})")
    elif new_status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        # Bot was removed from group
        if chat.id in subscribers and subscribers[chat.id].get("type") == TYPE_GROUP:
            del subscribers[chat.id]
            save_subscribers()
            logger.info(f"Bot removed from group: {chat.title} ({chat.id})")


async def send_alert(app: Application, chat_id: int, message: str) -> bool:
    """Send an alert message to a chat. Returns True if successful."""
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send alert to {chat_id}: {e}")
        return False


async def check_and_alert(app: Application) -> None:
    """Periodic task to check metrics and send alerts."""
    global last_metrics, floor_alert_triggered, ceiling_alert_triggered, user_alert_state
    
    logger.info("Checking metrics...")
    metrics = await get_metrics()
    
    if not metrics:
        logger.warning("Failed to fetch metrics")
        return
    
    last_metrics = metrics
    proofrate = metrics.proofrate_value
    logger.info(f"Current proofrate: {metrics.proofrate} ({proofrate:.3f} MP/s)")
    
    import time
    now = int(time.time())
    
    # Process each user subscriber (not groups) with their custom thresholds
    for user_id, sub in get_user_subscribers().items():
        # Skip groups - they use global thresholds and are handled separately
        if sub.get("type") == TYPE_GROUP:
            continue
        
        # Check if subscription is active (expiry=0 means lifetime)
        expiry = sub.get("expiry", 0) if isinstance(sub, dict) else sub
        if expiry != LIFETIME_EXPIRY and expiry <= now:
            continue
        
        # Get user's thresholds
        floor, ceiling = get_user_thresholds(user_id)
        
        # Get or create user's alert state
        if user_id not in user_alert_state:
            user_alert_state[user_id] = {"floor_triggered": False, "ceiling_triggered": False}
        
        state = user_alert_state[user_id]
        
        # Check floor alert
        if proofrate < floor and not state["floor_triggered"]:
            state["floor_triggered"] = True
            alert_msg = (
                f"🔴 <b>Low Proofrate Alert!</b>\n\n"
                f"Network proofrate has dropped below your threshold of {floor} MP/s\n\n"
                f"Current: <code>{metrics.proofrate}</code>\n"
                f"Difficulty: <code>{metrics.difficulty}</code>\n\n"
                f"🔗 <a href='https://nockblocks.com/metrics?tab=mining'>View Details</a>"
            )
            await send_alert(app, user_id, alert_msg)
        
        # Floor recovery
        elif proofrate >= floor and state["floor_triggered"]:
            state["floor_triggered"] = False
            recovery_msg = (
                f"✅ <b>Proofrate Recovered!</b>\n\n"
                f"Network proofrate is back above your threshold of {floor} MP/s\n\n"
                f"Current: <code>{metrics.proofrate}</code>\n"
                f"Difficulty: <code>{metrics.difficulty}</code>"
            )
            await send_alert(app, user_id, recovery_msg)
        
        # Check ceiling alert
        if proofrate > ceiling and not state["ceiling_triggered"]:
            state["ceiling_triggered"] = True
            alert_msg = (
                f"🚀 <b>High Proofrate Alert!</b>\n\n"
                f"Network proofrate has risen above your threshold of {ceiling} MP/s\n\n"
                f"Current: <code>{metrics.proofrate}</code>\n"
                f"Difficulty: <code>{metrics.difficulty}</code>\n\n"
                f"🔗 <a href='https://nockblocks.com/metrics?tab=mining'>View Details</a>"
            )
            await send_alert(app, user_id, alert_msg)
        
        # Ceiling recovery
        elif proofrate <= ceiling and state["ceiling_triggered"]:
            state["ceiling_triggered"] = False
            recovery_msg = (
                f"📉 <b>Proofrate Normalized</b>\n\n"
                f"Network proofrate is back below your threshold of {ceiling} MP/s\n\n"
                f"Current: <code>{metrics.proofrate}</code>\n"
                f"Difficulty: <code>{metrics.difficulty}</code>"
            )
            await send_alert(app, user_id, recovery_msg)
    
    # Also alert group chats and ALERT_CHAT_IDS using global thresholds
    group_recipients = set(ALERT_CHAT_IDS).union(get_group_chats())
    
    if group_recipients:
        # Floor alert for groups
        if proofrate < PROOFRATE_ALERT_FLOOR and not floor_alert_triggered:
            floor_alert_triggered = True
            alert_msg = (
                f"🔴 <b>Low Proofrate Alert!</b>\n\n"
                f"Network proofrate has dropped below {PROOFRATE_ALERT_FLOOR} MP/s\n\n"
                f"Current: <code>{metrics.proofrate}</code>\n"
                f"Difficulty: <code>{metrics.difficulty}</code>\n\n"
                f"🔗 <a href='https://nockblocks.com/metrics?tab=mining'>View Details</a>"
            )
            for chat_id in group_recipients:
                await send_alert(app, chat_id, alert_msg)
        
        # Floor recovery for groups
        elif proofrate >= PROOFRATE_ALERT_FLOOR and floor_alert_triggered:
            floor_alert_triggered = False
            recovery_msg = (
                f"✅ <b>Proofrate Recovered!</b>\n\n"
                f"Network proofrate is back above {PROOFRATE_ALERT_FLOOR} MP/s\n\n"
                f"Current: <code>{metrics.proofrate}</code>\n"
                f"Difficulty: <code>{metrics.difficulty}</code>"
            )
            for chat_id in group_recipients:
                await send_alert(app, chat_id, recovery_msg)
        
        # Ceiling alert for groups
        if proofrate > PROOFRATE_ALERT_CEILING and not ceiling_alert_triggered:
            ceiling_alert_triggered = True
            alert_msg = (
                f"🚀 <b>High Proofrate Alert!</b>\n\n"
                f"Network proofrate has risen above {PROOFRATE_ALERT_CEILING} MP/s\n\n"
                f"Current: <code>{metrics.proofrate}</code>\n"
                f"Difficulty: <code>{metrics.difficulty}</code>\n\n"
                f"🔗 <a href='https://nockblocks.com/metrics?tab=mining'>View Details</a>"
            )
            for chat_id in group_recipients:
                await send_alert(app, chat_id, alert_msg)
        
        # Ceiling recovery for groups
        elif proofrate <= PROOFRATE_ALERT_CEILING and ceiling_alert_triggered:
            ceiling_alert_triggered = False
            recovery_msg = (
                f"📉 <b>Proofrate Normalized</b>\n\n"
                f"Network proofrate is back below {PROOFRATE_ALERT_CEILING} MP/s\n\n"
                f"Current: <code>{metrics.proofrate}</code>\n"
                f"Difficulty: <code>{metrics.difficulty}</code>"
            )
            for chat_id in group_recipients:
                await send_alert(app, chat_id, recovery_msg)


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
    app.add_handler(CommandHandler("subscription", subscription))
    app.add_handler(CommandHandler("setalerts", setalerts))
    app.add_handler(CommandHandler("resetalerts", resetalerts))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("tip", tip))
    app.add_handler(CommandHandler("volume", volume))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChatMemberHandler(track_chat_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    
    # Payment handlers
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    
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
        
        # Set bot commands via API (https://core.telegram.org/bots/api#setmycommands)
        # Commands for private chats (full feature set)
        private_commands = [
            BotCommand("start", "Start the bot and see options"),
            BotCommand("proofrate", "Get current mining metrics"),
            BotCommand("tip", "Get latest block info"),
            BotCommand("volume", "Get 24h transaction volume"),
            BotCommand("subscribe", "Subscribe to proofrate alerts"),
            BotCommand("subscription", "Check your subscription status"),
            BotCommand("setalerts", "Set custom alert thresholds"),
            BotCommand("resetalerts", "Reset to default thresholds"),
            BotCommand("unsubscribe", "Stop receiving alerts"),
            BotCommand("status", "Check bot status"),
            BotCommand("help", "Show all commands"),
        ]
        
        # Commands for group chats (info only, no subscription management)
        group_commands = [
            BotCommand("proofrate", "Get current mining metrics"),
            BotCommand("tip", "Get latest block info"),
            BotCommand("volume", "Get 24h transaction volume"),
            BotCommand("status", "Check bot status"),
            BotCommand("help", "Show all commands"),
        ]
        
        # Register commands for all scopes
        await app.bot.set_my_commands(private_commands, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
        logger.info("Bot commands registered for all scopes.")
    
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
    print(f"💰 Subscription: {SUBSCRIPTION_PRICE_STARS} Stars / {SUBSCRIPTION_DURATION_DAYS} days")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
