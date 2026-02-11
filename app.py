import os
import csv
import json
import requests
import time
import tempfile
import threading
from datetime import timedelta
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from werkzeug.utils import secure_filename

# ==================== НАСТРОЙКА ПРИЛОЖЕНИЯ ====================
app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'local-dev-secret-key')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)

# ==================== СВОЯ РЕАЛИЗАЦИЯ CORS ====================
@app.after_request
def add_cors_headers(response):
    """Добавление CORS заголовков ко всем ответам"""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Session-ID'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    """Обработка OPTIONS запросов для CORS"""
    return '', 200

# ==================== ВЕРСИЯ VK API ====================
VK_API_VERSION = "5.199"

# ==================== ХРАНЕНИЕ СЕССИЙ ====================
sessions = {}
session_lock = threading.Lock()

def get_session(session_id):
    """Получение сессии"""
    with session_lock:
        return sessions.get(session_id, {})

def set_session(session_id, data):
    """Сохранение сессии"""
    with session_lock:
        sessions[session_id] = data
        sessions[session_id]['_timestamp'] = time.time()

def delete_session(session_id):
    """Удаление сессии"""
    with session_lock:
        if session_id in sessions:
            del sessions[session_id]

# ==================== ФУНКЦИИ ЗАГРУЗКИ КОНФИГА ====================
def load_config_from_file(config_content):
    """
    Парсинг конфигурации из текста config.txt
    Поддерживает комментарии и пустые строки
    """
    config = {}
    
    if isinstance(config_content, bytes):
        content = config_content.decode('utf-8', errors='ignore')
    else:
        content = config_content
    
    lines = content.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        
        # Пропускаем пустые строки и комментарии
        if not line or line.startswith('#'):
            continue
        
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip().upper()
            value = value.strip()
            
            if value:
                config[key] = value
    
    return config

# ==================== ПАРСИНГ CSV ====================
def parse_csv_content(csv_content):
    """Парсинг CSV файла с разделителем |"""
    if isinstance(csv_content, bytes):
        content = csv_content.decode('utf-8-sig', errors='ignore')
    else:
        content = csv_content
    
    lines = [line.strip() for line in content.strip().split('\n') if line.strip()]
    
    # Определяем разделитель
    delimiter = '|'
    if lines and lines[0].startswith('sep='):
        delimiter = lines[0].split('=')[1].strip()
        lines = lines[1:]
    
    csv_data = []
    for line in lines:
        if line.strip():
            parts = [part.strip().strip('"') for part in line.split(delimiter)]
            
            if len(parts) >= 2:
                main_photo = parts[0]
                description = parts[1] if len(parts) > 1 else ''
                
                comment_photos = []
                if len(parts) > 2 and parts[2]:
                    raw_photos = [p.strip() for p in parts[2].split(';')]
                    comment_photos = [p for p in raw_photos if p]
                
                csv_data.append({
                    'main_photo': main_photo,
                    'description': description,
                    'comment_photos': comment_photos,
                    'row_index': len(csv_data)
                })
    
    return csv_data

# ==================== АНАЛИЗ ФАЙЛОВ ====================
def analyze_files(csv_data, uploaded_files):
    """Анализ файлов: какие нужны, какие есть, какие лишние"""
    required_files = set()
    for row in csv_data:
        if row['main_photo']:
            required_files.add(row['main_photo'])
        for photo in row['comment_photos']:
            if photo:
                required_files.add(photo)
    
    uploaded_file_names = set(uploaded_files.keys())
    
    missing_files = required_files - uploaded_file_names
    extra_files = uploaded_file_names - required_files
    
    return {
        'required_files': list(required_files),
        'uploaded_files': list(uploaded_file_names),
        'missing_files': list(missing_files),
        'extra_files': list(extra_files),
        'all_required_present': len(missing_files) == 0,
        'required_count': len(required_files),
        'uploaded_count': len(uploaded_file_names),
        'missing_count': len(missing_files),
        'extra_count': len(extra_files)
    }

