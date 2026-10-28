#!/usr/bin/env python3
import asyncio
import html
import logging
import os
import re
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("captionrenamer")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN absent. Vérifie ~/captionrenamer/.env")

CHANNEL = "[@FilmEnVFR]"
VIDEO_EXTS = (
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".m4v", ".mpeg", ".mpg", ".3gp"
)

# Une file active par utilisateur et conversation.
PENDING = {}
COLLECTING = {}
LOCKS = {}


def get_video(message):
    if message.video:
        return message.video
    if message.document:
        name = (message.document.file_name or "").lower()
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("video/") or name.endswith(VIDEO_EXTS):
            return message.document
    return None


def parse_filename(filename):
    clean = Path(filename or "Vidéo").stem
    clean = clean.replace("_", " ").replace(".", " ")
    clean = re.sub(r"\s+", " ", clean).strip()

    season = episode = None
    patterns = [
        r"(?i)\bS(\d{1,2})\s*E(\d{1,3})\b",
        r"(?i)\b(\d{1,2})x(\d{1,3})\b",
        r"(?i)\bSeason\s*(\d{1,2})\s*(?:Episode|Ep)\s*(\d{1,3})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, clean)
        if match:
            season, episode = map(int, match.groups())
            clean = clean[:match.start()] + " " + clean[match.end():]
            break

    noise = re.compile(
        r"(?i)\b(480p|576p|720p|1080p|1440p|2160p|4k|8k|"
        r"web[- .]?dl|web[- .]?rip|blu[- .]?ray|brrip|bdrip|dvdrip|"
        r"hdtv|hdrip|x264|x265|h\.?264|h\.?265|hevc|av1|aac|ac3|"
        r"multi|multisub|vostfr|subfrench|truefrench|french|"
        r"proper|repack|extended|uncut|remux|10bit|8bit|web)\b"
    )
    clean = noise.sub(" ", clean)
    # Supprimer les "+" résiduels et les séparateurs techniques.
    clean = re.sub(r"\s*\+\s*", " ", clean)
    clean = re.sub(r"[\[\](){}]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" -_.+")
    clean = clean or "Vidéo"

    parts = [clean]
    if season is not None and episode is not None:
        parts.append(f"Saison {season} Épisode {episode}")
    parts.append("VF")
    return " ".join(parts)


def format_caption(text):
    # La chaîne de chaîne est toujours ajoutée sur une ligne séparée.
    text = re.sub(
        r"(?im)^\s*@FilmEnVFR\s*$", "", text or ""
    ).strip()
    text = text or "Vidéo VF"
    result = f"<b>{html.escape(text)}</b>\n<b>{CHANNEL}</b>"
    if len(text) + len(CHANNEL) > 1000:
        raise ValueError("La légende est trop longue.")
    return result


def increment_episode(text, number):
    pattern = re.compile(
        r"(?i)(Épisode|Episode|Ép\.?|Ep\.?)\s*0*(\d+)"
    )
    if not pattern.search(text):
        return None
    return pattern.sub(
        lambda m: f"{m.group(1)} {number}", text, count=1
    )


def keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Automatique", callback_data="auto")],
        [InlineKeyboardButton("✍️ Manuel (une légende pour tout le lot)", callback_data="manual")],
        [InlineKeyboardButton("❌ Annuler", callback_data="cancel")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎬 Renommeur de légendes vidéo\n\n"
        "Envoie une ou plusieurs vidéos. Je regrouperai les vidéos "
        "reçues rapprochées, puis tu choisiras le mode automatique "
        "ou manuel.\n\n"
        "Les légendes seront en gras, avec @FilmEnVFR sur la "
        "deuxième ligne. La qualité vidéo sera exclue.\n\n"
        "/cancel : annuler le lot en attente."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = (update.effective_user.id, update.effective_chat.id)
    state = COLLECTING.pop(key, None)
    if state and state.get("task"):
        state["task"].cancel()
    PENDING.pop(key, None)
    context.user_data.pop("awaiting_caption", None)
    await update.effective_message.reply_text("Lot annulé.")


async def send_item(bot, item, caption):
    kwargs = {
        "chat_id": item["chat_id"],
        "caption": caption,
        "parse_mode": "HTML",
        "reply_to_message_id": item["message_id"],
    }
    if item["is_document"]:
        await bot.send_document(document=item["file_id"], **kwargs)
    else:
        await bot.send_video(video=item["file_id"], **kwargs)


async def flush_batch(key, context):
    try:
        await asyncio.sleep(1.8)
        state = COLLECTING.get(key)
        if not state:
            return
        items = state["items"]
        COLLECTING.pop(key, None)
        if not items:
            return

        # Si un lot attend déjà un choix, les nouvelles vidéos le rejoignent.
        pending = PENDING.get(key)
        if pending:
            pending["items"].extend(items)
            try:
                await context.bot.edit_message_text(
                    chat_id=pending["chat_id"],
                    message_id=pending["prompt_id"],
                    text=f"📁 {len(pending['items'])} vidéo(s) en attente.\nChoisis le mode :",
                    reply_markup=keyboard(),
                )
            except Exception:
                pass
            return

        PENDING[key] = {
            "items": items,
            "chat_id": key[1],
            "prompt_id": None,
        }
        names = "\n".join(
            f"• {html.escape(item['filename'])}" for item in items[:8]
        )
        if len(items) > 8:
            names += f"\n… et {len(items) - 8} autre(s)"
        prompt = await context.bot.send_message(
            chat_id=key[1],
            text=f"📁 {len(items)} vidéo(s) reçue(s)\n\n{names}\n\nChoisis le mode :",
            reply_markup=keyboard(),
        )
        PENDING[key]["prompt_id"] = prompt.message_id
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Erreur pendant le regroupement des vidéos")


async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    media = get_video(message)
    if not media:
        return

    key = (update.effective_user.id, update.effective_chat.id)
    item = {
        "chat_id": update.effective_chat.id,
        "message_id": message.message_id,
        "file_id": media.file_id,
        "filename": getattr(media, "file_name", None) or "Vidéo",
        "is_document": bool(message.document),
    }

    # Si un choix est déjà affiché, ajoute la vidéo à ce lot.
    pending = PENDING.get(key)
    if pending:
        pending["items"].append(item)
        try:
            await context.bot.edit_message_text(
                chat_id=pending["chat_id"],
                message_id=pending["prompt_id"],
                text=f"📁 {len(pending['items'])} vidéo(s) en attente.\nChoisis le mode :",
                reply_markup=keyboard(),
            )
        except Exception:
            pass
        return

    state = COLLECTING.setdefault(key, {"items": [], "task": None})
    state["items"].append(item)
    if state.get("task"):
        state["task"].cancel()
    state["task"] = asyncio.create_task(flush_batch(key, context))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = (query.from_user.id, query.message.chat_id)
    pending = PENDING.get(key)
    if not pending:
        await query.edit_message_text("Lot expiré. Envoie à nouveau les vidéos.")
        return

    if query.data == "cancel":
        PENDING.pop(key, None)
        context.user_data.pop("awaiting_caption", None)
        await query.edit_message_text("Lot annulé.")
        return

    if query.data == "manual":
        context.user_data["awaiting_caption"] = key
        await query.edit_message_text(
            "✍️ Envoie UNE légende de départ pour tout le lot.\n\n"
            "Pour numéroter les épisodes, écris par exemple :\n"
            "The Last of Us Saison 2 Épisode 1 VF\n\n"
            "Le bot fera ensuite Épisode 2, Épisode 3, etc.\n"
            "/cancel pour annuler."
        )
        return

    if query.data == "auto":
        items = pending["items"]
        await query.edit_message_text(f"⏳ Renommage automatique de {len(items)} vidéo(s)…")
        success = 0
        for item in items:
            try:
                caption = format_caption(parse_filename(item["filename"]))
                await send_item(context.bot, item, caption)
                success += 1
            except Exception:
                log.exception("Erreur de publication automatique")
        PENDING.pop(key, None)
        await context.bot.send_message(
            chat_id=key[1],
            text=f"✅ Terminé : {success}/{len(items)} vidéo(s) renommée(s)."
        )


async def manual_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("awaiting_caption")
    if not key:
        return
    pending = PENDING.get(key)
    if not pending:
        context.user_data.pop("awaiting_caption", None)
        await update.effective_message.reply_text("Lot introuvable. Renvoie les vidéos.")
        return

    base = (update.effective_message.text or "").strip()
    base = re.sub(r"(?im)^\s*@FilmEnVFR\s*$", "", base).strip()
    if not base:
        await update.effective_message.reply_text("La légende est vide. Réessaie ou /cancel.")
        return

    items = pending["items"]
    match = re.search(r"(?i)(Épisode|Episode|Ép\.?|Ep\.?)\s*0*(\d+)", base)
    if len(items) > 1 and not match:
        await update.effective_message.reply_text(
            "Pour numéroter plusieurs épisodes, ajoute le numéro dans la légende, "
            "par exemple : « Saison 1 Épisode 1 VF ».\nRéessaie ou /cancel."
        )
        return

    start_number = int(match.group(2)) if match else None
    success = 0
    for index, item in enumerate(items):
        text = base
        if start_number is not None:
            text = increment_episode(base, start_number + index)
        try:
            caption = format_caption(text)
            await send_item(context.bot, item, caption)
            success += 1
        except Exception:
            log.exception("Erreur de publication manuelle")

    PENDING.pop(key, None)
    context.user_data.pop("awaiting_caption", None)
    await update.effective_message.reply_text(
        f"✅ Terminé : {success}/{len(items)} vidéo(s) renommée(s)."
    )


def main():
    from telegram.request import HTTPXRequest

    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=40,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=30,
    )
    app = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .get_updates_request(
            HTTPXRequest(
                connection_pool_size=4,
                connect_timeout=40,
                read_timeout=60,
                write_timeout=60,
                pool_timeout=30,
            )
        )
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(
        button_handler, pattern=r"^(auto|manual|cancel)$"
    ))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.ALL, receive_video
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, manual_caption
    ))
    log.info("CaptionRenamer démarré.")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()