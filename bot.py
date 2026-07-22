import asyncio
import logging
import os
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

# Не даём серверу одновременно забить память большим количеством загрузок.
download_semaphore = asyncio.Semaphore(2)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("Health server started on port %s", PORT)
    server.serve_forever()


def download_video(url: str, output_template: str):
    options = {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "socket_timeout": 45,
        "restrictfilenames": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36"
            )
        },
    }

    with YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=True)


def find_downloaded_file(folder: Path) -> Path | None:
    files = [
        path for path in folder.iterdir()
        if path.is_file() and not path.name.endswith((".part", ".ytdl"))
    ]
    return max(files, key=lambda path: path.stat().st_size) if files else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь ссылку на TikTok или Instagram.\n\n"
        "Я попробую скачать видео и отправить его сюда."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Поддерживаются ссылки:\n"
        "• tiktok.com и vm.tiktok.com\n"
        "• instagram.com/reel и instagram.com/p\n\n"
        "Приватные публикации без разрешения скачать нельзя."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    lower_url = url.lower()

    is_tiktok = "tiktok.com/" in lower_url
    is_instagram = "instagram.com/" in lower_url

    if not url.startswith(("https://", "http://")) or not (is_tiktok or is_instagram):
        await update.message.reply_text(
            "Отправь правильную ссылку только с TikTok или Instagram."
        )
        return

    status = await update.message.reply_text("⏳ Скачиваю видео...")

    async with download_semaphore:
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                folder = Path(temp_dir)
                output_template = str(folder / "video.%(ext)s")

                await asyncio.wait_for(
                    asyncio.to_thread(download_video, url, output_template),
                    timeout=150,
                )

                downloaded_file = find_downloaded_file(folder)
                if downloaded_file is None:
                    raise RuntimeError("Видео не найдено после загрузки")

                file_size = downloaded_file.stat().st_size
                if file_size > 49 * 1024 * 1024:
                    await status.edit_text(
                        "❌ Видео больше 49 МБ. Telegram-бот не смог отправить его."
                    )
                    return

                with downloaded_file.open("rb") as video:
                    try:
                        await update.message.reply_video(
                            video=video,
                            caption="✅ Готово",
                            supports_streaming=True,
                            read_timeout=120,
                            write_timeout=120,
                        )
                    except Exception:
                        video.seek(0)
                        await update.message.reply_document(
                            document=video,
                            caption="✅ Готово",
                            read_timeout=120,
                            write_timeout=120,
                        )

                await status.delete()

        except asyncio.TimeoutError:
            await status.edit_text("❌ Загрузка заняла слишком много времени.")
        except Exception as exc:
            logger.exception("Download error")
            error_text = str(exc).replace("\n", " ")[:180]
            await status.edit_text(
                f"❌ Не получилось скачать.\n{error_text}\n\n"
                "Возможно, публикация приватная или платформа временно блокирует загрузку."
            )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная BOT_TOKEN не задана")

    Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    logger.info("Telegram bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