# ==================== РАЗБИВКА НА ГРУППЫ ====================
def split_into_groups(photos, group_size=2):
    """Разделение фото на группы для комментариев"""
    groups = []
    for i in range(0, len(photos), group_size):
        groups.append(photos[i:i + group_size])
    return groups

# ==================== КЛАСС ДЛЯ РАБОТЫ С VK API ====================
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
                error_msg = result['error'].get('error_msg', 'Unknown error')
                error_code = result['error'].get('error_code', '')
                raise Exception(f"VK API Error {error_code}: {error_msg}")
            
            return result['response']
        except Exception as e:
            raise Exception(f"VK API Error: {str(e)}")
    
    def get_album_upload_server(self, album_id):
        """Получить адрес для загрузки фото в альбом"""
        params = {'album_id': album_id}
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        return self._call_api('photos.getUploadServer', params)
    
    def get_wall_upload_server(self):
        """Получить адрес для загрузки фото на стену"""
        params = {}
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        return self._call_api('photos.getWallUploadServer', params)
    
    def save_album_photo(self, server, photos_list, hash_value, album_id):
        """Сохранить загруженное фото в альбом"""
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
    
    def create_album_comment(self, owner_id, photo_id, attachments=None, message=""):
        """Создать комментарий к фото в альбоме"""
        params = {
            'owner_id': owner_id,
            'photo_id': photo_id,
            'message': message
        }
        
        if attachments:
            params['attachments'] = ','.join(attachments)
        
        return self._call_api('photos.createComment', params)

# ==================== ОБРАБОТКА ФАЙЛОВ ====================
def process_upload_stream(file_storage):
    """Обработка файла в потоковом режиме"""
    # Создаем новый временный файл
    temp_file = tempfile.SpooledTemporaryFile(max_size=10*1024*1024)
    file_storage.seek(0)
    
    chunk_size = 8192
    while True:
        chunk = file_storage.read(chunk_size)
        if not chunk:
            break
        temp_file.write(chunk)
    
    temp_file.seek(0)
    return temp_file

# ==================== ОСНОВНЫЕ МАРШРУТЫ ====================
@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    """Сервирование статических файлов"""
    return send_from_directory('static', filename)

@app.route('/api/health', methods=['GET'])
def health():
    """Проверка работоспособности API"""
    return jsonify({
        'status': 'ok',
        'service': 'vk-photo-uploader',
        'timestamp': time.time(),
        'version': '1.0'
    })

# ==================== ИНИЦИАЛИЗАЦИЯ ЗАГРУЗКИ ====================
@app.route('/api/init', methods=['POST'])
def init_upload():
    """Инициализация загрузки - анализ файлов"""
    try:
        uploaded_files = {}
        config_content = None
        csv_content = None
        
        for file_storage in request.files.getlist('files'):
            filename = secure_filename(file_storage.filename)
            
            if filename.lower() == 'config.txt':
                config_content = file_storage.read()
            elif filename.lower().endswith('.csv'):
                csv_content = file_storage.read()
            else:
                # Сохраняем файл в словарь
                file_storage.seek(0)
                uploaded_files[filename] = {
                    'storage': file_storage,
                    'name': filename,
                    'size': len(file_storage.read()) if hasattr(file_storage, 'read') else 0
                }
                file_storage.seek(0)
        
        if not config_content:
            return jsonify({'success': False, 'error': 'Не найден файл config.txt'}), 400
        
        if not csv_content:
            return jsonify({'success': False, 'error': 'Не найден CSV файл'}), 400
        
        config = load_config_from_file(config_content)
        
        required_keys = ['ACCESS_TOKEN', 'ALBUM_ID']
        missing_keys = [key for key in required_keys if not config.get(key)]
        
        if missing_keys:
            return jsonify({
                'success': False,
                'error': f'В config.txt отсутствуют ключи: {", ".join(missing_keys)}'
            }), 400
        
        csv_data = parse_csv_content(csv_content)
        
        if not csv_data:
            return jsonify({'success': False, 'error': 'CSV файл пуст'}), 400
        
        file_analysis = analyze_files(csv_data, uploaded_files)
        
        session_id = str(int(time.time() * 1000))
        session_data = {
            'config': config,
            'csv_data': csv_data,
            'uploaded_files': uploaded_files,
            'file_analysis': file_analysis,
            'current_row': 0,
            'results': [],
            'start_time': time.time(),
            '_timestamp': time.time()
        }
        
        set_session(session_id, session_data)
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'total_rows': len(csv_data),
            'file_analysis': file_analysis,
            'message': f'Найдено {len(csv_data)} записей для обработки'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': f'Ошибка инициализации: {str(e)}'}), 500

