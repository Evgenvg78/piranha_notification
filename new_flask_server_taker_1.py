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

# --- ДОБАВЛЕНО: Telegram polling с командой /status ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes


import sys
print('PYTHON:', sys.executable)
import pytz
print('PYTZ:', pytz.__file__)

# ------------------ НАСТРОЙКИ ------------------
# Загружаем переменные из .env
load_dotenv()

# Конфигурация серверов
SERVERS = [
    {
        "name": "Artem",
        "LOG_SERVER_URL": 'http://194.58.71.175:5001/logs',
        "PING_URL": 'http://194.58.71.175:5001/ping',
        "PLAN_POSITIONS_THRESHOLD": 200_000
    },
    {
        "name": "Udin",
        "LOG_SERVER_URL": 'http://176.58.60.25:5001/logs',
        "PING_URL": 'http://176.58.60.25:5001/ping',
        "PLAN_POSITIONS_THRESHOLD": 200_000
    }
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEOUT = 120           # Максимальное время (сек) без новых данных
TIME_BEFORE_PING = 60   # Если данные не поступают в течение 60 сек – пингуем сервер
MAX_PING_ATTEMPTS = 2   # Число попыток ping
CHECK_INTERVAL = 60     # Интервал проверки логов (сек)

# ------------------ ЛОГИРОВАНИЕ ------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ------------------ ИНИЦИАЛИЗАЦИЯ ------------------

app = Flask(__name__)
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ------------------ ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ------------------

# Для каждого сервера храним отдельные состояния
server_states = {}
for server in SERVERS:
    server_states[server["name"]] = {
        "last_data": {
            "timestamp": None,
            "connectionStatus": None,
            "balance": None,
            "planNetPositions": None,
            "lastTradeTime": None,
            "last_received": 0
        },
        "active_errors": {
            "server_down": False,
            "connection": False,
            "balance": False,
            "plan_positions_crit": False,
            "data_delay": False,
            "trade_no_data": False,
            "trade_stuck": False,
            "trade_delay": False
        }
    }

# ------------------ МЕХАНИЗМ АГРЕГАЦИИ УВЕДОМЛЕНИЙ ------------------

pending_notifications = []
pending_lock = Lock()

def add_notification(server_name: str, message: str):
    """Добавляет уведомление в очередь с префиксом сервера."""
    full_message = f"[{server_name}] {message}"
    with pending_lock:
        if full_message not in pending_notifications:
            pending_notifications.append(full_message)
            logging.info(f"Уведомление добавлено в очередь: {full_message}")

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

def fetch_logs(server_cfg):
    """
    Запрашивает логи с удалённого сервера.
    Возвращает текст лога или None при ошибке.
    """
    try:
        response = requests.get(server_cfg["LOG_SERVER_URL"], timeout=10)
        if response.status_code == 200:
            logging.info(f"[{server_cfg['name']}] Логи успешно получены!")
            return response.text
        else:
            logging.error(f"[{server_cfg['name']}] Ошибка запроса логов: {response.status_code}")
            return None
    except requests.RequestException as e:
        logging.error(f"[{server_cfg['name']}] Ошибка получения логов: {e}")
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
                "lastTradeInfo": parts[4].strip(),
                "LongPositions": float(parts[8].strip()),
                "ShortPositions": float(parts[9].strip()),
                "netPositions": float(parts[10].strip())  # ожидается чистые позиции в рублях
            }
        except ValueError:
            logging.warning(f"Ошибка преобразования типов в строке лога: {line}")
            return None
    else:
        logging.warning(f"Неверный формат строки лога: {line}")
        return None

def ping_quik_server(server_cfg):
    """
    Пингует сервер Quik. Выполняется MAX_PING_ATTEMPTS раз.
    Возвращает True, если хотя бы один ping успешен.
    """
    for attempt in range(MAX_PING_ATTEMPTS):
        try:
            response = requests.get(server_cfg["PING_URL"], timeout=5)
            if response.status_code == 200:
                return True
        except requests.RequestException as e:
            logging.warning(f"[{server_cfg['name']}] Попытка пинга #{attempt + 1} не удалась: {e}")
        time.sleep(1)
    return False

