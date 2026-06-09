import os
import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction
from agent import process_audio
from database import get_user, create_user, increment_usage, is_premium
import logging

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FREE_DAILY_LIMIT = 3


# ── Helpers ────────────────────────────────────────────────────────────────────

def format_result(result: dict) -> str:
    lines = []
    lines.append("🎙 *Voice Note Summary*")
    lines.append("─────────────────────")

    if result.get("summary"):
        lines.append("\n📋 *Summary*")
        lines.append(result["summary"])

    if result.get("transcript"):
        transcript = result["transcript"]
        if len(transcript) > 600:
            transcript = transcript[:600] + "..."
        lines.append("\n📝 *Transcript*")
        lines.append(f"_{transcript}_")

    lines.append("\n✅ *Action Points*")
    if result.get("action_points"):
        for i, action in enumerate(result["action_points"], 1):
            lines.append(f"{i}. {action}")
    else:
        lines.append("No action points found.")

    return "\n".join(lines)


async def download_audio(bot, file_id: str) -> tuple:
    file = await bot.get_file(file_id)
    file_name = file.file_path.split("/")[-1]
    async with httpx.AsyncClient() as client:
        response = await client.get(file.file_path)
        response.raise_for_status()
    return response.content, file_name


# ── Handlers ───────────────────────────────────────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await create_user(user.id, user.first_name, user.username)
    text = (
        f"👋 Hey *{user.first_name}*!\n\n"
        "I'm *VoxSync* — your AI voice note summarizer.\n\n"
        "Send me any voice note or audio file and I'll give you:\n\n"
        "📋 A clear summary\n"
        "📝 Full transcript\n"
        "✅ Action points extracted\n\n"
        f"*Free plan:* {FREE_DAILY_LIMIT} summaries per day\n\n"
        "Just send a voice note to get started 🎙"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎙 *How to use VoxSync:*\n\n"
        "1. Record or forward any voice note\n"
        "2. Send it to me\n"
        "3. Get your summary instantly!\n\n"
        "*Supported:* Voice notes, MP3, WAV, M4A, MP4\n\n"
        "*Commands:*\n"
        "/start - Welcome message\n"
        "/help - This message\n"
        "/usage - Check your daily usage"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def usage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)

    if not user:
        await update.message.reply_text("Send /start first to get started.")
        return

    used = user.get("daily_usage", 0)
    premium = await is_premium(user_id)

    if premium:
        text = f"✨ *Premium* — Unlimited summaries\nUsed today: *{used}*"
    else:
        remaining = max(0, FREE_DAILY_LIMIT - used)
        text = (
            f"📊 *Your Usage Today*\n\n"
            f"Used: *{used}/{FREE_DAILY_LIMIT}*\n"
            f"Remaining: *{remaining}*\n\n"
            "Resets at midnight UTC."
        )
    await update.message.reply_text(text, parse_mode="Markdown")


async def audio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    await create_user(user_id, user.first_name, user.username)

    db_user = await get_user(user_id)
    premium = await is_premium(user_id)
    daily_usage = db_user.get("daily_usage", 0) if db_user else 0

    if not premium and daily_usage >= FREE_DAILY_LIMIT:
        await update.message.reply_text(
            f"⚠️ You've used all *{FREE_DAILY_LIMIT} free summaries* for today.\n\n"
            "Resets at midnight UTC. 🔄",
            parse_mode="Markdown",
        )
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING
    )

    message = update.message
    if message.voice:
        file_id = message.voice.file_id
        file_name = "voice.ogg"
    elif message.audio:
        file_id = message.audio.file_id
        file_name = message.audio.file_name or "audio.mp3"
    elif message.video_note:
        file_id = message.video_note.file_id
        file_name = "video_note.mp4"
    else:
        await update.message.reply_text("Please send a voice note or audio file.")
        return

    processing_msg = await update.message.reply_text(
        "⏳ Processing your voice note...\n\n"
        "🔊 Transcribing → 📋 Summarizing → ✅ Extracting actions"
    )

    try:
        audio_data, detected_name = await download_audio(context.bot, file_id)
        result = await process_audio(audio_data, detected_name or file_name)

        await processing_msg.delete()

        if result.get("error"):
            await update.message.reply_text(
                f"❌ Something went wrong: {result['error']}\n\nPlease try again."
            )
            return

        await update.message.reply_text(format_result(result), parse_mode="Markdown")
        await increment_usage(user_id)

        if not premium:
            new_usage = daily_usage + 1
            remaining = FREE_DAILY_LIMIT - new_usage
            if remaining > 0:
                await update.message.reply_text(
                    f"📊 *{remaining} free summar{'y' if remaining == 1 else 'ies'} remaining today.*",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    "📊 *No free summaries left today.* Resets at midnight UTC.",
                    parse_mode="Markdown",
                )

    except Exception as e:
        logger.error(f"Audio processing error: {e}")
        await processing_msg.delete()
        await update.message.reply_text(
            "❌ An error occurred. Please try again."
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎙 Send me a *voice note* or *audio file* to summarize!\n\nUse /help for info.",
        parse_mode="Markdown",
    )


# ── App ────────────────────────────────────────────────────────────────────────

application = Application.builder().token(TELEGRAM_TOKEN).build()

application.add_handler(CommandHandler("start", start_handler))
application.add_handler(CommandHandler("help", help_handler))
application.add_handler(CommandHandler("usage", usage_handler))
application.add_handler(MessageHandler(
    filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE,
    audio_handler
))
application.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND,
    text_handler
))