# ==================== ОБРАБОТКА СТРОКИ CSV ====================
@app.route('/api/process-row/<int:row_index>', methods=['POST'])
def process_row(row_index):
    """Обработка одной строки CSV"""
    session_id = None
    
    try:
        session_id = request.headers.get('X-Session-ID') or request.form.get('session_id')
        
        if not session_id:
            return jsonify({'success': False, 'error': 'Не указан session_id'}), 400
        
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена или устарела'}), 404
        
        csv_data = session_data.get('csv_data', [])
        if row_index >= len(csv_data):
            return jsonify({'success': False, 'error': f'Неверный индекс строки: {row_index}'}), 400
        
        config = session_data.get('config', {})
        access_token = config.get('ACCESS_TOKEN')
        group_id = config.get('GROUP_ID', '')
        album_id = config.get('ALBUM_ID')
        
        if not access_token:
            return jsonify({'success': False, 'error': 'Отсутствует ACCESS_TOKEN'}), 400
        
        if not album_id:
            return jsonify({'success': False, 'error': 'Отсутствует ALBUM_ID'}), 400
        
        row = csv_data[row_index]
        uploaded_files = session_data.get('uploaded_files', {})
        
        uploader = VKUploader(access_token, group_id if group_id else None)
        
        result = {
            'row_index': row_index,
            'main_photo': row['main_photo'],
            'description': row['description'],
            'success': False,
            'main_photo_result': None,
            'comment_results': [],
            'errors': []
        }
        
        # 1. Загружаем основное фото
        main_photo_name = row['main_photo']
        
        if main_photo_name and main_photo_name in uploaded_files:
            try:
                # Получаем upload сервер
                upload_server_info = uploader.get_album_upload_server(album_id)
                
                # Загружаем файл
                file_storage = uploaded_files[main_photo_name]['storage']
                file_storage.seek(0)
                
                # Создаем копию файла для загрузки
                temp_file = tempfile.SpooledTemporaryFile(max_size=10*1024*1024)
                file_storage.seek(0)
                temp_file.write(file_storage.read())
                temp_file.seek(0)
                
                files = {'file1': (main_photo_name, temp_file, 'image/jpeg')}
                upload_response = requests.post(
                    upload_server_info['upload_url'], 
                    files=files, 
                    timeout=60
                )
                upload_response.raise_for_status()
                upload_result = upload_response.json()
                temp_file.close()
                
                # Сохраняем фото
                save_result = uploader.save_album_photo(
                    upload_result['server'],
                    upload_result['photos_list'],
                    upload_result['hash'],
                    album_id
                )
                
                if save_result and len(save_result) > 0:
                    photo_info = save_result[0]
                    result['main_photo_result'] = {
                        'photo_id': photo_info['id'],
                        'owner_id': photo_info['owner_id'],
                        'vk_url': f"photo{photo_info['owner_id']}_{photo_info['id']}"
                    }
                    result['success'] = True
                    
                    # 2. Загружаем фото для комментариев
                    comment_photos = row['comment_photos']
                    if comment_photos:
                        photo_groups = split_into_groups(comment_photos, 2)
                        
                        for group_index, group in enumerate(photo_groups):
                            group_result = {
                                'group_index': group_index,
                                'photos': [],
                                'success': False
                            }
                            
                            for photo_name in group:
                                if photo_name and photo_name in uploaded_files:
                                    try:
                                        # Получаем upload сервер для стены
                                        wall_upload_server = uploader.get_wall_upload_server()
                                        
                                        # Загружаем файл
                                        photo_storage = uploaded_files[photo_name]['storage']
                                        photo_storage.seek(0)
                                        
                                        # Создаем копию
                                        temp_photo = tempfile.SpooledTemporaryFile(max_size=10*1024*1024)
                                        photo_storage.seek(0)
                                        temp_photo.write(photo_storage.read())
                                        temp_photo.seek(0)
                                        
                                        files = {'photo': (photo_name, temp_photo, 'image/jpeg')}
                                        wall_response = requests.post(
                                            wall_upload_server['upload_url'],
                                            files=files,
                                            timeout=60
                                        )
                                        wall_response.raise_for_status()
                                        wall_result = wall_response.json()
                                        temp_photo.close()
                                        
                                        # Сохраняем фото для стены
                                        wall_save_result = uploader.save_wall_photo(
                                            wall_result['server'],
                                            wall_result['photo'],
                                            wall_result['hash']
                                        )
                                        
                                        if wall_save_result and len(wall_save_result) > 0:
                                            wall_photo_info = wall_save_result[0]
                                            group_result['photos'].append({
                                                'name': photo_name,
                                                'photo_id': wall_photo_info['id'],
                                                'owner_id': wall_photo_info['owner_id'],
                                                'vk_url': f"photo{wall_photo_info['owner_id']}_{wall_photo_info['id']}"
                                            })
                                        
                                        time.sleep(0.5)
                                        
                                    except Exception as e:
                                        error_msg = f"{photo_name}: {str(e)}"
                                        group_result['errors'] = group_result.get('errors', [])
                                        group_result['errors'].append(error_msg)
                                        result['errors'].append(error_msg)
                            
                            # Создаем комментарий
                            if group_result['photos']:
                                try:
                                    attachments = [
                                        f"photo{photo['owner_id']}_{photo['photo_id']}" 
                                        for photo in group_result['photos']
                                    ]
                                    
                                    comment_result = uploader.create_album_comment(
                                        owner_id=result['main_photo_result']['owner_id'],
                                        photo_id=result['main_photo_result']['photo_id'],
                                        attachments=attachments
                                    )
                                    
                                    group_result['success'] = True
                                    group_result['comment_id'] = comment_result.get('comment_id')
                                    group_result['attachments_count'] = len(attachments)
                                    
                                except Exception as e:
                                    error_msg = f"Ошибка создания комментария: {str(e)}"
                                    group_result['errors'] = group_result.get('errors', [])
                                    group_result['errors'].append(error_msg)
                                    result['errors'].append(error_msg)
                            
                            result['comment_results'].append(group_result)
                
                else:
                    result['errors'].append('Не удалось сохранить основное фото')
                    result['success'] = False
                
            except Exception as e:
                result['errors'].append(f"Ошибка загрузки основного фото: {str(e)}")
                result['success'] = False
        
        else:
            error_msg = f'Файл {main_photo_name} не найден'
            result['errors'].append(error_msg)
            result['success'] = False
        
        # Сохраняем результат
        if 'results' not in session_data:
            session_data['results'] = []
        
        session_data['results'].append(result)
        session_data['current_row'] = row_index + 1
        set_session(session_id, session_data)
        
        time.sleep(1)
        
        return jsonify({
            'success': True,
            'result': result,
            'progress': {
                'current': row_index + 1,
                'total': len(csv_data),
                'percentage': ((row_index + 1) / len(csv_data)) * 100
            }
        })
        
    except Exception as e:
        error_msg = f"Ошибка обработки строки {row_index}: {str(e)}"
        print(error_msg)
        
        # Пытаемся сохранить ошибку в сессии
        if session_id:
            try:
                session_data = get_session(session_id)
                if session_data:
                    if 'errors' not in session_data:
                        session_data['errors'] = []
                    session_data['errors'].append(error_msg)
                    set_session(session_id, session_data)
            except:
                pass
        
        return jsonify({'success': False, 'error': error_msg}), 500

