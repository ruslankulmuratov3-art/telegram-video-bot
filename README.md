# TikTok + Instagram Telegram Bot

Файлы готовы для Render.

1. Смените токен через BotFather, потому что старый токен был опубликован в коде.
2. Загрузите эти файлы в новый GitHub-репозиторий.
3. На Render создайте Web Service из репозитория.
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `python bot.py`
6. Добавьте Environment Variable:
   - Key: `BOT_TOKEN`
   - Value: новый токен от BotFather
7. Health Check Path: `/health`
