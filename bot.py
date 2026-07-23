import asyncio
import logging
import os
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from yt_dlp import YoutubeDL

# Отключаем большинство логов для стабильности
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)

# Настройка логирования только для ошибок
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://telegram-video-bot-5evo.onrender.com"
).rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram-webhook").strip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or None
WEBHOOK_URL = f"{BASE_URL}/{WEBHOOK_PATH}"

# Хранилище информации о видео
video_info_cache = {}
# Хранилище для сообщений, которые нужно удалить
messages_to_delete = {}

# Семафор для ограничения одновременных загрузок
download_semaphore = asyncio.Semaphore(2)

def create_menu_keyboard():
    """Создает клавиатуру меню как на картинке"""
    keyboard = [
        [KeyboardButton("/start    скачать видео")],
        [KeyboardButton("help   помощь")],
        [KeyboardButton("Меню    Сообщение...")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "🎬 **Video Downloader**\n"
        "────────────────────\n"
        "📥 **Отправьте ссылку для скачивания:**\n\n"
        "✅ **Поддерживаемые платформы:**\n"
        "• TikTok (без водяных знаков)\n"
        "• Instagram Reels/Posts\n"
        "⚡ **Просто отправьте ссылку!**\n"
        "────────────────────\n"
        "👇 **Ожидаю вашу ссылку...**"
    )

    await update.message.reply_text(
        message,
        parse_mode='Markdown',
        reply_markup=create_menu_keyboard()
    )

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню"""
    await update.message.reply_text(
        "📱 **Меню бота:**\n\n"
        "• Отправьте ссылку на видео для скачивания\n"
        "• Используйте кнопки ниже для навигации\n"
        "• Для помощи нажмите /help",
        reply_markup=create_menu_keyboard()
    )

def get_video_info_sync(url):
    """Синхронная версия получения информации о видео"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'socket_timeout': 90,
        'retries': 5,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        },
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                'title': info.get('title', 'Без названия'),
                'duration': info.get('duration', 0),
                'uploader': info.get('uploader', 'Неизвестно'),
                'thumbnail': info.get('thumbnail', ''),
                'view_count': info.get('view_count', 0),
                'like_count': info.get('like_count', 0),
                'description': info.get('description', ''),
                'formats': info.get('formats', [])
            }
    except Exception as e:
        logger.error(f"Error getting video info: {e}")
        return None

async def get_video_info(url):
    """Асинхронная версия получения информации о видео"""
    return await asyncio.to_thread(get_video_info_sync, url)

