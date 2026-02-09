import os
import time
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import hashlib

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1MB max upload (только для текстовых файлов)

# Локальное хранилище (в памяти, без сохранения на диск)
file_storage = {}

class FileValidator:
    """Валидатор файлов без сохранения на диск"""
    
    @staticmethod
    def validate_config(content):
        """Проверка config.txt"""
        required_fields = ['access_token', 'album_id']
        lines = content.strip().split('\n')
        
        config = {}
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip()
        
        missing = [field for field in required_fields if field not in config]
        return len(missing) == 0, missing
    
    @staticmethod
    def validate_csv(content):
        """Проверка photos.csv"""
        lines = content.strip().split('\n')
        
        if len(lines) < 2:
            return False, "CSV файл должен содержать данные"
        
        # Проверяем формат
        for line in lines[1:]:  # Пропускаем заголовок
            line = line.strip()
            if line:
                if '|' not in line:
                    return False, "Неверный формат CSV. Используйте разделитель |"
        
        return True, None
    
    @staticmethod
    def generate_instructions(config_content, csv_content):
        """Генерация инструкций для локального выполнения"""
        lines = csv_content.strip().split('\n')
        photo_count = max(0, len(lines) - 1)  # Минус заголовок
        
        config_lines = config_content.strip().split('\n')
        config_dict = {}
        for line in config_lines:
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' in line:
                    key, value = line.split('=', 1)
                    config_dict[key.strip()] = value.strip()
        
        instructions = {
            'photo_count': photo_count,
            'config': config_dict,
            'steps': [
                "Скопируйте приведенные ниже файлы на ваш компьютер",
                "Создайте папку и поместите туда все фотографии",
                "Создайте файлы config.txt и photos.csv с содержимым ниже",
                "Скачайте локальный скрипт main.py с нашего сайта",
                "Установите зависимости: pip install vk-api requests chardet",
                "Запустите: python main.py"
            ]
        }
        
        return instructions

@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html')

@app.route('/prepare', methods=['GET', 'POST'])
def prepare_files():
    """Страница подготовки файлов (без загрузки фото)"""
    if request.method == 'POST':
        session_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
        session['session_id'] = session_id
        
        try:
            # Получаем содержимое файлов из формы
            config_content = request.form.get('config_content', '').strip()
            csv_content = request.form.get('csv_content', '').strip()
            
            if not config_content:
                return render_template('prepare.html', 
                                     error="Введите содержимое config.txt")
            
            if not csv_content:
                return render_template('prepare.html', 
                                     error="Введите содержимое photos.csv")
            
            # Валидируем config.txt
            config_valid, config_missing = FileValidator.validate_config(config_content)
            if not config_valid:
                missing_str = ", ".join(config_missing)
                return render_template('prepare.html', 
                                     error=f"В config.txt отсутствуют обязательные поля: {missing_str}")
            
            # Валидируем photos.csv
            csv_valid, csv_error = FileValidator.validate_csv(csv_content)
            if not csv_valid:
                return render_template('prepare.html', 
                                     error=f"Ошибка в photos.csv: {csv_error}")
            
            # Сохраняем в памяти (без записи на диск)
            file_storage[session_id] = {
                'config_content': config_content,
                'csv_content': csv_content,
                'created_at': datetime.now().isoformat(),
                'status': 'prepared'
            }
            
            return redirect(url_for('process_files'))
            
        except Exception as e:
            return render_template('prepare.html', 
                                 error=f"Ошибка обработки: {str(e)}")
    
    return render_template('prepare.html')

@app.route('/process')
def process_files():
    """Страница обработки"""
    session_id = session.get('session_id')
    if not session_id or session_id not in file_storage:
        return redirect(url_for('prepare_files'))
    
    # Генерируем инструкции
    data = file_storage[session_id]
    instructions = FileValidator.generate_instructions(
        data['config_content'], 
        data['csv_content']
    )
    
    return render_template('process.html', 
                         instructions=instructions,
                         config_content=data['config_content'],
                         csv_content=data['csv_content'],
                         session_id=session_id)

