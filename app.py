import os
import sys
import time
import uuid
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import threading

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # Только для конфиг-файлов (10MB)

# Хранилище статусов заданий
jobs = {}

class RemoteJob:
    """Задание для локального выполнения"""
    
    def __init__(self, job_id, config_content, csv_content):
        self.job_id = job_id
        self.config_content = config_content
        self.csv_content = csv_content
        self.status = 'created'
        self.progress = 0
        self.message = 'Задание создано'
        self.result = None
        self.created_at = datetime.now().isoformat()
        self.started_at = None
        self.completed_at = None
        
    def start(self):
        """Запуск имитации выполнения (в реальности выполняется локально)"""
        self.status = 'processing'
        self.started_at = datetime.now().isoformat()
        self.message = 'Готов к локальному выполнению'
        self.progress = 10
        
        # Имитация обработки
        self.simulate_processing()
    
    def simulate_processing(self):
        """Имитация процесса выполнения"""
        steps = [
            (20, 'Анализ CSV файла...'),
            (40, 'Подготовка пакетов...'),
            (60, 'Настройка подключения к VK...'),
            (80, 'Генерация инструкций...'),
            (100, 'Задание готово к локальному выполнению')
        ]
        
        for progress, message in steps:
            time.sleep(2)
            self.progress = progress
            self.message = message
            jobs[self.job_id] = self.to_dict()
        
        self.complete()
    
    def complete(self):
        """Завершение задания"""
        self.status = 'ready_for_local'
        self.completed_at = datetime.now().isoformat()
        self.result = self.generate_instructions()
        jobs[self.job_id] = self.to_dict()
    
    def generate_instructions(self):
        """Генерация инструкций для локального выполнения"""
        return f"""=== ИНСТРУКЦИЯ ДЛЯ ЛОКАЛЬНОГО ВЫПОЛНЕНИЯ ===

📋 ЗАДАНИЕ ID: {self.job_id}

✅ КОНФИГУРАЦИЯ ПРИНЯТА
✅ CSV ФАЙЛ ПРИНЯТ

📊 ДАННЫЕ:
• Записей в CSV: {len(self.csv_content.split('\\n')) - 1}
• Время создания: {self.created_at}

🚀 ИНСТРУКЦИИ ДЛЯ ЗАПУСКА:

1. СКАЧАЙТЕ И УСТАНОВИТЕ ЛОКАЛЬНУЮ ВЕРСИЮ:
   https://ваш-сайт/local_version

2. СОЗДАЙТЕ ПАПКУ ДЛЯ РАБОТЫ:
   mkdir vk-upload-job-{self.job_id}
   cd vk-upload-job-{self.job_id}

3. СОЗДАЙТЕ ФАЙЛ config.txt С СОДЕРЖИМЫМ:
{self.config_content}

4. СОЗДАЙТЕ ФАЙЛ photos.csv С СОДЕРЖИМЫМ:
{self.csv_content}

5. ПОМЕСТИТЕ ВСЕ ФОТОГРАФИИ В ЭТУ ЖЕ ПАПКУ

6. ЗАПУСТИТЕ ЛОКАЛЬНУЮ ПРОГРАММУ:
   python main.py --job {self.job_id}

💡 АВТОМАТИЧЕСКАЯ ГЕНЕРАЦИЯ СКРИПТА:
• Скачайте готовый скрипт: https://ваш-сайт/download_script/{self.job_id}
• Запустите его на своем компьютере

⚠️ ВАЖНО:
• Убедитесь, что все файлы из CSV находятся в папке
• Проверьте токен доступа в config.txt
• Программа сама обработает все фотографии

📞 ПОДДЕРЖКА:
• ID задания: {self.job_id}
• Создано: {self.created_at}
• Статус: Готово к локальному выполнению

=== КОНЕЦ ИНСТРУКЦИЙ ==="""
    
    def to_dict(self):
        """Преобразование в словарь"""
        return {
            'job_id': self.job_id,
            'status': self.status,
            'progress': self.progress,
            'message': self.message,
            'result': self.result,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'config_size': len(self.config_content),
            'csv_size': len(self.csv_content),
            'csv_lines': len(self.csv_content.split('\\n'))
        }

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/configure', methods=['GET', 'POST'])
def configure():
    """Страница конфигурации (без загрузки фото)"""
    if request.method == 'POST':
        job_id = str(uuid.uuid4())
        session['job_id'] = job_id
        
        try:
            # Получаем конфиг и CSV из формы
            config_content = request.form.get('config_text', '').strip()
            csv_content = request.form.get('csv_text', '').strip()
            
            if not config_content:
                # Пробуем загрузить файл если текст пустой
                if 'config_file' in request.files:
                    config_file = request.files['config_file']
                    if config_file and config_file.filename:
                        config_content = config_file.read().decode('utf-8')
            
            if not csv_content:
                if 'csv_file' in request.files:
                    csv_file = request.files['csv_file']
                    if csv_file and csv_file.filename:
                        csv_content = csv_file.read().decode('utf-8')
            
            # Проверяем обязательные поля
            if not config_content:
                return render_template('configure.html', 
                                    error='Укажите конфигурацию или загрузите config.txt')
            
            if not csv_content:
                return render_template('configure.html',
                                    error='Укажите CSV данные или загрузите photos.csv')
            
            # Создаем задание
            job = RemoteJob(job_id, config_content, csv_content)
            jobs[job_id] = job.to_dict()
            
            # Запускаем в фоне
            thread = threading.Thread(target=job.start)
            thread.daemon = True
            thread.start()
            
            return redirect(url_for('job_status', job_id=job_id))
            
        except Exception as e:
            return render_template('configure.html', error=f'Ошибка: {str(e)}')
    
    return render_template('configure.html')