async def show_youtube_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """Показывает меню для YouTube видео"""
    async with download_semaphore:
        # Получаем информацию о видео
        info = await get_video_info(url)

        if not info:
            await update.message.reply_text(
                "❌ Не удалось получить информацию о видео\n"
                "Проверьте ссылку и попробуйте снова."
            )
            return

        # Сохраняем информацию в контексте
        context.user_data['current_video_url'] = url
        context.user_data['video_info'] = info

        # Форматируем длительность
        duration_sec = info['duration']
        if duration_sec > 0:
            duration_min = duration_sec // 60
            duration_sec_rem = duration_sec % 60
            duration_str = f"{duration_min}:{duration_sec_rem:02d}"
        else:
            duration_str = "Неизвестно"

        # Примерные размеры файлов
        quality_sizes = {
            144: "4-8MB",
            240: "5-10MB",
            360: "7-15MB",
            480: "9-20MB",
            720: "15-40MB",
            1080: "30-80MB",
            'best': "100-500MB"
        }

        # Создаем клавиатуру
        keyboard = [
            [
                InlineKeyboardButton("144p", callback_data="quality_144"),
                InlineKeyboardButton("240p", callback_data="quality_240"),
                InlineKeyboardButton("360p", callback_data="quality_360")
            ],
            [
                InlineKeyboardButton("480p", callback_data="quality_480"),
                InlineKeyboardButton("720p", callback_data="quality_720"),
                InlineKeyboardButton("1080p", callback_data="quality_1080")
            ],
            [
                InlineKeyboardButton("2K/4K", callback_data="quality_best"),
                InlineKeyboardButton("🎵 MP3", callback_data="audio_320"),
                InlineKeyboardButton("📋 Превью", callback_data="preview")
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        # Безопасное создание сообщения
        title = info['title'][:50] + "..." if len(info['title']) > 50 else info['title']
        uploader = info['uploader'][:30] + "..." if len(info['uploader']) > 30 else info['uploader']

        message = (
            f"🎬 **{title}**\n"
            f"👤 {uploader}\n"
            f"────────────────────\n"
            f"⏱️ Длительность: {duration_str}\n\n"
            f"📊 **Примерные размеры:**\n"
            f"• 144p: {quality_sizes[144]}\n"
            f"• 240p: {quality_sizes[240]}\n"
            f"• 360p: {quality_sizes[360]}\n"
            f"• 480p: {quality_sizes[480]}\n"
            f"• 720p: {quality_sizes[720]}\n"
            f"• 1080p: {quality_sizes[1080]}\n"
            f"• 2K/4K: {quality_sizes['best']}\n\n"
            f"👇 **Выберите качество:**"
        )

        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

def download_video_sync(url, ydl_opts):
    """Синхронная загрузка видео"""
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)

async def download_direct_video(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, platform: str):
    """Скачивает видео напрямую (для TikTok, Instagram)"""
    async with download_semaphore:
        status_msg = await update.message.reply_text(
            "⏳ Пожалуйста, подождите"
        )

        # Сохраняем ID сообщения для удаления
        user_id = update.effective_user.id
        if user_id not in messages_to_delete:
            messages_to_delete[user_id] = []
        messages_to_delete[user_id].append(status_msg.message_id)

        # Генерируем имя файла на основе времени
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        temp_filename = f"temp_video_{timestamp}"
        output_template = temp_filename + ".%(ext)s"

        # Настройки для разных платформ
        ydl_opts = {
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 90,
            'retries': 10,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            },
        }

        # Специальные настройки для TikTok
        if platform == 'tiktok':
            ydl_opts.update({
                'format': 'best[ext=mp4]/best',
                'extractor_args': {
                    'tiktok': {
                        'app_version': '30.2.0'
                    }
                }
            })

        # Специальные настройки для Instagram
        elif platform == 'instagram':
            ydl_opts.update({
                'format': 'best[ext=mp4]/best',
                'extractor_args': {
                    'instagram': {
                        'extract_post_types': ['video', 'reel', 'igtv']
                    }
                },
            })
        else:
            ydl_opts['format'] = 'best[ext=mp4]/best'

        try:
            # Обновляем статус
            await status_msg.edit_text(
                f"📥 Скачиваю видео...\n"
                "⏳ Пожалуйста, подождите"
            )

            # Скачиваем видео асинхронно с таймаутом
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(download_video_sync, url, ydl_opts),
                    timeout=300
                )
            except asyncio.TimeoutError:
                await status_msg.edit_text("❌ Таймаут загрузки. Попробуйте позже.")
                return

            # Ищем скачанный файл
            downloaded_file = None
            for ext in ['.mp4', '.webm', '.mkv', '.mov', '.avi']:
                if os.path.exists(temp_filename + ext):
                    downloaded_file = temp_filename + ext
                    break

            # Если не нашли с расширением, ищем файл с началом temp_filename
            if not downloaded_file:
                for file in os.listdir('.'):
                    if file.startswith(temp_filename):
                        downloaded_file = file
                        break

            if downloaded_file and os.path.exists(downloaded_file):
                # Получаем размер файла
                file_size = os.path.getsize(downloaded_file)

                # Получаем расширение файла
                file_ext = os.path.splitext(downloaded_file)[1].lower()

                # Если не mp4, переименовываем в mp4
                if file_ext != '.mp4':
                    new_filename = temp_filename + '.mp4'
                    os.rename(downloaded_file, new_filename)
                    downloaded_file = new_filename

                # Отправляем видео
                try:
                    with open(downloaded_file, 'rb') as video_file:
                        await update.message.reply_video(
                            video=video_file,
                            caption=(
                                f"✅ ВИДЕО ЗАГРУЖЕНО!\n"

                            ),
                            supports_streaming=True,
                            read_timeout=300,
                            write_timeout=600,
                            connect_timeout=60,
                            pool_timeout=60
                        )
                except Exception as e:
                    logger.error(f"Error sending video: {e}")
                    # Если не удалось отправить как видео, отправляем как документ
                    with open(downloaded_file, 'rb') as video_file:
                        await update.message.reply_document(
                            document=video_file,
                            caption=(
                                f"✅ ВИДЕО ЗАГРУЖЕНО!\n"

                            ),
                            read_timeout=300,
                            write_timeout=600,
                            connect_timeout=60,
                            pool_timeout=60
                        )

                # Удаляем временный файл
                try:
                    if os.path.exists(downloaded_file):
                        os.remove(downloaded_file)
                except:
                    pass

                # Удаляем сообщения о статусе
                if user_id in messages_to_delete:
                    for msg_id in messages_to_delete[user_id]:
                        try:
                            await context.bot.delete_message(
                                chat_id=update.effective_chat.id,
                                message_id=msg_id
                            )
                        except:
                            pass
                    messages_to_delete[user_id] = []
            else:
                raise Exception("Файл не найден после загрузки")

        except Exception as e:
            logger.error(f"Error downloading {platform} video: {str(e)}")

            # Безопасное сообщение об ошибке
            error_msg = str(e)[:100]

            await status_msg.edit_text(
                f"❌ ОШИБКА ЗАГРУЗКИ\n"
                f"────────────────────\n"
                f"⚠️ Проблема: {error_msg}\n\n"
                f"📌 Попробуйте:\n"
                f"• Проверить ссылку\n"
                f"• Попробовать позже\n"
                f"────────────────────\n"
                f"⚡ Попробуйте снова"
            )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "back":
        await start(query, context)

    elif data.startswith("quality_"):
        quality = data.replace("quality_", "")

        if 'current_video_url' in context.user_data:
            url = context.user_data['current_video_url']
            info = context.user_data.get('video_info', {})

            # Сохраняем ID сообщения для удаления
            user_id = query.from_user.id
            if user_id not in messages_to_delete:
                messages_to_delete[user_id] = []
            messages_to_delete[user_id].append(query.message.message_id)

            status_msg = await query.edit_message_text(
                f"⏳ Начинаю загрузку...\n\n"
                f"Пожалуйста, подождите ⏱️"
            )

            messages_to_delete[user_id].append(status_msg.message_id)

            # Ждем 2-3 секунды
            await asyncio.sleep(2.5)

            # Обновляем сообщение
            await status_msg.edit_text(
                f"📥 ЗАГРУЗКА ВИДЕО...\n"
                f"────────────────────\n"
                f"⏳ Пожалуйста, подождите"
            )

            # Начинаем скачивание
            await download_youtube_video(update, context, url, quality, info)

    elif data.startswith("audio_"):
        if 'current_video_url' in context.user_data:
            url = context.user_data['current_video_url']
            info = context.user_data.get('video_info', {})

            # Сохраняем ID сообщения для удаления
            user_id = query.from_user.id
            if user_id not in messages_to_delete:
                messages_to_delete[user_id] = []
            messages_to_delete[user_id].append(query.message.message_id)

            # Первое сообщение
            status_msg = await query.edit_message_text(
                f"Пожалуйста, подождите ⏱️"
            )

            messages_to_delete[user_id].append(status_msg.message_id)

            # Ждем 2-3 секунды
            await asyncio.sleep(2.5)

            # Второе сообщение
            await status_msg.edit_text(
                "⏳ Пожалуйста, подождите"
            )

            # Начинаем скачивание аудио
            await download_youtube_audio(update, context, url, info)

    elif data == "preview":
        if 'video_info' in context.user_data:
            info = context.user_data['video_info']

            safe_title = info['title'][:50] + "..." if len(info['title']) > 50 else info['title']

            await query.edit_message_text(
                f"📋 ПРЕВЬЮ ВИДЕО\n"
                f"────────────────────\n"
                f"🎬 Название: {safe_title}\n"
                f"👤 Автор: {info['uploader'][:30]}\n"
                f"⏱️ Длительность: {info['duration']} сек\n"
                f"────────────────────\n"
                f"🎯 Выберите качество для скачивания:"
            )

