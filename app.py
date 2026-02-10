import os
import io
import csv
import json
import requests
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from flask_cors import CORS
import tempfile
from werkzeug.utils import secure_filename
import time
import atexit
import threading

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-123")
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB max
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False  # Для Render бесплатного

# Разрешаем все источники для локальной разработки
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# Конфигурация VK API
VK_API_VERSION = "5.199"
VK_ALBUM_ID = None
VK_ACCESS_TOKEN = None
VK_GROUP_ID = None

# Хранилище сессий в памяти (для Render)
sessions = {}
session_lock = threading.Lock()

def get_session(session_id):
    """Получение сессии из памяти"""
    with session_lock:
        return sessions.get(session_id, {})

def set_session(session_id, data):
    """Сохранение сессии в памяти"""
    with session_lock:
        sessions[session_id] = data

def delete_session(session_id):
    """Удаление сессии"""
    with session_lock:
        if session_id in sessions:
            del sessions[session_id]

# Очистка старых сессий каждые 30 минут
def cleanup_sessions():
    """Очистка устаревших сессий"""
    current_time = time.time()
    to_delete = []
    
    with session_lock:
        for session_id, session_data in sessions.items():
            if current_time - session_data.get('_timestamp', 0) > 1800:  # 30 минут
                to_delete.append(session_id)
        
        for session_id in to_delete:
            del sessions[session_id]
    
    # Повторяем каждые 30 минут
    timer = threading.Timer(1800, cleanup_sessions)
    timer.daemon = True
    timer.start()

# Запускаем очистку сессий
cleanup_sessions()

def load_config_from_file(config_content):
    """Парсинг конфигурации из текста config.txt"""
    config = {}
    if isinstance(config_content, bytes):
        content = config_content.decode('utf-8', errors='ignore')
    else:
        content = config_content
    
    lines = content.strip().split('\n')
    for line in lines:
        line = line.strip()
        if line and '=' in line:
            key, value = line.split('=', 1)
            config[key.strip()] = value.strip()
    return config

def parse_csv_content(csv_content, delimiter='|'):
    """Парсинг CSV с определением разделителя"""
    # Декодируем содержимое
    if isinstance(csv_content, bytes):
        content = csv_content.decode('utf-8-sig', errors='ignore')
    else:
        content = csv_content
    
    # Удаляем лишние пробелы и разбиваем на строки
    lines = [line.strip() for line in content.strip().split('\n') if line.strip()]
    
    # Пропускаем строку sep=|
    if lines and lines[0].startswith('sep='):
        delimiter = lines[0].split('=')[1].strip()
        lines = lines[1:]
    
    # Парсим CSV
    csv_data = []
    for line in lines:
        if line.strip():
            # Разбиваем строку с учетом экранирования
            parts = []
            current_part = []
            in_quotes = False
            
            for char in line:
                if char == '"':
                    in_quotes = not in_quotes
                elif char == delimiter and not in_quotes:
                    parts.append(''.join(current_part).strip())
                    current_part = []
                else:
                    current_part.append(char)
            
            # Добавляем последнюю часть
            if current_part:
                parts.append(''.join(current_part).strip())
            
            # Очищаем кавычки
            parts = [part.strip('"') for part in parts]
            
            if len(parts) >= 2:
                # Обрабатываем фото в комментариях
                comment_photos = []
                if len(parts) > 2 and parts[2]:
                    # Разделяем по точке с запятой
                    raw_photos = [p.strip() for p in parts[2].split(';')]
                    comment_photos = [p for p in raw_photos if p]
                
                csv_data.append({
                    'main_photo': parts[0],
                    'description': parts[1] if len(parts) > 1 else '',
                    'comment_photos': comment_photos
                })
    
    return csv_data

