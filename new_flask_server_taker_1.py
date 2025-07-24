import time
import requests
import logging
import threading
from flask import Flask
from telegram import Bot
from datetime import datetime
import os
from dotenv import load_dotenv
import asyncio
import logging
from threading import Lock  # Импортируем Lock

# ------------------ НАСТРОЙКИ ------------------
# Загружаем переменные из .env
load_dotenv()

# Читаем их так же, как переменные окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
LOG_SERVER_URL = os.getenv("LOG_SERVER_URL")
PING_URL = os.getenv("PING_URL")

TIMEOUT = 120           # Максимальное время (сек) без новых данных
TIME_BEFORE_PING = 60   # Если данные не поступают в течение 60 сек – пингуем сервер
MAX_PING_ATTEMPTS = 2   # Число попыток ping
CHECK_INTERVAL = 60     # Интервал проверки логов (сек)

PLAN_POSITIONS_THRESHOLD = 200_000  # Минимально допустимое значение «Плановых чистых позиций»

# ------------------ ЛОГИРОВАНИЕ ------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ------------------ ИНИЦАЛИЗАЦИЯ ------------------

app = Flask(__name__)
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ------------------ ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ------------------

# Храним последние полученные данные
last_data = {
    "timestamp": None,
    "connectionStatus": None,
    "balance": None,
    "planNetPositions": None,
    # Для контроля обновления данных по сделкам – хранится время в виде объекта datetime.
    "lastTradeTime": None,
    "last_received": 0
}

# Словарь для отслеживания уже отправленных уведомлений по типам ошибок,
# чтобы не спамить повторными сообщениями.
active_errors = {
    "server_down": False,          # Сервер не отвечает на ping
    "connection": False,           # Quik не подключен
    "balance": False,              # Баланс снизился более чем на 1%
    "plan_positions_crit": False,  # Плановые чистые позиции ниже порога
    "data_delay": False,           # Данные не обновляются более TIMEOUT сек
    # Ошибки, связанные с обновлением сделок:
    "trade_no_data": False,        # Пришло значение "нет данных"
    "trade_stuck": False,          # Время сделки не изменилось (нет обновления)
    "trade_delay": False           # Разница между сделками больше 60 секунд
}

# ------------------ МЕХАНИЗМ АГРЕГАЦИИ УВЕДОМЛЕНИЙ ------------------

pending_notifications = []
pending_lock = Lock()

def add_notification(message: str):
    """Добавляет уведомление в очередь (если оно ещё не добавлено)."""
    with pending_lock:
        if message not in pending_notifications:
            pending_notifications.append(message)
            logging.info(f"Уведомление добавлено в очередь: {message}")