def analyze_log(data: dict, server_cfg, state):
    """
    Анализирует полученные данные лога для конкретного сервера.
    """
    last_data = state["last_data"]
    active_errors = state["active_errors"]
    server_name = server_cfg["name"]

    # Обновляем общие показатели
    last_data["timestamp"] = data["timestamp"]
    last_data["connectionStatus"] = data["connectionStatus"]
    last_data["balance"] = data["balance"]
    last_data["planNetPositions"] = data["planNetPositions"]
    last_data["last_received"] = time.time()

    # --- 1. Проверка доступности сервера (ping) ---
    current_time = time.time()
    if current_time - last_data["last_received"] > TIME_BEFORE_PING:
        if not ping_quik_server(server_cfg):
            if not active_errors["server_down"]:
                add_notification(server_name, "❌ Сервер недоступен (ping не прошёл)!")
                active_errors["server_down"] = True
            return
        else:
            if active_errors["server_down"]:
                add_notification(server_name, "✅ Сервер снова доступен (ping успешен).")
                active_errors["server_down"] = False

    # --- 2. Проверка подключения Quik ---
    if data["connectionStatus"].lower() == "false":
        if not active_errors["connection"]:
            add_notification(server_name, "❌ Quik не подключен!")
            active_errors["connection"] = True
    else:
        if active_errors["connection"]:
            add_notification(server_name, "✅ Подключение Quik восстановлено.")
            active_errors["connection"] = False

    # --- 3. Проверка баланса (снижение более чем на 1%) ---
    old_balance = last_data.get("balance")
    if old_balance is not None:
        if data["balance"] < old_balance * 0.99:
            if not active_errors["balance"]:
                add_notification(server_name, f"❗ Баланс снизился более чем на 1%! Был: {old_balance}, стал: {data['balance']}")
                active_errors["balance"] = True
        else:
            if active_errors["balance"]:
                add_notification(server_name, f"✅ Баланс восстановился. Текущее значение: {data['balance']}")
                active_errors["balance"] = False

    # --- 4. Проверка критичного уровня плановых чистых позиций ---
    plan_val = data["planNetPositions"]
    if plan_val < server_cfg["PLAN_POSITIONS_THRESHOLD"]:
        if not active_errors["plan_positions_crit"]:
            add_notification(server_name, f"❌ Свободное ГО на критическом уровне = {plan_val}")
            active_errors["plan_positions_crit"] = True
    else:
        if active_errors["plan_positions_crit"]:
            add_notification(server_name, f"✅ Свободное ГО восстановлено. Текущее значение = {plan_val}")
            active_errors["plan_positions_crit"] = False

    # --- 5. Проверка данных по сделкам ---
    trade_time_str = data["lastTradeInfo"].strip()
    if trade_time_str.lower() in ["нет данных", "no data", "данные отсутствуют"]:
        if not active_errors["trade_no_data"]:
            add_notification(server_name, "📉 Нет данных о сделках в Quik!")
            active_errors["trade_no_data"] = True
        return
    else:
        if active_errors["trade_no_data"]:
            add_notification(server_name, "✅ Данные о сделках снова доступны.")
            active_errors["trade_no_data"] = False

    try:
        trade_time = datetime.strptime(trade_time_str, "%H:%M:%S")
    except ValueError:
        logging.error(f"[{server_name}] Ошибка преобразования времени сделки: {trade_time_str}")
        return

    if last_data.get("lastTradeTime"):
        prev_trade_time = last_data["lastTradeTime"]

        # Если время сделки не изменилось – данные не обновляются
        if trade_time == prev_trade_time:
            if not active_errors.get("trade_stuck"):
                add_notification(server_name, f"⛔ Данные о сделках не обновляются! Время сделки осталось {trade_time_str}.")
                active_errors["trade_stuck"] = True
        else:
            if active_errors.get("trade_stuck"):
                add_notification(server_name, "✅ Сделки снова обновляются корректно.")
                active_errors["trade_stuck"] = False

            time_diff = (trade_time - prev_trade_time).total_seconds()
            if time_diff > 90:
                if not active_errors.get("trade_delay"):
                    add_notification(server_name, f"⚠️ Задержка обновления сделок! Разница: {int(time_diff)} секунд.")
                    active_errors["trade_delay"] = True
            else:
                if active_errors.get("trade_delay"):
                    add_notification(server_name, "✅ Данные о сделках снова обновляются в нормальном режиме.")
                    active_errors["trade_delay"] = False

    last_data["lastTradeTime"] = trade_time