# ==================== ФИНАЛИЗАЦИЯ ====================
@app.route('/api/finalize/<session_id>', methods=['GET'])
def finalize(session_id):
    """Финальный отчет после загрузки"""
    try:
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({
                'success': False,
                'error': 'Сессия не найдена'
            }), 404
        
        csv_data = session_data.get('csv_data', [])
        results = session_data.get('results', [])
        file_analysis = session_data.get('file_analysis', {})
        
        total_rows = len(csv_data)
        processed_rows = len(results)
        successful_rows = sum(1 for r in results if r.get('success', False))
        
        all_errors = []
        for result in results:
            if 'errors' in result and result['errors']:
                all_errors.extend(result['errors'])
        
        report = {
            'session_id': session_id,
            'processing_time': time.time() - session_data.get('start_time', 0),
            'statistics': {
                'total_rows': total_rows,
                'processed_rows': processed_rows,
                'successful_rows': successful_rows,
                'failed_rows': processed_rows - successful_rows,
                'success_rate': (successful_rows / processed_rows * 100) if processed_rows > 0 else 0
            },
            'file_analysis': file_analysis,
            'errors': all_errors[:50],
            'summary': {
                'message': f'Обработано {processed_rows} из {total_rows} строк. Успешно: {successful_rows}.'
            }
        }
        
        # Очищаем сессию после отчета
        delete_session(session_id)
        
        return jsonify({
            'success': True,
            'report': report
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Ошибка формирования отчета: {str(e)}'
        }), 500