class VKUploader:
    def __init__(self, access_token, group_id=None):
        self.access_token = access_token
        self.group_id = group_id
        self.api_url = "https://api.vk.com/method/"
    
    def _call_api(self, method, params):
        """Вызов VK API"""
        params.update({
            'access_token': self.access_token,
            'v': VK_API_VERSION
        })
        
        try:
            response = requests.post(f"{self.api_url}{method}", data=params, timeout=30)
            response.raise_for_status()
            result = response.json()
            
            if 'error' in result:
                error_msg = result['error'].get('error_msg', 'Unknown VK API error')
                raise Exception(f"VK API Error: {error_msg}")
            
            return result['response']
        except requests.exceptions.Timeout:
            raise Exception("VK API timeout")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network error: {str(e)}")
    
    def get_album_upload_server(self, album_id):
        """Получить сервер для загрузки фото в альбом"""
        params = {
            'album_id': album_id
        }
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        
        return self._call_api('photos.getUploadServer', params)
    
    def get_wall_upload_server(self):
        """Получить сервер для загрузки фото на стену (для комментариев)"""
        params = {}
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        
        return self._call_api('photos.getWallUploadServer', params)
    
    def save_album_photo(self, server, photos_list, hash_value, album_id):
        """Сохранить фото в альбоме после загрузки"""
        params = {
            'server': server,
            'photos_list': photos_list,
            'hash': hash_value,
            'album_id': album_id
        }
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        
        return self._call_api('photos.save', params)
    
    def save_wall_photo(self, server, photo, hash_value):
        """Сохранить фото для стены (комментариев)"""
        params = {
            'server': server,
            'photo': photo,
            'hash': hash_value
        }
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        
        return self._call_api('photos.saveWallPhoto', params)
    
    def create_album_comment(self, owner_id, photo_id, message="", attachments=None):
        """Создать комментарий к фото в альбоме"""
        params = {
            'owner_id': owner_id,
            'photo_id': photo_id,
            'message': message or ""
        }
        
        if attachments:
            params['attachments'] = ','.join(attachments)
        
        return self._call_api('photos.createComment', params)

def process_upload_stream(file_storage):
    """Обработка файла в потоковом режиме без сохранения на диск"""
    # Используем временный файл в памяти
    temp_file = tempfile.SpooledTemporaryFile(max_size=5*1024*1024)  # 5MB в памяти
    
    # Копируем поток
    chunk_size = 8192
    file_storage.seek(0)
    
    while True:
        chunk = file_storage.read(chunk_size)
        if not chunk:
            break
        temp_file.write(chunk)
    
    temp_file.seek(0)
    return temp_file

@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    """Сервирование статических файлов"""
    return send_from_directory('static', filename)

@app.after_request
def add_cors_headers(response):
    """Добавление CORS заголовков ко всем ответам"""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route('/api/check-files', methods=['POST', 'OPTIONS'])