# Пример: [(start_hour, start_minute, end_hour, end_minute)]
PAUSE_SCHEDULE = [
    (0, 0, 9, 0),     # 00:00 - 09:00
    (13, 59, 14, 5),  # 13:59 - 14:05
    (18, 49, 19, 5),  # 18:49 - 19:05
    (23, 49, 0, 0),   # 23:49 - 00:00 (через полночь)
]

def is_pause_time():
    now = datetime.now()
    now_minutes = now.hour * 60 + now.minute
    for start_h, start_m, end_h, end_m in PAUSE_SCHEDULE:
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        if start_minutes <= end_minutes:
            if start_minutes <= now_minutes < end_minutes:
                return True
        else:  # Интервал через полночь
            if now_minutes >= start_minutes or now_minutes < end_minutes:
                return True
    return False

def background_log_checker(server_cfg, state):
    """
    Фоновая задача для конкретного сервера:
      - Каждые CHECK_INTERVAL секунд запрашивает логи.
      - Извлекает последнюю непустую строку, парсит её и анализирует данные.
      - Проверяет, не прошло ли более TIMEOUT секунд без обновления данных.
    """
    server_name = server_cfg["name"]
    while True:
        if is_pause_time():
            logging.info(f"[{server_name}] Мониторинг приостановлен по расписанию.")
            time.sleep(CHECK_INTERVAL)
            continue
        logs = fetch_logs(server_cfg)
        if logs:
            lines = [line for line in logs.split("\n") if line.strip()]
            if lines:
                last_line = lines[-1]
                log_data = parse_log_line(last_line)
                if log_data:
                    analyze_log(log_data, server_cfg, state)
                else:
                    logging.warning(f"[{server_name}] Некорректная строка лога: {last_line}")
            else:
                logging.warning(f"[{server_name}] Получены логи, но все строки пусты.")
        else:
            logging.warning(f"[{server_name}] Не удалось получить логи с сервера (None).")

        now = time.time()
        if now - state["last_data"]["last_received"] > TIMEOUT:
            if not state["active_errors"]["data_delay"]:
                add_notification(server_name, f"⌛ Данные не обновлялись более {TIMEOUT} секунд!")
                state["active_errors"]["data_delay"] = True
        else:
            if state["active_errors"]["data_delay"]:
                add_notification(server_name, "✅ Данные снова обновляются своевременно.")
                state["active_errors"]["data_delay"] = False

        time.sleep(CHECK_INTERVAL)

