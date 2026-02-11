import os
import csv
import json
import requests
import time
import tempfile
import threading
import io
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

# ==================== CORS ====================
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Session-ID'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    return '', 200

# ==================== КОНСТАНТЫ ====================
VK_API_VERSION = "5.199"
sessions = {}
session_lock = threading.Lock()

# ==================== РАБОТА С СЕССИЯМИ ====================
def get_session(session_id):
    with session_lock:
        return sessions.get(session_id, {})

def set_session(session_id, data):
    with session_lock:
        sessions[session_id] = data
        sessions[session_id]['_timestamp'] = time.time()

def delete_session(session_id):
    with session_lock:
        if session_id in sessions:
            del sessions[session_id]

# ==================== ЗАГРУЗКА КОНФИГА ====================
def load_config_from_file(config_content):
    """Загрузка конфигурации из config.txt"""
    config = {}
    
    if isinstance(config_content, bytes):
        content = config_content.decode('utf-8', errors='ignore')
    else:
        content = config_content
    
    for line in content.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            config[key.strip().upper()] = value.strip()
    
    return config

# ==================== ПАРСИНГ CSV ====================
def parse_csv_content(csv_content):
    """Парсинг CSV - пропускаем заголовок"""
    if isinstance(csv_content, bytes):
        content = csv_content.decode('utf-8-sig', errors='ignore')
    else:
        content = csv_content
    
    lines = [line.strip() for line in content.split('\n') if line.strip()]
    
    # Определяем разделитель
    delimiter = '|'
    if lines and lines[0].startswith('sep='):
        delimiter = lines[0].split('=')[1].strip()
        lines = lines[1:]
    
    # Пропускаем строку заголовка
    if lines and ('Файл изображения' in lines[0] or 'файл' in lines[0].lower()):
        lines = lines[1:]
    
    csv_data = []
    for line in lines:
        if not line.strip():
            continue
        
        # Разбиваем по разделителю
        parts = []
        current = []
        in_quotes = False
        
        for char in line:
            if char == '"':
                in_quotes = not in_quotes
            elif char == delimiter and not in_quotes:
                parts.append(''.join(current).strip())
                current = []
            else:
                current.append(char)
        parts.append(''.join(current).strip())
        
        # Очищаем кавычки
        parts = [p.strip('"') for p in parts]
        
        if len(parts) >= 2:
            main_photo = parts[0].strip()
            description = parts[1].strip() if len(parts) > 1 else ''
            
            comment_photos = []
            if len(parts) > 2 and parts[2].strip():
                comment_photos = [p.strip() for p in parts[2].split(';') if p.strip()]
            
            if main_photo:  # Только если есть имя файла
                csv_data.append({
                    'main_photo': main_photo,
                    'description': description,
                    'comment_photos': comment_photos
                })
    
    return csv_data

# ==================== АНАЛИЗ ФАЙЛОВ ====================
def analyze_files(csv_data, uploaded_files):
    """Анализ наличия файлов"""
    required_files = set()
    for row in csv_data:
        if row['main_photo']:
            required_files.add(row['main_photo'])
        for photo in row['comment_photos']:
            if photo:
                required_files.add(photo)
    
    uploaded_names = set(uploaded_files.keys())
    
    return {
        'required_files': list(required_files),
        'uploaded_files': list(uploaded_names),
        'missing_files': list(required_files - uploaded_names),
        'extra_files': list(uploaded_names - required_files),
        'required_count': len(required_files),
        'uploaded_count': len(uploaded_names),
        'missing_count': len(required_files - uploaded_names)
    }

# ==================== РАЗБИВКА НА ГРУППЫ ====================
def split_into_groups(photos, group_size=2):
    groups = []
    for i in range(0, len(photos), group_size):
        groups.append(photos[i:i + group_size])
    return groups