def download_youtube_video_sync(url, ydl_opts):
    """Синхронная загрузка YouTube видео"""
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)

async def download_youtube_video(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, quality: str,
                                 info: dict):
    """Скачивает YouTube видео с выбранным качеством"""
    async with download_semaphore:
        query = update.callback_query
        user_id = query.from_user.id

        # Генерируем имя файла на основе названия
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_title = re.sub(r'[<>:"/\\|?*]', '', info.get('title', 'video'))
        safe_title = re.sub(r'[\n\r\t]', ' ', safe_title)
        safe_title = safe_title[:50].strip()
        if not safe_title:
            safe_title = 'youtube_video'

        temp_filename = f"temp_{timestamp}"
        output_template = temp_filename + ".%(ext)s"

        # Определяем формат для скачивания (без конвертации)
        try:
            if quality == 'best':
                # Ищем лучший готовый MP4 файл
                format_str = 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best'
            else:
                height = int(quality)
                # Ищем MP4 с нужным качеством
                format_str = f'best[height<={height}][ext=mp4]/bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height}]'
        except:
            format_str = 'best[ext=mp4]/best'

        ydl_opts = {
            'format': format_str,
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 200,
            'retries': 10,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            },
            # Отключаем все, что требует FFmpeg
            'postprocessors': [],
            'merge_output_format': None,
        }

        try:
            # Обновляем статус
            status_msg = await query.message.reply_text(
                f"📥 ЗАГРУЗКА ВИДЕО...\n"
                f"⏳ Это может занять несколько минут"
            )

            messages_to_delete[user_id].append(status_msg.message_id)

            # Скачиваем видео асинхронно с таймаутом
            try:
                video_info = await asyncio.wait_for(
                    asyncio.to_thread(download_youtube_video_sync, url, ydl_opts),
                    timeout=300
                )
            except asyncio.TimeoutError:
                await query.message.reply_text("❌ Таймаут загрузки. Слишком большое видео.")
                return

            # Ищем скачанный файл
            downloaded_file = None
            for ext in ['.mp4', '.webm', '.mkv', '.mov']:
                if os.path.exists(temp_filename + ext):
                    downloaded_file = temp_filename + ext
                    break

            # Если не нашли с расширением
            if not downloaded_file:
                for file in os.listdir('.'):
                    if file.startswith(temp_filename) and not file.endswith('.part'):
                        downloaded_file = file
                        break

            if downloaded_file and os.path.exists(downloaded_file):
                # Получаем размер файла
                file_size = os.path.getsize(downloaded_file)

                # Получаем расширение файла
                file_ext = os.path.splitext(downloaded_file)[1].lower()

                # Переименовываем файл с безопасным именем
                final_filename = f"{safe_title}_{timestamp}{file_ext}"
                os.rename(downloaded_file, final_filename)

                # Отправляем видео
                try:
                    with open(final_filename, 'rb') as video_file:
                        await query.message.reply_video(
                            video=video_file,
                            caption=(
                                f"✅ ВИДЕО ЗАГРУЖЕНО!\n"

                            ),
                            supports_streaming=True,
                            read_timeout=300,
                            write_timeout=600,
                            connect_timeout=60,
                            pool_timeout=60
                        )
                except Exception as e:
                    logger.error(f"Error sending video: {e}")
                    # Если не удалось отправить как видео, отправляем как документ
                    with open(final_filename, 'rb') as video_file:
                        await query.message.reply_document(
                            document=video_file,
                            caption=(
                                f"✅ ВИДЕО ЗАГРУЖЕНО!\n"

                            ),
                            read_timeout=300,
                            write_timeout=600,
                            connect_timeout=60,
                            pool_timeout=60
                        )

                # Удаляем временный файл
                try:
                    if os.path.exists(final_filename):
                        os.remove(final_filename)
                except:
                    pass

                # Удаляем сообщения о статусе
                if user_id in messages_to_delete:
                    for msg_id in messages_to_delete[user_id]:
                        try:
                            await context.bot.delete_message(
                                chat_id=query.message.chat_id,
                                message_id=msg_id
                            )
                        except:
                            pass
                    messages_to_delete[user_id] = []

            else:
                raise Exception("Файл не найден после загрузки")

        except Exception as e:
            logger.error(f"Error downloading YouTube video: {str(e)}")

            # Безопасное сообщение об ошибке
            error_msg = str(e)[:100]

            error_message = await query.message.reply_text(
                f"❌ ОШИБКА ЗАГРУЗКИ\n"
                f"────────────────────\n"
                f"⚠️ Проблема: {error_msg}\n\n"
                f"📌 Попробуйте:\n"
                f"• Другое качество\n"
                f"• Другую ссылку\n"
                f"────────────────────\n"
                f"⚡ Попробуйте снова"
            )

            # Удаляем сообщения о статусе
            if user_id in messages_to_delete:
                for msg_id in messages_to_delete[user_id]:
                    try:
                        await context.bot.delete_message(
                            chat_id=query.message.chat_id,
                            message_id=msg_id
                        )
                    except:
                        pass
                messages_to_delete[user_id] = []