def check_and_notify_trading_start():
    """Проверяет состояние серверов в 9:01 и отправляет уведомление, если всё в норме."""
    while True:
        now = datetime.now()
        # Следующая цель — сегодня в 9:01:00, либо завтра, если уже позже
        target = now.replace(hour=9, minute=1, second=0, microsecond=0)
        if now >= target:
            # Если уже позже 9:01, ждём до следующего дня
            target = target.replace(day=now.day + 1)
        time_to_wait = (target - now).total_seconds()
        if time_to_wait > 0:
            time.sleep(time_to_wait)
        # Проверяем состояние каждого сервера
        for server in SERVERS:
            state = server_states[server["name"]]
            last_data = state["last_data"]
            active_errors = state["active_errors"]
            # Данные должны быть свежими (например, не старше 2*CHECK_INTERVAL)
            data_fresh = (time.time() - last_data["last_received"]) < 2 * CHECK_INTERVAL
            no_errors = not any(active_errors.values())
            if data_fresh and no_errors:
                add_notification(server["name"], "Торги запущены➡️, все показатели в норме 😉")
            else:
                logging.info(f"[{server['name']}] Не все показатели в норме или нет свежих данных для утреннего уведомления.")
        # Ждём сутки до следующей проверки
        time.sleep(24 * 60 * 60)

# --- Функция для тестового уведомления ---
def delayed_test_notification():
    time.sleep(5)
    for server in SERVERS:
        add_notification(server["name"], f"🔔 Тест: бот работает и умеет отправлять сообщения для {server['name']}")

# ------------------ ЗАПУСК СЕРВЕРА ------------------

# Запускаем обработчик логов для каждого сервера
for server in SERVERS:
    threading.Thread(
        target=background_log_checker,
        args=(server, server_states[server["name"]]),
        daemon=True
    ).start()

# --- ДОБАВЛЕНО: Telegram polling с командой /status для python-telegram-bot 22.x ---
# async-версия функции формирования статуса (можно оставить sync, если не делает await)
def get_server_status(server_name):
    server_cfg = next((s for s in SERVERS if s["name"] == server_name), None)
    if not server_cfg:
        return f"Сервер {server_name} не найден."
    state = server_states[server_name]
    last_data = state["last_data"]
    ping_ok = ping_quik_server(server_cfg)
    ping_status = "✅ Сервер доступен" if ping_ok else "❌ Сервер недоступен (ping)"
    quik_status = last_data["connectionStatus"]
    quik_status_str = "✅ QUIK подключен" if str(quik_status).lower() == "true" else "❌ QUIK не подключен"
    balance = last_data.get("balance")
    planNetPositions = last_data.get("planNetPositions")
    long_pos = last_data.get("LongPositions")
    short_pos = last_data.get("ShortPositions")
    net_pos = last_data.get("netPositions")
    msg = (
        f"<b>Статус сервера: {server_name}</b>\n"
        f"{ping_status}\n"
        f"{quik_status_str}\n"
        f"Баланс: <b>{balance}</b>\n"
        f"Свободное ГО: <b>{planNetPositions}</b>\n"
        f"Лонги: <b>{long_pos}</b>\n"
        f"Шорты: <b>{short_pos}</b>\n"
        f"Нетто позиция: <b>{net_pos}</b>\n"
    )
    return msg

# async-обработчик команды /status
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(s["name"], callback_data=f'status_{s["name"]}')]
                for s in SERVERS]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "От какого сервера хотите получить данные?",
        reply_markup=reply_markup
    )

# async-обработчик кнопки
async def status_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith('status_'):
        server_name = query.data.replace('status_', '')
        msg = get_server_status(server_name)
        await query.edit_message_text(text=msg, parse_mode='HTML')

# Запуск polling-бота (async)
async def start_polling_bot():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(status_button))
    print("Telegram polling bot started!")
    await application.run_polling()

# Запускать polling только если это основной процесс
if __name__ == '__main__':
    threading.Thread(target=delayed_test_notification, daemon=True).start()
    threading.Thread(target=check_and_notify_trading_start, daemon=True).start()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000), daemon=True).start()

    import asyncio
    try:
        asyncio.run(start_polling_bot())
    except RuntimeError as e:
        if "event loop is closed" in str(e) or "Cannot close a running event loop" in str(e):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(start_polling_bot())
        else:
            raise