# ==================== VK UPLOADER ====================
class VKUploader:
    def __init__(self, access_token, group_id=None):
        self.access_token = access_token
        self.group_id = group_id
        self.api_url = "https://api.vk.com/method/"
    
    def _call_api(self, method, params):
        params.update({
            'access_token': self.access_token,
            'v': VK_API_VERSION
        })
        
        response = requests.post(f"{self.api_url}{method}", data=params, timeout=30)
        response.raise_for_status()
        result = response.json()
        
        if 'error' in result:
            raise Exception(f"VK Error: {result['error'].get('error_msg', 'Unknown')}")
        
        return result['response']
    
    def get_album_upload_server(self, album_id):
        params = {'album_id': album_id}
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        return self._call_api('photos.getUploadServer', params)
    
    def get_wall_upload_server(self):
        params = {}
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        return self._call_api('photos.getWallUploadServer', params)
    
    def save_album_photo(self, server, photos_list, hash_value, album_id):
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
        params = {
            'server': server,
            'photo': photo,
            'hash': hash_value
        }
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        return self._call_api('photos.saveWallPhoto', params)
    
    def create_album_comment(self, owner_id, photo_id, attachments=None):
        params = {
            'owner_id': owner_id,
            'photo_id': photo_id,
            'message': ''
        }
        if attachments:
            params['attachments'] = ','.join(attachments)
        return self._call_api('photos.createComment', params)

