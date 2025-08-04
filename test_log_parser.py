import requests
import logging

def parse_log_line(line):
    parts = line.strip().split(";")
    print("DEBUG parts:", parts, "len:", len(parts))  # Для диагностики
    if len(parts) >= 8:
        try:
            return {
                "timestamp": parts[0].strip(),
                "connectionStatus": parts[1].strip(),
                "balance": float(parts[2].strip()),
                "planNetPositions": float(parts[3].strip()),
                "lastTradeInfo": parts[4].strip(),
                "LongPositions": float(parts[-3].strip()) if parts[-3].strip() else None,
                "ShortPositions": float(parts[-2].strip()) if parts[-2].strip() else None,
                "netPositions": float(parts[-1].strip()) if parts[-1].strip() else None
            }
        except ValueError:
            logging.warning(f"Ошибка преобразования типов в строке лога: {line}")
            return None
    else:
        logging.warning(f"Неверный формат строки лога: {line}")
        return None

if __name__ == "__main__":
    url = 'http://176.58.60.25:5001/logs'
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        log_text = response.text
        lines = [l for l in log_text.strip().split('\n') if l.strip()]
        if not lines:
            print("Лог пустой!")
        else:
            last_line = lines[-1]
            print("Последняя строка лога:", last_line)
            parsed = parse_log_line(last_line)
            print("Результат парсинга:", parsed)
    except Exception as e:
        print(f"Ошибка при получении или обработке лога: {e}")
