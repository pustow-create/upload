from flask import Flask, render_template, request, jsonify, session, send_file, redirect, url_for
import os
import sys
import tempfile
import shutil
import subprocess
import threading
from pathlib import Path
import uuid
from datetime import datetime
import logging

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max upload
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'txt', 'csv'}

# Создаем папку для загрузок
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Глобальное хранилище статусов
upload_statuses = {}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload_files():
    """Страница загрузки файлов"""
    if request.method == 'POST':
        # Генерируем уникальный ID для этой сессии
        session_id = str(uuid.uuid4())
        session['upload_id'] = session_id
        
        # Создаем временную папку для этой сессии
        temp_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
        os.makedirs(temp_dir, exist_ok=True)
        
        # Инициализируем статус
        upload_statuses[session_id] = {
            'status': 'processing',
            'message': 'Обработка файлов...',
            'progress': 0,
            'result': None,
            'start_time': datetime.now().isoformat(),
            'files_received': 0,
            'files_processed': 0
        }
        
        try:
            # Обрабатываем загруженные файлы
            files_uploaded = 0
            
            # Проверяем конфигурационный файл
            if 'config_file' in request.files:
                config_file = request.files['config_file']
                if config_file and allowed_file(config_file.filename) and config_file.filename.endswith('.txt'):
                    config_path = os.path.join(temp_dir, 'config.txt')
                    config_file.save(config_path)
                    files_uploaded += 1
            
            # Проверяем CSV файл
            if 'csv_file' in request.files:
                csv_file = request.files['csv_file']
                if csv_file and allowed_file(csv_file.filename) and csv_file.filename.endswith('.csv'):
                    csv_path = os.path.join(temp_dir, 'photos.csv')
                    csv_file.save(csv_path)
                    files_uploaded += 1
            
            # Обрабатываем фото
            photo_files = request.files.getlist('photo_files')
            for photo in photo_files:
                if photo and allowed_file(photo.filename):
                    # Проверяем, что это изображение
                    ext = photo.filename.rsplit('.', 1)[1].lower()
                    if ext in {'jpg', 'jpeg', 'png', 'gif', 'bmp'}:
                        photo_path = os.path.join(temp_dir, photo.filename)
                        photo.save(photo_path)
                        files_uploaded += 1
            
            upload_statuses[session_id]['files_received'] = files_uploaded
            
            if files_uploaded == 0:
                upload_statuses[session_id]['status'] = 'error'
                upload_statuses[session_id]['message'] = 'Не загружены файлы'
                return redirect(url_for('result'))
            
            # Запускаем обработку в фоновом потоке
            thread = threading.Thread(
                target=process_upload,
                args=(session_id, temp_dir)
            )
            thread.daemon = True
            thread.start()
            
            return redirect(url_for('result'))
            
        except Exception as e:
            logger.error(f"Error processing upload: {e}")
            upload_statuses[session_id]['status'] = 'error'
            upload_statuses[session_id]['message'] = f'Ошибка: {str(e)}'
            return redirect(url_for('result'))
    
    return render_template('upload.html')