# ==================== ПРОГРЕСС ====================
@app.route('/api/progress/<session_id>', methods=['GET'])
def get_progress(session_id):
    """Получение прогресса обработки"""
    try:
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        csv_data = session_data.get('csv_data', [])
        current_row = session_data.get('current_row', 0)
        results = session_data.get('results', [])
        
        total_rows = len(csv_data)
        processed_rows = len(results)
        
        return jsonify({
            'success': True,
            'progress': {
                'current': current_row,
                'processed': processed_rows,
                'total': total_rows,
                'percentage': (current_row / total_rows * 100) if total_rows > 0 else 0
            },
            'status': 'processing' if current_row < total_rows else 'completed'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ОТМЕНА ====================
@app.route('/api/cancel/<session_id>', methods=['POST'])
def cancel_upload(session_id):
    """Отмена загрузки"""
    try:
        delete_session(session_id)
        return jsonify({'success': True, 'message': 'Загрузка отменена'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ТЕСТ VK ====================
@app.route('/api/test-vk', methods=['POST'])
def test_vk_connection():
    """Тест подключения к VK API"""
    try:
        config_content = None
        for file_storage in request.files.getlist('files'):
            if file_storage.filename.lower() == 'config.txt':
                config_content = file_storage.read()
                break
        
        if not config_content:
            return jsonify({'success': False, 'error': 'Не найден config.txt'}), 400
        
        config = load_config_from_file(config_content)
        access_token = config.get('ACCESS_TOKEN')
        
        if not access_token:
            return jsonify({'success': False, 'error': 'Не найден ACCESS_TOKEN'}), 400
        
        params = {
            'access_token': access_token,
            'v': VK_API_VERSION
        }
        
        response = requests.post('https://api.vk.com/method/users.get', data=params, timeout=30)
        result = response.json()
        
        if 'error' in result:
            return jsonify({
                'success': False,
                'error': f'VK API Error: {result["error"]["error_msg"]}'
            }), 400
        
        return jsonify({
            'success': True,
            'message': 'Подключение к VK успешно',
            'user_info': result['response'][0] if result['response'] else {}
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': f'Ошибка теста: {str(e)}'}), 500

# ==================== ЗАПУСК СЕРВЕРА ====================
if __name__ == '__main__':
    os.makedirs('static', exist_ok=True)
    os.makedirs('templates', exist_ok=True)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