def download_youtube_audio_sync(url, ydl_opts):
    """Синхронная загрузка YouTube аудио"""
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)

async def download_youtube_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, info: dict):
    """Скачивает YouTube аудио без конвертации"""
    async with download_semaphore:
        query = update.callback_query
        user_id = query.from_user.id

        # Генерируем имя файла на основе названия
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_title = re.sub(r'[<>:"/\\|?*]', '', info.get('title', 'audio'))
        safe_title = re.sub(r'[\n\r\t]', ' ', safe_title)
        safe_title = safe_title[:50].strip()
        if not safe_title:
            safe_title = 'youtube_audio'

        temp_filename = f"temp_audio_{timestamp}"
        output_template = temp_filename + ".%(ext)s"

        # Настройки для скачивания аудио без конвертации
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 90,
            'retries': 10,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            },
            # Без пост-обработки (конвертации)
            'postprocessors': [],
        }

        try:
            # Обновляем статус
            status_msg = await query.message.reply_text(
                f"⚡ Статус: Скачивание...\n"
                f"⏳ Пожалуйста, подождите"
            )

            messages_to_delete[user_id].append(status_msg.message_id)

            # Скачиваем аудио асинхронно с таймаутом
            try:
                audio_info = await asyncio.wait_for(
                    asyncio.to_thread(download_youtube_audio_sync, url, ydl_opts),
                    timeout=300
                )
            except asyncio.TimeoutError:
                await query.message.reply_text("❌ Таймаут загрузки. Слишком большое аудио.")
                return

            # Ищем скачанный файл
            downloaded_file = None
            extensions = ['.m4a', '.mp3', '.webm', '.opus', '.mp4']

            for ext in extensions:
                if os.path.exists(temp_filename + ext):
                    downloaded_file = temp_filename + ext
                    break

            # Если не нашли с расширением
            if not downloaded_file:
                for file in os.listdir('.'):
                    if file.startswith(temp_filename) and not file.endswith('.part'):
                        downloaded_file = file
                        break

            if downloaded_file and os.path.exists(downloaded_file):
                # Получаем размер файла
                file_size = os.path.getsize(downloaded_file)

                # Определяем расширение файла
                file_ext = os.path.splitext(downloaded_file)[1].lower()

                # Переименовываем файл с правильным расширением
                final_filename = f"{safe_title}_{timestamp}{file_ext}"
                os.rename(downloaded_file, final_filename)

                # Пытаемся отправить как аудио (если формат поддерживается)
                try:
                    if file_ext in ['.mp3', '.m4a']:
                        with open(final_filename, 'rb') as audio_file:
                            await query.message.reply_audio(
                                audio=audio_file,
                                caption=(
                                    f"✅ АУДИО ЗАГРУЖЕНО!\n"

                                ),
                                title=safe_title[:50],
                                performer=info.get('uploader', '')[:30],
                                read_timeout=300,
                                write_timeout=600,
                                connect_timeout=60,
                                pool_timeout=60
                            )
                    else:
                        # Если формат не поддерживается, отправляем как документ
                        raise Exception("Формат не поддерживается")
                except Exception as e:
                    logger.error(f"Error sending audio: {e}")
                    # Отправляем как документ
                    with open(final_filename, 'rb') as audio_file:
                        await query.message.reply_document(
                            document=audio_file,
                            filename=f"{safe_title}{file_ext}",
                            caption=(
                                f"✅ АУДИО ЗАГРУЖЕНО!\n"

                            ),
                            read_timeout=300,
                            write_timeout=600,
                            connect_timeout=60,
                            pool_timeout=60
                        )

                # Удаляем временный файл
                try:
                    if os.path.exists(final_filename):
                        os.remove(final_filename)
                except:
                    pass

                # Удаляем сообщения о статусе
                if user_id in messages_to_delete:
                    for msg_id in messages_to_delete[user_id]:
                        try:
                            await context.bot.delete_message(
                                chat_id=query.message.chat_id,
                                message_id=msg_id
                            )
                        except:
                            pass
                    messages_to_delete[user_id] = []

            else:
                raise Exception("Аудио файл не найден")

        except Exception as e:
            logger.error(f"Error downloading YouTube audio: {str(e)}")

            # Безопасное сообщение об ошибке
            error_msg = str(e)[:100]

            error_message = await query.message.reply_text(
                f"❌ ОШИБКА СКАЧИВАНИЯ\n"
                f"────────────────────\n"
                f"⚠️ Проблема: {error_msg}\n\n"
                f"📌 Возможные причины:\n"
                f"• Проблема со ссылкой\n"
                f"• Ошибка скачивания\n"
                f"────────────────────\n"
                f"⚡ Попробуйте другую ссылку"
            )

            # Удаляем сообщения о статусе
            if user_id in messages_to_delete:
                for msg_id in messages_to_delete[user_id]:
                    try:
                        await context.bot.delete_message(
                            chat_id=query.message.chat_id,
                            message_id=msg_id
                        )
                    except:
                        pass
                messages_to_delete[user_id] = []

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает отправленную ссылку"""
    text = update.message.text.strip()

    # Проверяем, является ли текст командой меню
    if text == "Меню    Сообщение..." or text == "Меню":
        await show_menu(update, context)
        return
    elif text == "help   помощь" or text.lower() == "help":
        await help_command(update, context)
        return
    elif text == "/start    скачать видео":
        await start(update, context)
        return

    # Проверяем, является ли текст ссылкой
    if not re.match(r'^https?://', text):
        await update.message.reply_text(
            "❌ НЕВЕРНАЯ ССЫЛКА\n"
            "────────────────────\n"
            "Отправьте корректную ссылку:\n"
            "• https://tiktok.com/@user/...\n"
            "• https://instagram.com/reel/...\n\n"
            "⚡ Попробуйте еще раз"
        )
        return

    url = text

    # Определяем платформу
    if 'youtube.com' in url or 'youtu.be' in url:
        await update.message.reply_text(
            "❌ YouTube сейчас не поддерживается на сервере.\n"
            "Отправьте ссылку из TikTok или Instagram."
        )

    elif 'tiktok.com' in url or 'vm.tiktok.com' in url:
        # Для TikTok - сразу скачиваем
        await download_direct_video(update, context, url, 'tiktok')

    elif 'instagram.com' in url:
        # Для Instagram - сразу скачиваем
        await download_direct_video(update, context, url, 'instagram')

    else:
        # Для других платформ - пробуем скачать
        await download_direct_video(update, context, url, 'other')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "ℹ️ КАК ПОЛЬЗОВАТЬСЯ\n"
        "────────────────────\n"
        "1. Просто отправьте ссылку на видео\n\n"
        "✅ Поддерживаемые платформы:\n"
        "• TikTok - сразу видео\n"
        "• Instagram - Reels/Posts\n\n"
        "⚡ Начните с команды /start\n\n"
        "📱 Используйте кнопки меню для навигации"
    )

    await update.message.reply_text(
        help_text,
        reply_markup=create_menu_keyboard()
    )

def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в Render Environment")

    if not BASE_URL.startswith("https://"):
        raise RuntimeError("RENDER_EXTERNAL_URL должен начинаться с https://")

    print("🎬 VIDEO DOWNLOADER запускается через WEBHOOK...")
    print(f"🌐 Webhook URL: {WEBHOOK_URL}")
    print(f"🔌 Port: {PORT}")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    print("✅ Бот запущен через webhook!")
    print("📥 Просто отправьте ссылку на видео!")

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