def telegram_worker():
    """
    Асинхронно отправляет сгруппированные уведомления из очереди.
    Создаёт постоянный event loop в этом потоке, чтобы избежать ошибки "Event loop is closed".
    Каждые 5 секунд проверяет очередь, объединяет уведомления и отправляет их в Telegram.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        time.sleep(5)  # Ждем 5 секунд между проверками
        combined_message = None  # Инициализируем переменную для безопасного использования
        with pending_lock:
            if pending_notifications:
                unique_msgs = list(dict.fromkeys(pending_notifications))
                combined_message = "\n".join(unique_msgs)
                pending_notifications.clear()
        if combined_message:
            try:
                loop.run_until_complete(
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=combined_message)
                )
                logging.info("Групповое уведомление отправлено в Telegram.")
            except Exception as e:
                logging.error(f"Ошибка отправки группового уведомления: {e}")

# Запускаем воркер для отправки уведомлений в отдельном потоке
threading.Thread(target=telegram_worker, daemon=True).start()

# ------------------ ФУНКЦИИ ПРОЕКТА ------------------

def fetch_logs():
    """
    Запрашивает логи с удалённого сервера.
    Возвращает текст лога или None при ошибке.
    """
    try:
        response = requests.get(LOG_SERVER_URL, timeout=10)
        if response.status_code == 200:
            logging.info("Логи успешно получены!")
            return response.text
        else:
            logging.error(f"Ошибка запроса логов: {response.status_code}")
            return None
    except requests.RequestException as e:
        logging.error(f"Ошибка получения логов: {e}")
        return None

def parse_log_line(line: str):
    """
    Парсит строку лога. Ожидается формат:
    timestamp; connectionStatus; balance; planNetPositions; lastTradeInfo; ...
    
    Пример строки:
    "2025-02-14 14:07:12;true;2112506.300000;403357.58;14:07:12; fut_code=MXH5; price=329200.0; volume=1.0"
    
    Возвращает словарь с ключами:
      - timestamp (строка)
      - connectionStatus (строка)
      - balance (float)
      - planNetPositions (float)
      - lastTradeInfo (строка) – время последней сделки
    Если строка имеет неверный формат – возвращает None.
    """
    parts = line.strip().split(";")
    if len(parts) >= 5:
        try:
            return {
                "timestamp": parts[0].strip(),
                "connectionStatus": parts[1].strip(),
                "balance": float(parts[2].strip()),
                "planNetPositions": float(parts[3].strip()),
                "lastTradeInfo": parts[4].strip()  # Ожидается время сделки в формате HH:MM:SS или "нет данных"
            }
        except ValueError:
            logging.warning(f"Ошибка преобразования типов в строке лога: {line}")
            return None
    else:
        logging.warning(f"Неверный формат строки лога: {line}")
        return None

def ping_quik_server():
    """
    Пингует сервер Quik. Выполняется MAX_PING_ATTEMPTS раз.
    Возвращает True, если хотя бы один ping успешен.
    """
    for attempt in range(MAX_PING_ATTEMPTS):
        try:
            response = requests.get(PING_URL, timeout=5)
            if response.status_code == 200:
                return True
        except requests.RequestException as e:
            logging.warning(f"Попытка пинга #{attempt + 1} не удалась: {e}")
        time.sleep(1)
    return False

def analyze_log(data: dict):
    """
    Анализирует полученные данные лога:
      - Обновляет общие показатели (timestamp, connectionStatus, balance, planNetPositions).
      - Проверяет связь с сервером, состояние подключения, баланс и критичный уровень плановых чистых позиций.
      - Анализирует данные по сделкам:
          * Если в поле lastTradeInfo приходит "нет данных" – уведомление.
          * Если данные по сделкам приходят, то время сделки (формат HH:MM:SS) конвертируется в datetime и сравнивается с предыдущим значением.
            - Если новое время совпадает с предыдущим – данные не обновляются.
            - Если разница между сделками больше 60 секунд – уведомление о задержке.
    """
    global last_data

    # Обновляем общие показатели
    last_data["timestamp"] = data["timestamp"]
    last_data["connectionStatus"] = data["connectionStatus"]
    last_data["balance"] = data["balance"]
    last_data["planNetPositions"] = data["planNetPositions"]
    last_data["last_received"] = time.time()

    # --- 1. Проверка доступности сервера (ping) ---
    current_time = time.time()
    if current_time - last_data["last_received"] > TIME_BEFORE_PING:
        if not ping_quik_server():
            if not active_errors["server_down"]:
                add_notification("❌ Сервер недоступен (ping не прошёл)!")
                active_errors["server_down"] = True
            return
        else:
            if active_errors["server_down"]:
                add_notification("✅ Сервер снова доступен (ping успешен).")
                active_errors["server_down"] = False

    # --- 2. Проверка подключения Quik ---
    if data["connectionStatus"].lower() == "false":
        if not active_errors["connection"]:
            add_notification("❌ Quik не подключен!")
            active_errors["connection"] = True
    else:
        if active_errors["connection"]:
            add_notification("✅ Подключение Quik восстановлено.")
            active_errors["connection"] = False

    # --- 3. Проверка баланса (снижение более чем на 1%) ---
    old_balance = last_data.get("balance")
    if old_balance is not None:
        if data["balance"] < old_balance * 0.99:
            if not active_errors["balance"]:
                add_notification(f"❗ Баланс снизился более чем на 1%! Был: {old_balance}, стал: {data['balance']}")
                active_errors["balance"] = True
        else:
            if active_errors["balance"]:
                add_notification(f"✅ Баланс восстановился. Текущее значение: {data['balance']}")
                active_errors["balance"] = False

    # --- 4. Проверка критичного уровня плановых чистых позиций ---
    plan_val = data["planNetPositions"]
    if plan_val < PLAN_POSITIONS_THRESHOLD:
        if not active_errors["plan_positions_crit"]:
            add_notification(f"❌ Свободное ГО на критическом уровне = {plan_val}")
            active_errors["plan_positions_crit"] = True
    else:
        if active_errors["plan_positions_crit"]:
            add_notification(f"✅ Свободное ГО восстановлено. Текущее значение = {plan_val}")
            active_errors["plan_positions_crit"] = False

    # --- 5. Проверка данных по сделкам ---
    trade_time_str = data["lastTradeInfo"].strip()
    if trade_time_str.lower() in ["нет данных", "no data", "данные отсутствуют"]:
        if not active_errors["trade_no_data"]:
            add_notification("📉 Нет данных о сделках в Quik!")
            active_errors["trade_no_data"] = True
        return
    else:
        if active_errors["trade_no_data"]:
            add_notification("✅ Данные о сделках снова доступны.")
            active_errors["trade_no_data"] = False

    try:
        trade_time = datetime.strptime(trade_time_str, "%H:%M:%S")
    except ValueError:
        logging.error(f"Ошибка преобразования времени сделки: {trade_time_str}")
        return

    if last_data.get("lastTradeTime"):
        prev_trade_time = last_data["lastTradeTime"]

        # Если время сделки не изменилось – данные не обновляются
        if trade_time == prev_trade_time:
            if not active_errors.get("trade_stuck"):
                add_notification(f"⛔ Данные о сделках не обновляются! Время сделки осталось {trade_time_str}.")
                active_errors["trade_stuck"] = True
        else:
            if active_errors.get("trade_stuck"):
                add_notification("✅ Сделки снова обновляются корректно.")
                active_errors["trade_stuck"] = False

            time_diff = (trade_time - prev_trade_time).total_seconds()
            if time_diff > 90:
                if not active_errors.get("trade_delay"):
                    add_notification(f"⚠️ Задержка обновления сделок! Разница: {int(time_diff)} секунд.")
                    active_errors["trade_delay"] = True
            else:
                if active_errors.get("trade_delay"):
                    add_notification("✅ Данные о сделках снова обновляются в нормальном режиме.")
                    active_errors["trade_delay"] = False

    last_data["lastTradeTime"] = trade_time

def background_log_checker():
    """
    Фоновая задача:
      - Каждые CHECK_INTERVAL секунд запрашивает логи.
      - Извлекает последнюю непустую строку, парсит её и анализирует данные.
      - Проверяет, не прошло ли более TIMEOUT секунд без обновления данных.
    """
    while True:
        logs = fetch_logs()
        if logs:
            lines = [line for line in logs.split("\n") if line.strip()]
            if lines:
                last_line = lines[-1]
                log_data = parse_log_line(last_line)
                if log_data:
                    analyze_log(log_data)
                else:
                    logging.warning(f"Некорректная строка лога: {last_line}")
            else:
                logging.warning("Получены логи, но все строки пусты.")
        else:
            logging.warning("Не удалось получить логи с сервера (None).")

        now = time.time()
        if now - last_data["last_received"] > TIMEOUT:
            if not active_errors["data_delay"]:
                add_notification(f"⌛ Данные не обновлялись более {TIMEOUT} секунд!")
                active_errors["data_delay"] = True
        else:
            if active_errors["data_delay"]:
                add_notification("✅ Данные снова обновляются своевременно.")
                active_errors["data_delay"] = False

        time.sleep(CHECK_INTERVAL)

# ------------------ ЗАПУСК СЕРВЕРА ------------------

threading.Thread(target=background_log_checker, daemon=True).start()

if __name__ == '__main__':
    def delayed_test_notification():
    	time.sleep(5)
    	add_notification("🔔 Тест: бот работает и умеет отправлять сообщения")

    threading.Thread(target=delayed_test_notification, daemon=True).start()

    app.run(host='0.0.0.0', port=5000)
