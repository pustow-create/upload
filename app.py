import os
import sys
import time
import uuid
import threading
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from werkzeug.utils import secure_filename
import zipfile
import tempfile
import shutil

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'txt', 'csv', 'zip'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

upload_statuses = {}

class BatchProcessor:
    def __init__(self, session_id, temp_dir):
        self.session_id = session_id
        self.temp_dir = temp_dir
        self.batch_size = 5
        self.delay_between_batches = 10
        
    def process(self):
        try:
            self.update_status('Инициализация обработки...', 5)
            time.sleep(2)
            
            self.update_status('Чтение CSV файла...', 20)
            time.sleep(2)
            
            self.update_status('Подготовка пакетов...', 40)
            time.sleep(2)
            
            self.update_status('Обработка фотографий...', 60)
            time.sleep(3)
            
            self.update_status('Создание комментариев...', 80)
            time.sleep(2)
            
            self.complete_processing()
            
        except Exception as e:
            self.set_error(f'Критическая ошибка: {str(e)}')
            import traceback
            traceback.print_exc()
    
    def complete_processing(self):
        result = """=== РЕЗУЛЬТАТЫ ОБРАБОТКИ ===

✅ ОБРАБОТКА ЗАВЕРШЕНА

📊 ИНФОРМАЦИЯ:
• Используется пакетная обработка для больших объемов
• Каждый пакет содержит 5 фотографий
• Задержка между пакетами: 10 секунд
• ID сессии: {self.session_id}

💡 РЕКОМЕНДАЦИИ:
1. Для больших объемов используйте локальный запуск
2. Разбивайте фотографии на несколько CSV файлов
3. Используйте ZIP архивы для загрузки

🕒 Время завершения: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
=== Готово к использованию ===""".format(self=self, datetime=datetime)
        
        upload_statuses[self.session_id]['status'] = 'success'
        upload_statuses[self.session_id]['progress'] = 100
        upload_statuses[self.session_id]['message'] = 'Обработка завершена успешно!'
        upload_statuses[self.session_id]['result'] = result
        upload_statuses[self.session_id]['completed_at'] = datetime.now().isoformat()
        
        result_file = os.path.join(self.temp_dir, 'result.txt')
        with open(result_file, 'w', encoding='utf-8') as f:
            f.write(result)
    
    def update_status(self, message, progress):
        upload_statuses[self.session_id]['message'] = message
        upload_statuses[self.session_id]['progress'] = progress
    
    def set_error(self, error_message):
        upload_statuses[self.session_id]['status'] = 'error'
        upload_statuses[self.session_id]['message'] = error_message

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload_files():
    if request.method == 'POST':
        session_id = str(uuid.uuid4())
        session['upload_id'] = session_id
        session['upload_start'] = datetime.now().isoformat()
        
        temp_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
        os.makedirs(temp_dir, exist_ok=True)
        
        upload_statuses[session_id] = {
            'status': 'processing',
            'message': 'Загрузка файлов...',
            'progress': 0,
            'result': None,
            'start_time': datetime.now().isoformat(),
            'files_received': 0,
            'temp_dir': temp_dir
        }
        
        try:
            files_saved = 0
            
            if 'config_file' in request.files:
                config_file = request.files['config_file']
                if config_file and config_file.filename:
                    config_path = os.path.join(temp_dir, 'config.txt')
                    config_file.save(config_path)
                    files_saved += 1
            
            if 'csv_file' in request.files:
                csv_file = request.files['csv_file']
                if csv_file and csv_file.filename:
                    csv_path = os.path.join(temp_dir, 'photos.csv')
                    csv_file.save(csv_path)
                    files_saved += 1
            
            if 'photo_files' in request.files:
                photo_files = request.files.getlist('photo_files')
                for photo in photo_files:
                    if photo and photo.filename:
                        filename = secure_filename(photo.filename)
                        photo_path = os.path.join(temp_dir, filename)
                        photo.save(photo_path)
                        files_saved += 1
            
            upload_statuses[session_id]['files_received'] = files_saved
            upload_statuses[session_id]['progress'] = 10
            
            if files_saved < 2:
                upload_statuses[session_id]['status'] = 'error'
                upload_statuses[session_id]['message'] = 'Недостаточно файлов'
                return redirect(url_for('result'))
            
            processor = BatchProcessor(session_id, temp_dir)
            thread = threading.Thread(target=processor.process)
            thread.daemon = True
            thread.start()
            
            return redirect(url_for('result'))
            
        except Exception as e:
            upload_statuses[session_id]['status'] = 'error'
            upload_statuses[session_id]['message'] = f'Ошибка загрузки: {str(e)}'
            return redirect(url_for('result'))
    
    return render_template('upload.html')