# ==================== ОСНОВНЫЕ МАРШРУТЫ ====================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
@app.route('/api/init', methods=['POST'])
def init_upload():
    try:
        uploaded_files = {}
        config_content = None
        csv_content = None
        
        for file in request.files.getlist('files'):
            name = secure_filename(file.filename)
            
            if name.lower() == 'config.txt':
                config_content = file.read()
            elif name.lower().endswith('.csv'):
                csv_content = file.read()
            else:
                # КРИТИЧЕСКИ ВАЖНО: сохраняем файл в BytesIO
                file.seek(0)
                file_data = io.BytesIO()
                file_data.write(file.read())
                file_data.seek(0)
                
                uploaded_files[name] = {
                    'data': file_data,
                    'name': name,
                    'size': len(file_data.getvalue())
                }
        
        if not config_content:
            return jsonify({'success': False, 'error': 'Нет config.txt'}), 400
        
        if not csv_content:
            return jsonify({'success': False, 'error': 'Нет CSV файла'}), 400
        
        # Загружаем конфиг
        config = load_config_from_file(config_content)
        
        if 'ACCESS_TOKEN' not in config:
            return jsonify({'success': False, 'error': 'Нет ACCESS_TOKEN в config.txt'}), 400
        
        if 'ALBUM_ID' not in config:
            return jsonify({'success': False, 'error': 'Нет ALBUM_ID в config.txt'}), 400
        
        # Парсим CSV
        csv_data = parse_csv_content(csv_content)
        
        if not csv_data:
            return jsonify({'success': False, 'error': 'CSV файл пуст'}), 400
        
        # Анализируем файлы
        analysis = analyze_files(csv_data, uploaded_files)
        
        # Создаем сессию
        session_id = str(int(time.time() * 1000))
        session_data = {
            'config': config,
            'csv_data': csv_data,
            'uploaded_files': uploaded_files,
            'analysis': analysis,
            'current_row': 0,
            'results': [],
            'start_time': time.time()
        }
        
        set_session(session_id, session_data)
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'total_rows': len(csv_data),
            'file_analysis': analysis
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ОБРАБОТКА СТРОКИ ====================
@app.route('/api/process-row/<int:row_index>', methods=['POST'])
def process_row(row_index):
    session_id = request.headers.get('X-Session-ID') or request.form.get('session_id')
    
    if not session_id:
        return jsonify({'success': False, 'error': 'Нет session_id'}), 400
    
    session_data = get_session(session_id)
    if not session_data:
        return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
    
    try:
        csv_data = session_data['csv_data']
        if row_index >= len(csv_data):
            return jsonify({'success': False, 'error': 'Неверный индекс'}), 400
        
        row = csv_data[row_index]
        config = session_data['config']
        uploaded_files = session_data['uploaded_files']
        
        # Инициализируем VK загрузчик
        uploader = VKUploader(
            config['ACCESS_TOKEN'],
            config.get('GROUP_ID')
        )
        
        result = {
            'row_index': row_index,
            'main_photo': row['main_photo'],
            'description': row['description'],
            'success': False,
            'main_photo_result': None,
            'comment_results': [],
            'errors': []
        }
        
        # 1. ЗАГРУЗКА ОСНОВНОГО ФОТО
        main_photo = row['main_photo']
        
        if main_photo in uploaded_files:
            try:
                # Получаем сервер для загрузки
                upload_server = uploader.get_album_upload_server(config['ALBUM_ID'])
                
                # КРИТИЧЕСКИ ВАЖНО: создаем НОВУЮ копию файла
                file_data = uploaded_files[main_photo]['data']
                file_data.seek(0)
                
                # Создаем новый BytesIO для загрузки
                upload_file = io.BytesIO()
                upload_file.write(file_data.read())
                upload_file.seek(0)
                
                # Загружаем на сервер VK
                files = {'file1': (main_photo, upload_file, 'image/jpeg')}
                upload_response = requests.post(
                    upload_server['upload_url'],
                    files=files,
                    timeout=60
                )
                upload_response.raise_for_status()
                upload_result = upload_response.json()
                
                # Сохраняем в альбоме
                save_result = uploader.save_album_photo(
                    upload_result['server'],
                    upload_result['photos_list'],
                    upload_result['hash'],
                    config['ALBUM_ID']
                )
                
                if save_result and len(save_result) > 0:
                    photo_info = save_result[0]
                    result['main_photo_result'] = {
                        'photo_id': photo_info['id'],
                        'owner_id': photo_info['owner_id'],
                        'vk_url': f"photo{photo_info['owner_id']}_{photo_info['id']}"
                    }
                    result['success'] = True
                    
                    # 2. ЗАГРУЗКА ФОТО ДЛЯ КОММЕНТАРИЕВ
                    comment_photos = row['comment_photos']
                    
                    if comment_photos:
                        groups = split_into_groups(comment_photos, 2)
                        
                        for g_idx, group in enumerate(groups):
                            group_result = {
                                'group_index': g_idx,
                                'photos': [],
                                'success': False
                            }
                            
                            # Загружаем каждое фото в группе
                            for photo_name in group:
                                if photo_name in uploaded_files:
                                    try:
                                        # Получаем сервер для загрузки на стену
                                        wall_server = uploader.get_wall_upload_server()
                                        
                                        # КРИТИЧЕСКИ ВАЖНО: создаем НОВУЮ копию
                                        photo_data = uploaded_files[photo_name]['data']
                                        photo_data.seek(0)
                                        
                                        wall_file = io.BytesIO()
                                        wall_file.write(photo_data.read())
                                        wall_file.seek(0)
                                        
                                        # Загружаем
                                        files = {'photo': (photo_name, wall_file, 'image/jpeg')}
                                        wall_response = requests.post(
                                            wall_server['upload_url'],
                                            files=files,
                                            timeout=60
                                        )
                                        wall_response.raise_for_status()
                                        wall_result = wall_response.json()
                                        
                                        # Сохраняем для стены
                                        wall_save = uploader.save_wall_photo(
                                            wall_result['server'],
                                            wall_result['photo'],
                                            wall_result['hash']
                                        )
                                        
                                        if wall_save and len(wall_save) > 0:
                                            wall_info = wall_save[0]
                                            group_result['photos'].append({
                                                'name': photo_name,
                                                'photo_id': wall_info['id'],
                                                'owner_id': wall_info['owner_id'],
                                                'vk_url': f"photo{wall_info['owner_id']}_{wall_info['id']}"
                                            })
                                        
                                        time.sleep(0.3)
                                        
                                    except Exception as e:
                                        group_result['errors'] = [str(e)]
                                        result['errors'].append(f"{photo_name}: {str(e)}")
                            
                            # Создаем комментарий для группы
                            if group_result['photos']:
                                try:
                                    attachments = [
                                        f"photo{p['owner_id']}_{p['photo_id']}"
                                        for p in group_result['photos']
                                    ]
                                    
                                    comment = uploader.create_album_comment(
                                        result['main_photo_result']['owner_id'],
                                        result['main_photo_result']['photo_id'],
                                        attachments
                                    )
                                    
                                    group_result['success'] = True
                                    group_result['comment_id'] = comment.get('comment_id')
                                    group_result['attachments_count'] = len(attachments)
                                    
                                except Exception as e:
                                    group_result['errors'] = group_result.get('errors', []) + [str(e)]
                                    result['errors'].append(f"Комментарий: {str(e)}")
                            
                            result['comment_results'].append(group_result)
                            time.sleep(0.5)
                
            except Exception as e:
                result['errors'].append(f"Ошибка загрузки: {str(e)}")
                result['success'] = False
        else:
            result['errors'].append(f"Файл {main_photo} не найден")
        
        # Сохраняем результат
        session_data['results'].append(result)
        session_data['current_row'] = row_index + 1
        set_session(session_id, session_data)
        
        time.sleep(0.5)
        
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
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ФИНАЛИЗАЦИЯ ====================
@app.route('/api/finalize/<session_id>', methods=['GET'])
def finalize(session_id):
    session_data = get_session(session_id)
    
    if not session_data:
        return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
    
    try:
        results = session_data.get('results', [])
        analysis = session_data.get('analysis', {})
        csv_data = session_data.get('csv_data', [])
        
        successful = sum(1 for r in results if r.get('success', False))
        
        report = {
            'session_id': session_id,
            'statistics': {
                'total_rows': len(csv_data),
                'processed_rows': len(results),
                'successful_rows': successful,
                'failed_rows': len(results) - successful
            },
            'file_analysis': analysis,
            'errors': [e for r in results for e in r.get('errors', [])][:50]
        }
        
        # Очищаем сессию
        delete_session(session_id)
        
        return jsonify({'success': True, 'report': report})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ТЕСТ VK ====================
