import os
from dotenv import load_dotenv

load_dotenv()

# --- Ключевые настройки ---

# Токен твоего Telegram-бота.
# Лучше всего брать его из переменной окружения для безопасности.
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")

# Строка подключения к базе данных PostgreSQL.
# Формат: "postgresql://ПОЛЬЗОВАТЕЛЬ:ПАРОЛЬ@ХОСТ:ПОРТ/ИМЯ_БАЗЫ"
DATABASE_URL = os.getenv("DATABASE_URL")

# --- Настройки бота ---

# Список числовых Telegram ID администраторов, которые могут управлять ботом.
# Пример: ADMIN_IDS = [111111111, 222222222]
raw_admin_ids = os.getenv("ADMIN_IDS")
ADMIN_IDS = [int(x.strip()) for x in raw_admin_ids.split(",") if x.strip()]

# ID основной группы для анонсов
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID"))

# Опции КНБ
RPS_OPTIONS = ['камень', 'ножницы', 'бумага']