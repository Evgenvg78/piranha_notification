from flask import Flask, Response
import os

app = Flask(__name__)

# Параметры
LOG_DIR = r"C:\QUIK_VTB\monitor\logs"  # Папка с логами
LOG_EXTENSION = ".log"  # Расширение лог-файлов

def get_latest_log_file():
    """Находит последний лог-файл в папке"""
    files = [f for f in os.listdir(LOG_DIR) if f.endswith(LOG_EXTENSION)]
    files.sort(reverse=True)  # Сортируем по имени (по дате)
    if files:
        return os.path.join(LOG_DIR, files[0])
    return None
    
    
@app.route('/ping')
def ping():
    return "pong", 200


@app.route('/logs')
def get_logs():
    """Отдает содержимое последнего лог-файла"""
    log_file_path = get_latest_log_file()
    
    if log_file_path and os.path.exists(log_file_path):
        with open(log_file_path, 'r') as f:
            log_content = f.read()
        # Возвращаем содержимое лог-файла в виде текста
        return Response(log_content, mimetype='text/plain')
    else:
        return "Log file not found!", 404  # Если лог-файл не найден, отправляем ошибку

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5001)  # Запуск Flask-сервера на порту 5001