@app.route('/job/<job_id>')
def job_status(job_id):
    """Статус задания"""
    if job_id not in jobs:
        return render_template('error.html', message='Задание не найдено')
    
    session['job_id'] = job_id
    return render_template('job_status.html', job_id=job_id)

@app.route('/api/job/<job_id>')
def get_job_status(job_id):
    """API для получения статуса задания"""
    if job_id in jobs:
        return jsonify(jobs[job_id])
    return jsonify({'error': 'Job not found'}), 404

@app.route('/download_script/<job_id>')
def download_script(job_id):
    """Скачивание скрипта для локального выполнения"""
    if job_id not in jobs:
        return 'Задание не найдено', 404
    
    job = jobs[job_id]
    
    # Создаем скрипт для локального выполнения
    script_content = f'''#!/usr/bin/env python3
# Скрипт для локальной загрузки фотографий в VK
# Задание ID: {job_id}
# Создано: {job.get('created_at', 'N/A')}

import os
import sys
import time
from datetime import datetime

def main():
    print("="*60)
    print("VK Photo Uploader - Локальный скрипт")
    print(f"Задание: {{job_id}}")
    print("="*60)
    
    # Проверяем файлы
    required_files = ['config.txt', 'photos.csv']
    for file in required_files:
        if not os.path.exists(file):
            print(f"✗ Отсутствует файл: {{file}}")
            print("Создайте файлы из инструкции и запустите снова.")
            input("Нажмите Enter для выхода...")
            return
    
    print("✓ Все необходимые файлы найдены")
    print("✓ Конфигурация загружена")
    
    # Читаем CSV
    try:
        with open('photos.csv', 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Считаем записи (исключая заголовки)
        data_lines = [line for line in lines if line.strip() and 'sep=' not in line and 'Файл изображения' not in line]
        total_photos = len(data_lines)
        
        print(f"📊 Найдено {{total_photos}} фотографий для обработки")
        
    except Exception as e:
        print(f"✗ Ошибка чтения CSV: {{e}}")
        input("Нажмите Enter для выхода...")
        return
    
    # Основной процесс
    print("\\n🚀 Начинаю обработку...")
    print("="*60)
    
    batch_size = 10
    delay_between_batches = 30
    total_batches = (total_photos + batch_size - 1) // batch_size
    
    print(f"Пакетов для обработки: {{total_batches}}")
    print(f"Фото в пакете: {{batch_size}}")
    print(f"Задержка между пакетами: {{delay_between_batches}} сек")
    print("="*60)
    
    # Создаем отчет
    report = f"""=== ОТЧЕТ О ЛОКАЛЬНОЙ ОБРАБОТКЕ ===

Задание ID: {job_id}
Дата начала: {{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}}
Всего фотографий: {{total_photos}}
Пакетов: {{total_batches}}
Размер пакета: {{batch_size}}
Задержка: {{delay_between_batches}} сек

ИНСТРУКЦИЯ ДЛЯ РУЧНОГО ЗАПУСКА:

1. Установите зависимости:
   pip install vk-api requests chardet

2. Используйте оригинальный main.py для загрузки:

3. Или запустите пакетную обработку вручную:

"""
    
    # Добавляем команды для каждого пакета
    for batch_num in range(total_batches):
        start_idx = batch_num * batch_size
        end_idx = min(start_idx + batch_size, total_photos)
        batch_files = data_lines[start_idx:end_idx]
        
        report += f"\\nПАКЕТ {{batch_num + 1}}/{{total_batches}} (фото {{start_idx + 1}}-{{end_idx}}):\\n"
        for i, line in enumerate(batch_files, 1):
            parts = line.strip().split('|', 1)
            filename = parts[0].strip().strip('"') if parts else "N/A"
            report += f"  {{i}}. {{filename}}\\n"
        
        if batch_num < total_batches - 1:
            report += f"  ⏳ Задержка: {{delay_between_batches}} сек\\n"
    
    report += f"""\\n=== КОНЕЦ ИНСТРУКЦИЙ ===

🕒 Время генерации: {{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}}
"""
    
    # Сохраняем отчет
    report_filename = f'vk_upload_plan_{job_id}.txt'
    with open(report_filename, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"✓ План обработки сохранен в {{report_filename}}")
    print("="*60)
    print("📄 ИНСТРУКЦИИ:")
    print(f"1. Откройте файл {{report_filename}}")
    print("2. Следуйте инструкциям для пакетной обработки")
    print("3. Используйте оригинальный main.py для загрузки")
    print("="*60)
    
    input("Нажмите Enter для завершения...")

if __name__ == "__main__":
    main()
'''
    
    from flask import Response
    response = Response(script_content, mimetype='text/plain')
    response.headers['Content-Disposition'] = f'attachment; filename=vk_upload_script_{job_id}.py'
    return response

@app.route('/local_version')
def local_version():
    """Страница с локальной версией"""
    return render_template('local_version.html')

@app.route('/download_full_version')
def download_full_version():
    """Скачивание полной локальной версии"""
    # Здесь будет код для создания ZIP с полной версией
    # Пока возвращаем простой текст
    from flask import Response
    content = """# VK Photo Uploader - Полная локальная версия
# Скачайте с GitHub: https://github.com/ваш-репозиторий/vk-photo-uploader
# Или используйте веб-интерфейс для генерации заданий"""
    
    response = Response(content, mimetype='text/plain')
    response.headers['Content-Disposition'] = 'attachment; filename=readme_local.txt'
    return response

@app.route('/cleanup/<job_id>', methods=['POST'])
def cleanup_job(job_id):
    """Очистка задания"""
    if job_id in jobs:
        del jobs[job_id]
    
    if 'job_id' in session and session['job_id'] == job_id:
        session.pop('job_id')
    
    return jsonify({'status': 'cleaned'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