def check_files():
    """Проверка наличия необходимых файлов"""
    if request.method == 'OPTIONS':
        return '', 200
    
    try:
        # Получаем список файлов из папки
        uploaded_files = request.files.getlist('files')
        
        # Ищем config.txt и .csv файлы
        config_file = None
        csv_file = None
        photo_files = {}
        
        for file_storage in uploaded_files:
            filename = secure_filename(file_storage.filename).lower()
            
            if filename == 'config.txt':
                config_file = file_storage
            elif filename.endswith('.csv'):
                csv_file = file_storage
            elif filename.endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
                photo_files[file_storage.filename] = file_storage
        
        # Проверяем наличие обязательных файлов
        errors = []
        
        if not config_file:
            errors.append('Не найден файл config.txt')
        
        if not csv_file:
            errors.append('Не найден CSV файл')
        
        if errors:
            return jsonify({
                'success': False,
                'errors': errors
            }), 400
        
        return jsonify({
            'success': True,
            'config_found': True,
            'csv_found': True,
            'photo_count': len(photo_files),
            'message': f'Найдено {len(photo_files)} фотографий'
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/start-upload', methods=['POST', 'OPTIONS'])
def start_upload():
    """Начало процесса загрузки"""
    if request.method == 'OPTIONS':
        return '', 200
    
    try:
        # Генерируем ID сессии
        session_id = request.headers.get('X-Session-ID') or str(int(time.time() * 1000))
        
        # Получаем все файлы
        uploaded_files = request.files.getlist('files')
        
        if not uploaded_files:
            return jsonify({'success': False, 'error': 'Нет загруженных файлов'}), 400
        
        # Разделяем файлы
        config_content = None
        csv_content = None
        files_dict = {}
        
        for file_storage in uploaded_files:
            filename = secure_filename(file_storage.filename)
            
            if filename.lower() == 'config.txt':
                config_content = file_storage.read()
            elif filename.lower().endswith('.csv'):
                csv_content = file_storage.read()
            else:
                # Сохраняем только имя и размер
                files_dict[filename] = {
                    'name': filename,
                    'size': len(file_storage.read()),
                    'storage': file_storage  # временно храним объект
                }
                file_storage.seek(0)
        
        if not config_content:
            return jsonify({'success': False, 'error': 'Не найден config.txt'}), 400
        
        if not csv_content:
            return jsonify({'success': False, 'error': 'Не найден CSV файл'}), 400
        
        # Загружаем конфиг
        config = load_config_from_file(config_content)
        
        global VK_ACCESS_TOKEN, VK_GROUP_ID, VK_ALBUM_ID
        
        VK_ACCESS_TOKEN = config.get('ACCESS_TOKEN')
        VK_GROUP_ID = config.get('GROUP_ID')
        VK_ALBUM_ID = config.get('ALBUM_ID')
        
        if not VK_ACCESS_TOKEN:
            return jsonify({'success': False, 'error': 'В config.txt не указан ACCESS_TOKEN'}), 400
        
        # Парсим CSV
        csv_data = parse_csv_content(csv_content)
        
        # Сохраняем данные в сессии
        session_data = {
            'csv_data': csv_data,
            'files_dict': {k: v for k, v in files_dict.items()},  # сохраняем только метаданные
            'current_row': 0,
            'uploaded_main_photos': {},
            'uploaded_comment_photos': {},
            'config': config,
            '_timestamp': time.time()
        }
        
        set_session(session_id, session_data)
        
        # Проверяем наличие всех необходимых файлов
        missing_files = []
        for row in csv_data:
            if row['main_photo'] not in files_dict:
                missing_files.append(row['main_photo'])
            
            for comment_photo in row['comment_photos']:
                if comment_photo not in files_dict:
                    missing_files.append(comment_photo)
        
        if missing_files:
            return jsonify({
                'success': False,
                'error': f'Не найдены файлы: {", ".join(set(missing_files[:5]))}',
                'missing_count': len(set(missing_files))
            }), 400
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'total_rows': len(csv_data),
            'total_files': len(files_dict),
            'message': f'Готово к загрузке: {len(csv_data)} записей, {len(files_dict)} файлов'
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/upload-row', methods=['POST', 'OPTIONS'])
def upload_row():
    """Обработка одной строки CSV"""
    if request.method == 'OPTIONS':
        return '', 200
    
    try:
        row_index = request.form.get('row_index', type=int)
        session_id = request.form.get('session_id')
        
        if row_index is None:
            return jsonify({'success': False, 'error': 'Не указан row_index'}), 400
        
        if not session_id:
            return jsonify({'success': False, 'error': 'Не указан session_id'}), 400
        
        # Получаем данные из сессии
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 400
        
        csv_data = session_data.get('csv_data', [])
        if row_index >= len(csv_data):
            return jsonify({'success': False, 'error': 'Неверный индекс строки'}), 400
        
        row = csv_data[row_index]
        
        # Получаем файлы из запроса
        uploaded_files = request.files.getlist('files')
        files_dict = {f.filename: f for f in uploaded_files}
        
        # Получаем конфиг из сессии
        config = session_data.get('config', {})
        access_token = config.get('ACCESS_TOKEN')
        group_id = config.get('GROUP_ID')
        album_id = config.get('ALBUM_ID')
        
        if not access_token:
            return jsonify({'success': False, 'error': 'Отсутствует ACCESS_TOKEN'}), 400
        
        # Инициализируем VKUploader
        uploader = VKUploader(access_token, group_id)
        
        results = {
            'success': True,
            'row_index': row_index,
            'main_photo': None,
            'comment_groups': []
        }
        
        # 1. Загружаем основное фото в альбом
        main_photo_name = row['main_photo']
        if main_photo_name in files_dict:
            print(f"Загрузка основного фото: {main_photo_name}")
            
            try:
                # Получаем сервер для загрузки в альбом
                upload_server_info = uploader.get_album_upload_server(album_id)
                
                # Загружаем файл на сервер VK
                file_storage = files_dict[main_photo_name]
                temp_file = process_upload_stream(file_storage)
                
                files = {'file1': (main_photo_name, temp_file, 'image/jpeg')}
                upload_response = requests.post(upload_server_info['upload_url'], files=files, timeout=60)
                upload_response.raise_for_status()
                upload_result = upload_response.json()
                
                # Сохраняем фото в альбоме
                save_result = uploader.save_album_photo(
                    upload_result['server'],
                    upload_result['photos_list'],
                    upload_result['hash'],
                    album_id
                )
                
                if save_result and len(save_result) > 0:
                    photo_info = save_result[0]
                    owner_id = photo_info['owner_id']
                    photo_id = photo_info['id']
                    
                    results['main_photo'] = {
                        'owner_id': owner_id,
                        'photo_id': photo_id,
                        'name': main_photo_name
                    }
                    
                    print(f"Основное фото загружено: photo{owner_id}_{photo_id}")
                    
                    # Обновляем сессию
                    if 'uploaded_main_photos' not in session_data:
                        session_data['uploaded_main_photos'] = {}
                    session_data['uploaded_main_photos'][str(row_index)] = results['main_photo']
                    set_session(session_id, session_data)
                
                temp_file.close()
                
            except Exception as e:
                print(f"Ошибка при загрузке основного фото: {str(e)}")
                results['main_photo_error'] = str(e)
        
        # 2. Загружаем фото для комментариев (группами по 2)
        comment_photos = row['comment_photos']
        if comment_photos:
            # Разбиваем на группы по 2
            groups = []
            for i in range(0, len(comment_photos), 2):
                group = comment_photos[i:i+2]
                groups.append(group)
            
            # Загружаем каждую группу
            for group_index, group in enumerate(groups):
                group_photos = []
                
                for photo_name in group:
                    if photo_name in files_dict:
                        print(f"Загрузка фото для комментария: {photo_name}")
                        
                        try:
                            # Получаем сервер для загрузки на стену
                            upload_server_info = uploader.get_wall_upload_server()
                            
                            # Загружаем файл
                            file_storage = files_dict[photo_name]
                            temp_file = process_upload_stream(file_storage)
                            
                            files = {'photo': (photo_name, temp_file, 'image/jpeg')}
                            upload_response = requests.post(upload_server_info['upload_url'], files=files, timeout=60)
                            upload_response.raise_for_status()
                            upload_result = upload_response.json()
                            
                            # Сохраняем фото для стены
                            save_result = uploader.save_wall_photo(
                                upload_result['server'],
                                upload_result['photo'],
                                upload_result['hash']
                            )
                            
                            if save_result and len(save_result) > 0:
                                photo_info = save_result[0]
                                group_photos.append({
                                    'owner_id': photo_info['owner_id'],
                                    'photo_id': photo_info['id'],
                                    'name': photo_name
                                })
                            
                            temp_file.close()
                            
                        except Exception as e:
                            print(f"Ошибка при загрузке фото для комментария: {str(e)}")
                            continue
                
                if group_photos:
                    results['comment_groups'].append({
                        'group_id': group_index,
                        'photos': group_photos
                    })
        
        return jsonify(results)
    
    except Exception as e:
        print(f"Ошибка при загрузке строки {row_index}: {str(e)}")
        return jsonify({'success': False, 'error': str(e), 'row_index': row_index}), 500

@app.route('/api/create-comments', methods=['POST', 'OPTIONS'])
def create_comments():
    """Создание комментариев для загруженных фото"""
    if request.method == 'OPTIONS':
        return '', 200
    
    try:
        row_index = request.form.get('row_index', type=int)
        session_id = request.form.get('session_id')
        comment_groups_data = request.form.get('comment_groups', '[]')
        
        if row_index is None:
            return jsonify({'success': False, 'error': 'Не указан row_index'}), 400
        
        if not session_id:
            return jsonify({'success': False, 'error': 'Не указан session_id'}), 400
        
        # Получаем данные из сессии
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 400
        
        csv_data = session_data.get('csv_data', [])
        uploaded_main_photos = session_data.get('uploaded_main_photos', {})
        
        if row_index >= len(csv_data):
            return jsonify({'success': False, 'error': 'Неверный индекс строки'}), 400
        
        main_photo_info = uploaded_main_photos.get(str(row_index))
        
        if not main_photo_info:
            return jsonify({'success': False, 'error': 'Основное фото не загружено'}), 400
        
        # Получаем конфиг из сессии
        config = session_data.get('config', {})
        access_token = config.get('ACCESS_TOKEN')
        group_id = config.get('GROUP_ID')
        
        if not access_token:
            return jsonify({'success': False, 'error': 'Отсутствует ACCESS_TOKEN'}), 400
        
        # Инициализируем VKUploader
        uploader = VKUploader(access_token, group_id)
        
        # Парсим группы комментариев
        comment_groups = json.loads(comment_groups_data)
        created_comments = []
        
        # Создаем комментарии для каждой группы
        for group in comment_groups:
            photos = group.get('photos', [])
            if photos:
                # Формируем attachments для комментария
                attachments = []
                for photo in photos:
                    owner_id = photo.get('owner_id')
                    photo_id = photo.get('photo_id')
                    if owner_id and photo_id:
                        attachments.append(f"photo{owner_id}_{photo_id}")
                
                if attachments:
                    # Создаем комментарий
                    try:
                        comment_result = uploader.create_album_comment(
                            owner_id=main_photo_info['owner_id'],
                            photo_id=main_photo_info['photo_id'],
                            message="",
                            attachments=attachments
                        )
                        
                        created_comments.append({
                            'comment_id': comment_result.get('comment_id'),
                            'photos_count': len(attachments)
                        })
                        
                        print(f"Создан комментарий с {len(attachments)} фото")
                        
                        # Пауза между комментариями чтобы не превысить лимиты VK
                        time.sleep(1)
                    
                    except Exception as e:
                        print(f"Ошибка при создании комментария: {str(e)}")
                        continue
        
        return jsonify({
            'success': True,
            'row_index': row_index,
            'main_photo': f"photo{main_photo_info['owner_id']}_{main_photo_info['photo_id']}",
            'comments_created': len(created_comments),
            'message': f'Создано {len(created_comments)} комментариев'
        })
    
    except Exception as e:
        print(f"Ошибка при создании комментариев: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get-progress', methods=['GET', 'OPTIONS'])
def get_progress():
    """Получение текущего прогресса"""
    if request.method == 'OPTIONS':
        return '', 200
    
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'success': False, 'error': 'Не указан session_id'}), 400
    
    session_data = get_session(session_id)
    if not session_data:
        return jsonify({'success': False, 'error': 'Сессия не найдена'}), 400
    
    current_row = session_data.get('current_row', 0)
    total_rows = len(session_data.get('csv_data', []))
    
    return jsonify({
        'success': True,
        'current_row': current_row,
        'total_rows': total_rows,
        'progress': (current_row / total_rows * 100) if total_rows > 0 else 0
    })

@app.route('/api/reset', methods=['POST', 'OPTIONS'])
def reset():
    """Сброс сессии"""
    if request.method == 'OPTIONS':
        return '', 200
    
    session_id = request.form.get('session_id')
    if session_id:
        delete_session(session_id)
    
    return jsonify({'success': True, 'message': 'Сессия сброшена'})

@app.route('/api/health', methods=['GET'])
def health_check():
    """Проверка работоспособности API"""
    return jsonify({
        'status': 'ok',
        'timestamp': time.time(),
        'service': 'vk-photo-uploader'
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