@app.route('/api/test-vk', methods=['POST'])
def test_vk():
    try:
        for file in request.files.getlist('files'):
            if file.filename.lower() == 'config.txt':
                config = load_config_from_file(file.read())
                token = config.get('ACCESS_TOKEN')
                
                if not token:
                    return jsonify({'success': False, 'error': 'Нет токена'}), 400
                
                response = requests.post(
                    'https://api.vk.com/method/users.get',
                    data={'access_token': token, 'v': VK_API_VERSION},
                    timeout=10
                )
                result = response.json()
                
                if 'error' in result:
                    return jsonify({'success': False, 'error': result['error']['error_msg']}), 400
                
                return jsonify({'success': True, 'user': result['response'][0]})
        
        return jsonify({'success': False, 'error': 'Нет config.txt'}), 400
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ПРОГРЕСС ====================
@app.route('/api/progress/<session_id>', methods=['GET'])
def progress(session_id):
    session_data = get_session(session_id)
    
    if not session_data:
        return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
    
    return jsonify({
        'success': True,
        'progress': {
            'current': session_data.get('current_row', 0),
            'total': len(session_data.get('csv_data', [])),
            'processed': len(session_data.get('results', []))
        }
    })

# ==================== ОТМЕНА ====================
@app.route('/api/cancel/<session_id>', methods=['POST'])
def cancel(session_id):
    delete_session(session_id)
    return jsonify({'success': True})

# ==================== ЗАПУСК ====================
if __name__ == '__main__':
    os.makedirs('static', exist_ok=True)
    os.makedirs('templates', exist_ok=True)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