def process_upload(session_id, temp_dir):
    """Обработка загруженных файлов"""
    try:
        status = upload_statuses[session_id]
        status['message'] = 'Проверка файлов...'
        status['progress'] = 10
        
        # Проверяем обязательные файлы
        required_files = ['config.txt', 'photos.csv']
        for file in required_files:
            file_path = os.path.join(temp_dir, file)
            if not os.path.exists(file_path):
                status['status'] = 'error'
                status['message'] = f'Отсутствует обязательный файл: {file}'
                return
        
        status['message'] = 'Запуск обработки...'
        status['progress'] = 20
        
        # Создаем копию main.py в папку с файлами
        main_py_path = os.path.join(temp_dir, 'main.py')
        
        # Получаем путь к оригинальному main.py
        current_dir = os.path.dirname(os.path.abspath(__file__))
        original_main_py = os.path.join(current_dir, 'main.py')
        
        if os.path.exists(original_main_py):
            shutil.copy2(original_main_py, main_py_path)
        else:
            # Если файла нет, создаем базовый скрипт
            create_basic_main_py(main_py_path)
        
        # Изменяем рабочую директорию на temp_dir
        original_cwd = os.getcwd()
        os.chdir(temp_dir)
        
        try:
            status['message'] = 'Выполнение скрипта загрузки...'
            status['progress'] = 30
            
            # Запускаем скрипт с перехватом вывода
            process = subprocess.Popen(
                [sys.executable, 'main.py'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8'
            )
            
            stdout, stderr = process.communicate()
            
            if process.returncode == 0:
                status['status'] = 'success'
                status['message'] = 'Обработка завершена успешно!'
                status['progress'] = 100
                status['result'] = stdout
                
                # Логируем результат
                log_file = os.path.join(temp_dir, 'result.log')
                with open(log_file, 'w', encoding='utf-8') as f:
                    f.write("=== STDOUT ===\n")
                    f.write(stdout)
                    f.write("\n\n=== STDERR ===\n")
                    f.write(stderr)
                
            else:
                status['status'] = 'error'
                status['message'] = f'Ошибка выполнения скрипта (код {process.returncode})'
                status['result'] = f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
                
        finally:
            # Возвращаемся в исходную директорию
            os.chdir(original_cwd)
            
    except Exception as e:
        logger.error(f"Error in process_upload: {e}")
        if session_id in upload_statuses:
            upload_statuses[session_id]['status'] = 'error'
            upload_statuses[session_id]['message'] = f'Ошибка: {str(e)}'

def create_basic_main_py(filepath):
    """Создает базовый main.py если оригинальный не найден"""
    basic_main = '''import vk_api
import os
import sys

def main():
    print("Это тестовая версия скрипта загрузки")
    print("Файлы в директории:", os.listdir('.'))
    
    try:
        with open('config.txt', 'r') as f:
            config_content = f.read()
            print("Конфиг загружен")
    except:
        print("Не удалось прочитать config.txt")
    
    try:
        with open('photos.csv', 'r') as f:
            lines = f.readlines()
            print(f"CSV файл содержит {len(lines)} строк")
    except:
        print("Не удалось прочитать photos.csv")
    
    print("Обработка завершена!")

if __name__ == "__main__":
    main()
'''
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(basic_main)

@app.route('/result')
def result():
    """Страница с результатами обработки"""
    session_id = session.get('upload_id')
    if not session_id or session_id not in upload_statuses:
        return redirect(url_for('index'))
    
    status_info = upload_statuses[session_id]
    return render_template('result.html', status=status_info)

@app.route('/status/<session_id>')
def get_status(session_id):
    """API для получения статуса обработки"""
    if session_id in upload_statuses:
        return jsonify(upload_statuses[session_id])
    return jsonify({'status': 'not_found', 'message': 'Сессия не найдена'}), 404

@app.route('/download/<session_id>')
def download_log(session_id):
    """Скачивание лог-файла"""
    if session_id in upload_statuses:
        log_file = os.path.join(app.config['UPLOAD_FOLDER'], session_id, 'result.log')
        if os.path.exists(log_file):
            return send_file(log_file, as_attachment=True, download_name=f'result_{session_id}.log')
    
    return 'Файл не найден', 404

@app.route('/cleanup', methods=['POST'])
def cleanup():
    """Очистка временных файлов"""
    session_id = session.get('upload_id')
    if session_id:
        temp_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except:
                pass
    
    # Очищаем старые сессии (старше 1 часа)
    for old_id in list(upload_statuses.keys()):
        if old_id in upload_statuses:
            start_time = datetime.fromisoformat(upload_statuses[old_id]['start_time'])
            age = datetime.now() - start_time
            if age.total_seconds() > 3600:  # 1 час
                del upload_statuses[old_id]
                
                temp_dir = os.path.join(app.config['UPLOAD_FOLDER'], old_id)
                if os.path.exists(temp_dir):
                    try:
                        shutil.rmtree(temp_dir)
                    except:
                        pass
    
    return jsonify({'status': 'cleaned'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)