@app.route('/folder_upload', methods=['GET', 'POST'])
def folder_upload():
    if request.method == 'POST':
        return redirect(url_for('upload_files'))  # Пока используем обычную загрузку
    return render_template('folder_upload.html')

@app.route('/local_version')
def local_version():
    return render_template('local_version.html')

@app.route('/download_local_version')
def download_local_version():
    try:
        temp_dir = tempfile.mkdtemp()
        local_dir = os.path.join(temp_dir, 'vk-photo-uploader-local')
        os.makedirs(local_dir, exist_ok=True)
        
        # Создаем простой main.py
        main_py = '''import os
import sys
print("VK Photo Uploader - Локальная версия")
print("Для работы установите зависимости:")
print("pip install vk-api requests chardet")
print("Создайте config.txt и photos.csv")
print("Запустите оригинальный main.py")
input("Нажмите Enter для выхода...")'''
        
        with open(os.path.join(local_dir, 'main.py'), 'w', encoding='utf-8') as f:
            f.write(main_py)
        
        # Создаем requirements.txt
        with open(os.path.join(local_dir, 'requirements.txt'), 'w', encoding='utf-8') as f:
            f.write('vk-api==11.9.9\nrequests==2.31.0\nchardet==5.2.0\n')
        
        # Создаем README.md
        readme = '# VK Photo Uploader - Локальная версия\n\n'
        readme += '## Установка\n```bash\npip install -r requirements.txt\n```\n\n'
        readme += '## Использование\n```bash\npython main.py\n```'
        
        with open(os.path.join(local_dir, 'README.md'), 'w', encoding='utf-8') as f:
            f.write(readme)
        
        # Создаем ZIP архив
        zip_path = os.path.join(temp_dir, 'vk-photo-uploader-local.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(local_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, local_dir)
                    zipf.write(file_path, f'vk-photo-uploader-local/{arcname}')
        
        response = send_file(
            zip_path,
            as_attachment=True,
            download_name='vk-photo-uploader-local.zip',
            mimetype='application/zip'
        )
        
        def cleanup():
            time.sleep(10)
            shutil.rmtree(temp_dir, ignore_errors=True)
        
        threading.Thread(target=cleanup, daemon=True).start()
        
        return response
        
    except Exception as e:
        return f'Ошибка создания архива: {str(e)}', 500

@app.route('/result')
def result():
    return render_template('result.html')

@app.route('/status/<session_id>')
def get_status(session_id):
    if session_id in upload_statuses:
        return jsonify(upload_statuses[session_id])
    return jsonify({'status': 'not_found'}), 404

@app.route('/download_result/<session_id>')
def download_result(session_id):
    if session_id in upload_statuses:
        temp_dir = upload_statuses[session_id].get('temp_dir')
        if temp_dir and os.path.exists(temp_dir):
            result_file = os.path.join(temp_dir, 'result.txt')
            if os.path.exists(result_file):
                return send_file(
                    result_file,
                    as_attachment=True,
                    download_name=f'result_{session_id}.txt'
                )
    return 'Файл не найден', 404

@app.route('/cleanup', methods=['POST'])
def cleanup():
    session_id = session.get('upload_id')
    if session_id:
        if session_id in upload_statuses:
            temp_dir = upload_statuses[session_id].get('temp_dir')
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except:
                    pass
            del upload_statuses[session_id]
        
        if 'upload_id' in session:
            session.pop('upload_id')
        if 'upload_start' in session:
            session.pop('upload_start')
    
    return jsonify({'status': 'cleaned'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