@app.route('/generate_local_script')
def generate_local_script():
    """Генерация локального скрипта для скачивания"""
    session_id = session.get('session_id')
    if not session_id or session_id not in file_storage:
        return redirect(url_for('prepare_files'))
    
    # Содержимое локального скрипта
    local_script = '''#!/usr/bin/env python3
# VK Photo Uploader - Локальная версия
# Сгенерировано автоматически

import vk_api
import os
import sys
import time
import chardet
from vk_api.upload import VkUpload
from vk_api.exceptions import VkApiError
from pathlib import Path

def load_config(config_file="config.txt"):
    """Загрузка конфигурации"""
    config = {}
    with open(config_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip()
    
    required = ['access_token', 'album_id']
    for field in required:
        if field not in config:
            print(f"Ошибка: {field} не указан в config.txt")
            sys.exit(1)
    
    return config

def read_csv_data(csv_file="photos.csv"):
    """Чтение CSV файла"""
    photos_data = []
    
    try:
        # Определяем кодировку
        with open(csv_file, 'rb') as f:
            raw_data = f.read()
            result = chardet.detect(raw_data)
            encoding = result['encoding'] if result['encoding'] else 'utf-8'
        
        with open(csv_file, 'r', encoding=encoding) as f:
            lines = f.readlines()
        
        # Пропускаем заголовки
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            if 'sep=' in line.lower():
                continue
            if 'файл изображения' in line.lower() or 'file image' in line.lower():
                continue
            
            # Парсим строку
            if '|' in line:
                parts = line.split('|', 2)
                main_photo = parts[0].strip().strip('"\'')
                
                if not main_photo:
                    continue
                
                description = parts[1].strip().strip('"\'') if len(parts) > 1 else ''
                comment_files_str = parts[2].strip().strip('"\'') if len(parts) > 2 else ''
                
                # Парсим файлы для комментариев
                comment_files = []
                if comment_files_str:
                    for separator in ('; ', ';', ', ', ','):
                        if separator in comment_files_str:
                            comment_files = [f.strip().strip('"\'') for f in comment_files_str.split(separator)]
                            break
                    else:
                        comment_files = [comment_files_str]
                
                photos_data.append({
                    'main_photo': main_photo,
                    'description': description,
                    'comment_files': [f for f in comment_files if f],
                    'row_num': i + 1
                })
    
    except Exception as e:
        print(f"Ошибка чтения CSV: {e}")
    
    return photos_data

def upload_photo_to_album(upload, filename, description, album_id, group_id=None):
    """Загрузка фото в альбом"""
    if not os.path.exists(filename):
        print(f"Файл не найден: {filename}")
        return None
    
    try:
        photo = upload.photo(
            [filename],
            album_id=album_id,
            group_id=group_id,
            description=description
        )[0]
        
        print(f"✓ Загружено: {filename}")
        return photo
    
    except VkApiError as e:
        print(f"✗ Ошибка загрузки {filename}: {e}")
        return None
    except Exception as e:
        print(f"✗ Ошибка {filename}: {e}")
        return None

def main():
    print("=" * 60)
    print("VK Photo Uploader - Локальная версия")
    print("=" * 60)
    
    # Проверяем файлы
    required_files = ['config.txt', 'photos.csv']
    for file in required_files:
        if not os.path.exists(file):
            print(f"Файл {file} не найден!")
            print("Поместите его в текущую папку")
            return
    
    # Загружаем конфигурацию
    print("\\nЗагружаю конфигурацию...")
    config = load_config()
    
    # Аутентификация
    print("Аутентификация в ВКонтакте...")
    try:
        session = vk_api.VkApi(token=config['access_token'])
        vk = session.get_api()
        upload = VkUpload(session)
        print("✓ Аутентификация успешна")
    except Exception as e:
        print(f"✗ Ошибка аутентификации: {e}")
        return
    
    # Читаем данные
    print("\\nЧитаю данные из photos.csv...")
    photos_data = read_csv_data()
    
    if not photos_data:
        print("Нет данных для обработки")
        return
    
    print(f"Найдено {len(photos_data)} записей")
    
    # Параметры
    group_id = config.get('group_id', '').replace('-', '')
    album_id = config['album_id']
    batch_size = 5  # Фото в пакете
    delay = 10      # Секунд между пакетами
    
    # Разбиваем на пакеты
    batches = [photos_data[i:i + batch_size] 
              for i in range(0, len(photos_data), batch_size)]
    
    successful = 0
    failed = 0
    
    print(f"\\nБудет обработано {len(batches)} пакетов")
    print(f"Размер пакета: {batch_size} фото")
    print(f"Задержка между пакетами: {delay} сек\\n")
    
    # Обработка пакетов
    for batch_num, batch in enumerate(batches, 1):
        print(f"{'='*40}")
        print(f"ПАКЕТ {batch_num}/{len(batches)}")
        print(f"{'='*40}")
        
        for item in batch:
            print(f"Обработка: {item['main_photo']}")
            
            result = upload_photo_to_album(
                upload, 
                item['main_photo'], 
                item['description'], 
                album_id, 
                group_id if group_id else None
            )
            
            if result:
                successful += 1
                
                # Обработка комментариев
                if item['comment_files']:
                    print(f"  Файлов для комментариев: {len(item['comment_files'])}")
            else:
                failed += 1
        
        # Задержка между пакетами
        if batch_num < len(batches):
            print(f"\\n⏳ Ожидание {delay} секунд...")
            time.sleep(delay)
    
    # Итог
    print(f"\\n{'='*60}")
    print("ОБРАБОТКА ЗАВЕРШЕНА!")
    print(f"{'='*60}")
    print(f"✅ Успешно: {successful}")
    print(f"❌ С ошибками: {failed}")
    print(f"📊 Всего: {len(photos_data)}")
    
    # Сохраняем отчет
    report = f"""Отчет об обработке
Дата: {time.strftime('%Y-%m-%d %H:%M:%S')}
Успешно: {successful}
С ошибками: {failed}
Всего: {len(photos_data)}"""
    
    with open('processing_report.txt', 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"📄 Отчет сохранен в processing_report.txt")
    print(f"\\nНажмите Enter для выхода...")
    input()

if __name__ == "__main__":
    main()
'''
    
    # Возвращаем скрипт как текст
    response = app.response_class(
        response=local_script,
        status=200,
        mimetype='text/plain',
        headers={'Content-Disposition': 'attachment; filename=main.py'}
    )
    
    return response

@app.route('/result')
def result():
    """Страница с результатами"""
    session_id = session.get('session_id')
    if session_id in file_storage:
        data = file_storage[session_id]
        
        # Подсчитываем количество фото
        lines = data['csv_content'].strip().split('\n')
        photo_count = max(0, len(lines) - 1)
        
        return render_template('result.html', 
                             photo_count=photo_count,
                             session_id=session_id)
    
    return redirect(url_for('index'))

@app.route('/cleanup')
def cleanup():
    """Очистка сессии"""
    session_id = session.get('session_id')
    if session_id and session_id in file_storage:
        del file_storage[session_id]
    
    if 'session_id' in session:
        session.pop('session_id')
    
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
