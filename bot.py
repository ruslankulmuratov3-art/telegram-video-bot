import asyncio
import logging
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import imageio_ffmpeg
from telegram import LinkPreviewOptions, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL


# ===================== НАСТРОЙКИ =====================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://telegram-video-bot-5evo.onrender.com",
).rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram-webhook").strip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or None
WEBHOOK_URL = f"{BASE_URL}/{WEBHOOK_PATH}"

# Необязательно: путь к cookies.txt в Render Secret Files.
# Нужен только для отдельных TikTok-публикаций, которые требуют вход.
COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()

MAX_CONCURRENT_DOWNLOADS = 2
DOWNLOAD_TIMEOUT = 300
SEND_READ_TIMEOUT = 300
SEND_WRITE_TIMEOUT = 600
CONNECT_TIMEOUT = 60
POOL_TIMEOUT = 60

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ===================== ТЕКСТЫ =====================

START_TEXT = (
    "⚡ <b>TT Save</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Скачивайте видео и музыку прямо в Telegram.\n\n"
    "🎵 <b>TikTok</b> — без водяного знака\n"
    "📸 <b>Instagram</b> — Reels и видео-публикации\n\n"
    "🚀 Быстро\n"
    "🆓 Бесплатно\n"
    "🔒 Без регистрации\n\n"
    "📥 <b>Просто отправьте ссылку.</b>"
)

HELP_TEXT = (
    "ℹ️ <b>Как пользоваться</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "1. Скопируйте ссылку на TikTok или Instagram.\n"
    "2. Отправьте её в этот чат.\n"
    "3. Бот пришлёт видео и отдельный MP3-файл.\n\n"
    "💡 Отправляйте ссылки по одной."
)

PROCESSING_TEXT = (
    "✨ <b>Обрабатываю ссылку</b>\n"
    "Сохраняю видео и подготавливаю музыку…"
)

INVALID_LINK_TEXT = (
    "❌ <b>Ссылка не распознана</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Отправьте ссылку из TikTok или Instagram."
)

UNSUPPORTED_TEXT = (
    "🚫 <b>Эта платформа пока не поддерживается</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Сейчас бот работает только с TikTok и Instagram."
)

RESTRICTED_TEXT = (
    "🔐 <b>Это видео доступно только после входа</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "TikTok ограничил публикацию по возрасту или содержанию, "
    "поэтому сервер не может скачать её без авторизации.\n\n"
    "Обычные открытые видео продолжат скачиваться нормально."
)

DOWNLOAD_ERROR_TEXT = (
    "❌ <b>Не удалось скачать публикацию</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Возможно, видео удалено, закрыто или сервис временно ограничил доступ.\n\n"
    "Попробуйте другую ссылку или повторите позже."
)

AUDIO_ERROR_TEXT = (
    "⚠️ Видео отправлено, но музыку отдельно подготовить не удалось."
)


# ===================== ЗАГРУЗКА =====================

def detect_platform(url: str) -> Optional[str]:
    value = url.lower()

    if any(domain in value for domain in (
        "tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
    )):
        return "tiktok"

    if "instagram.com" in value:
        return "instagram"

    return None


def build_ydl_options(output_template: str, platform: str) -> dict:
    options = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 90,
        "retries": 10,
        "fragment_retries": 10,
        "continuedl": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }

    if platform == "tiktok":
        options["extractor_args"] = {
            "tiktok": {
                "app_version": ["30.2.0"],
            },
        }

    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        options["cookiefile"] = COOKIES_FILE

    return options


def download_video_sync(url: str, output_template: str, platform: str) -> None:
    with YoutubeDL(build_ydl_options(output_template, platform)) as ydl:
        ydl.extract_info(url, download=True)


def find_downloaded_video(folder: Path) -> Optional[Path]:
    candidates = [
        path for path in folder.iterdir()
        if path.is_file()
        and not path.name.endswith((".part", ".ytdl"))
        and path.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov", ".avi"}
    ]

    if not candidates:
        return None

    return max(candidates, key=lambda path: path.stat().st_size)


def extract_audio_sync(video_path: Path, audio_path: Path) -> None:
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

    command = [
        ffmpeg_path,
        "-y",
        "-i", str(video_path),
        "-vn",
        "-codec:a", "libmp3lame",
        "-b:a", "192k",
        str(audio_path),
    ]

    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=180,
    )


def is_restricted_error(error: Exception) -> bool:
    text = str(error).lower()
    markers = (
        "not be comfortable for some audiences",
        "log in for",
        "login required",
        "sign in",
        "age-restricted",
        "age restricted",
    )
    return any(marker in text for marker in markers)


# ===================== ОТПРАВКА =====================

