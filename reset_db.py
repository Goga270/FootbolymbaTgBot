# Файл: reset_db.py

import sys
from database import engine, Base, create_db_tables

print("⚠️ ПРЕДУПРЕЖДЕНИЕ: Этот скрипт полностью удалит ВСЕ данные и пересоздаст таблицы в БД.")
confirm = input("Вы уверены, что хотите начать чистый тест? (y/n): ")
if confirm.lower() != 'y':
    print("Сброс базы данных отменен.")
    sys.exit()

try:
    print("Удаление старых таблиц...")
    Base.metadata.drop_all(bind=engine)

    print("Создание новых чистых таблиц...")
    create_db_tables()

    print("✅ База данных успешно очищена! Все таблицы пересозданы с нуля.")
except Exception as e:
    print(f"❌ Произошла ошибка при сбросе базы данных: {e}")