async def send_video(update: Update, video_path: Path) -> None:
    try:
        with video_path.open("rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption="✅ <b>Видео готово</b>",
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                read_timeout=SEND_READ_TIMEOUT,
                write_timeout=SEND_WRITE_TIMEOUT,
                connect_timeout=CONNECT_TIMEOUT,
                pool_timeout=POOL_TIMEOUT,
            )
    except (BadRequest, TimedOut, NetworkError) as exc:
        logger.warning("Отправка видео как video не удалась: %s", exc)

        with video_path.open("rb") as video_file:
            await update.message.reply_document(
                document=video_file,
                filename=f"TT_Save{video_path.suffix or '.mp4'}",
                caption="✅ <b>Видео готово</b>",
                parse_mode=ParseMode.HTML,
                read_timeout=SEND_READ_TIMEOUT,
                write_timeout=SEND_WRITE_TIMEOUT,
                connect_timeout=CONNECT_TIMEOUT,
                pool_timeout=POOL_TIMEOUT,
            )


async def send_audio(update: Update, audio_path: Path) -> None:
    try:
        with audio_path.open("rb") as audio_file:
            await update.message.reply_audio(
                audio=audio_file,
                filename="TT_Save_Audio.mp3",
                title="TT Save Audio",
                caption="🎧 <b>Музыка из видео</b>",
                parse_mode=ParseMode.HTML,
                read_timeout=SEND_READ_TIMEOUT,
                write_timeout=SEND_WRITE_TIMEOUT,
                connect_timeout=CONNECT_TIMEOUT,
                pool_timeout=POOL_TIMEOUT,
            )
    except (BadRequest, TimedOut, NetworkError) as exc:
        logger.warning("Отправка MP3 как audio не удалась: %s", exc)

        with audio_path.open("rb") as audio_file:
            await update.message.reply_document(
                document=audio_file,
                filename="TT_Save_Audio.mp3",
                caption="🎧 <b>Музыка из видео</b>",
                parse_mode=ParseMode.HTML,
                read_timeout=SEND_READ_TIMEOUT,
                write_timeout=SEND_WRITE_TIMEOUT,
                connect_timeout=CONNECT_TIMEOUT,
                pool_timeout=POOL_TIMEOUT,
            )


# ===================== ОБРАБОТЧИКИ =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        START_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
        link_preview_options=NO_PREVIEW,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
        link_preview_options=NO_PREVIEW,
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    if not re.match(r"^https?://", text, flags=re.IGNORECASE):
        await update.message.reply_text(
            INVALID_LINK_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
            link_preview_options=NO_PREVIEW,
        )
        return

    platform = detect_platform(text)

    if not platform:
        await update.message.reply_text(
            UNSUPPORTED_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
            link_preview_options=NO_PREVIEW,
        )
        return

    status_message = await update.message.reply_text(
        PROCESSING_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
        link_preview_options=NO_PREVIEW,
    )

    async with download_semaphore:
        with tempfile.TemporaryDirectory(prefix="tt_save_") as temp_dir:
            folder = Path(temp_dir)
            video_template = str(folder / f"{uuid.uuid4().hex}.%(ext)s")
            audio_path = folder / "TT_Save_Audio.mp3"

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        download_video_sync,
                        text,
                        video_template,
                        platform,
                    ),
                    timeout=DOWNLOAD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await status_message.edit_text(
                    "⌛ <b>Загрузка заняла слишком много времени</b>\n"
                    "Попробуйте повторить позже.",
                    parse_mode=ParseMode.HTML,
                )
                return
            except Exception as exc:
                logger.exception("Ошибка загрузки %s: %s", platform, exc)

                error_text = RESTRICTED_TEXT if is_restricted_error(exc) else DOWNLOAD_ERROR_TEXT
                await status_message.edit_text(
                    error_text,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=NO_PREVIEW,
                )
                return

            video_path = find_downloaded_video(folder)

            if not video_path:
                await status_message.edit_text(
                    DOWNLOAD_ERROR_TEXT,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=NO_PREVIEW,
                )
                return

            try:
                await asyncio.to_thread(
                    extract_audio_sync,
                    video_path,
                    audio_path,
                )
                audio_ready = audio_path.exists() and audio_path.stat().st_size > 0
            except Exception as exc:
                logger.exception("Ошибка извлечения MP3: %s", exc)
                audio_ready = False

            try:
                await send_video(update, video_path)

                if audio_ready:
                    await send_audio(update, audio_path)
                else:
                    await update.message.reply_text(
                        AUDIO_ERROR_TEXT,
                        parse_mode=ParseMode.HTML,
                    )
            except Exception as exc:
                logger.exception("Ошибка отправки файлов: %s", exc)
                await status_message.edit_text(
                    DOWNLOAD_ERROR_TEXT,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=NO_PREVIEW,
                )
                return

    try:
        await status_message.delete()
    except Exception:
        pass


# ===================== ЗАПУСК =====================

def main() -> None:
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в Render Environment")

    if not BASE_URL.startswith("https://"):
        raise RuntimeError("RENDER_EXTERNAL_URL должен начинаться с https://")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )

    print("⚡ TT Save запускается через webhook")
    print(f"🌐 Webhook URL: {WEBHOOK_URL}")
    print(f"🔌 Port: {PORT}")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
