import os
import csv
import json
import requests
import time
import threading
from datetime import timedelta
from flask import Flask, render_template, request, jsonify, session
from werkzeug.utils import secure_filename

# ==================== НАСТРОЙКА ПРИЛОЖЕНИЯ ====================
app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'local-dev-secret-key')
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

# ==================== ПАРСИНГ КОНФИГА ====================
def parse_config(content):
    """Парсинг config.txt - только текст, без файлов"""
    config = {}
    if isinstance(content, bytes):
        content = content.decode('utf-8', errors='ignore')
    
    for line in content.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            config[key.strip().upper()] = value.strip()
    
    return config

# ==================== ПАРСИНГ CSV ====================
def parse_csv(content):
    """Парсинг CSV - только текст, без файлов"""
    if isinstance(content, bytes):
        content = content.decode('utf-8-sig', errors='ignore')
    
    lines = [line.strip() for line in content.split('\n') if line.strip()]
    
    # Определяем разделитель
    delimiter = '|'
    if lines and lines[0].startswith('sep='):
        delimiter = lines[0].split('=')[1].strip()
        lines = lines[1:]
    
    # Пропускаем заголовок
    if lines and ('Файл изображения' in lines[0] or 'файл' in lines[0].lower()):
        lines = lines[1:]
    
    csv_data = []
    for line in lines:
        if not line.strip():
            continue
        
        parts = [p.strip().strip('"') for p in line.split(delimiter)]
        
        if len(parts) >= 2:
            main_photo = parts[0].strip()
            description = parts[1].strip() if len(parts) > 1 else ''
            
            comment_photos = []
            if len(parts) > 2 and parts[2].strip():
                comment_photos = [p.strip() for p in parts[2].split(';') if p.strip()]
            
            if main_photo:
                csv_data.append({
                    'main_photo': main_photo,
                    'description': description,
                    'comment_photos': comment_photos
                })
    
    return csv_data

# ==================== VK API - ТОЛЬКО ЗАПРОСЫ, БЕЗ ФАЙЛОВ ====================
class VKAPI:
    def __init__(self, access_token, group_id=None):
        self.access_token = access_token
        self.group_id = group_id
        self.api_url = "https://api.vk.com/method/"
    
    def call(self, method, params):
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
                raise Exception(f"VK Error: {result['error'].get('error_msg', 'Unknown')}")
            
            return result['response']
        except Exception as e:
            raise Exception(f"VK API Error: {str(e)}")
    
    def get_album_upload_url(self, album_id):
        """Получить URL для загрузки в альбом"""
        params = {'album_id': album_id}
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        result = self.call('photos.getUploadServer', params)
        return result['upload_url']
    
    def get_wall_upload_url(self):
        """Получить URL для загрузки на стену"""
        params = {}
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        result = self.call('photos.getWallUploadServer', params)
        return result['upload_url']
    
    def save_album_photo(self, server, photos_list, hash_value, album_id):
        """Сохранить фото в альбоме"""
        params = {
            'server': server,
            'photos_list': photos_list,
            'hash': hash_value,
            'album_id': album_id
        }
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        return self.call('photos.save', params)
    
    def save_wall_photo(self, server, photo, hash_value):
        """Сохранить фото для стены"""
        params = {
            'server': server,
            'photo': photo,
            'hash': hash_value
        }
        if self.group_id:
            params['group_id'] = abs(int(self.group_id))
        return self.call('photos.saveWallPhoto', params)
    
    def create_comment(self, owner_id, photo_id, attachments):
        """Создать комментарий к фото"""
        params = {
            'owner_id': owner_id,
            'photo_id': photo_id,
            'message': '',
            'attachments': ','.join(attachments)
        }
        return self.call('photos.createComment', params)
    
    def test_token(self):
        """Проверка токена"""
        result = self.call('users.get', {})
        return result[0] if result else None

# ==================== МАРШРУТЫ ====================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})

# ==================== 1. АНАЛИЗ CSV (ТОЛЬКО ТЕКСТ) ====================
@app.route('/api/analyze', methods=['POST'])
def analyze():
    """Анализ CSV файла - только текст, файлы не загружаются"""
    try:
        # Получаем только текстовые файлы (config.csv, .csv)
        config_content = None
        csv_content = None
        
        for file in request.files.getlist('files'):
            filename = file.filename.lower()
            
            # config.txt
            if filename == 'config.txt' or (filename.endswith('.txt') and 'config' in filename):
                config_content = file.read()
                print(f"✅ Config загружен: {file.filename}")
            
            # CSV файл
            elif filename.endswith('.csv'):
                csv_content = file.read()
                print(f"✅ CSV загружен: {file.filename}")
        
        if not config_content:
            return jsonify({'success': False, 'error': 'Не найден config.txt'}), 400
        
        if not csv_content:
            return jsonify({'success': False, 'error': 'Не найден CSV файл'}), 400
        
        # Парсим конфиг
        config = parse_config(config_content)
        
        if 'ACCESS_TOKEN' not in config:
            return jsonify({'success': False, 'error': 'В config.txt нет ACCESS_TOKEN'}), 400
        
        if 'ALBUM_ID' not in config:
            return jsonify({'success': False, 'error': 'В config.txt нет ALBUM_ID'}), 400
        
        # Парсим CSV
        csv_data = parse_csv(csv_content)
        
        if not csv_data:
            return jsonify({'success': False, 'error': 'CSV файл пуст'}), 400
        
        # Собираем список всех нужных файлов
        required_files = set()
        for row in csv_data:
            required_files.add(row['main_photo'])
            for photo in row['comment_photos']:
                required_files.add(photo)
        
        # Создаем сессию
        session_id = str(int(time.time() * 1000))
        session_data = {
            'config': config,
            'csv_data': csv_data,
            'required_files': list(required_files),
            'total_rows': len(csv_data),
            'current_row': 0,
            'results': [],
            'start_time': time.time()
        }
        
        set_session(session_id, session_data)
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'total_rows': len(csv_data),
            'required_files': list(required_files),
            'required_count': len(required_files)
        })
        
    except Exception as e:
        print(f"❌ Ошибка анализа: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== 2. ТЕСТ VK ====================
@app.route('/api/test-vk', methods=['POST'])
def test_vk():
    """Тест подключения к VK"""
    try:
        config_content = None
        
        for file in request.files.getlist('files'):
            filename = file.filename.lower()
            if filename == 'config.txt' or (filename.endswith('.txt') and 'config' in filename):
                config_content = file.read()
                break
        
        if not config_content:
            return jsonify({'success': False, 'error': 'Не найден config.txt'}), 400
        
        config = parse_config(config_content)
        token = config.get('ACCESS_TOKEN')
        
        if not token:
            return jsonify({'success': False, 'error': 'Нет ACCESS_TOKEN'}), 400
        
        vk = VKAPI(token, config.get('GROUP_ID'))
        user = vk.test_token()
        
        return jsonify({
            'success': True,
            'user': user
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== 3. ПОЛУЧИТЬ URL ДЛЯ ЗАГРУЗКИ ====================
@app.route('/api/get-upload-urls/<session_id>/<int:row_index>', methods=['GET'])
def get_upload_urls(session_id, row_index):
    """Получить URL для загрузки фото в VK"""
    try:
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        csv_data = session_data.get('csv_data', [])
        if row_index >= len(csv_data):
            return jsonify({'success': False, 'error': 'Неверный индекс'}), 400
        
        row = csv_data[row_index]
        config = session_data.get('config', {})
        
        vk = VKAPI(config['ACCESS_TOKEN'], config.get('GROUP_ID'))
        
        # 1. URL для основного фото
        album_url = vk.get_album_upload_url(config['ALBUM_ID'])
        
        # 2. URL для фото в комментариях
        comment_urls = []
        comment_photos = row['comment_photos']
        
        if comment_photos:
            # Разбиваем на группы по 2 фото
            groups = []
            for i in range(0, len(comment_photos), 2):
                groups.append(comment_photos[i:i+2])
            
            for group in groups:
                comment_urls.append({
                    'group': group,
                    'upload_url': vk.get_wall_upload_url()
                })
        
        return jsonify({
            'success': True,
            'row_index': row_index,
            'main_photo': {
                'filename': row['main_photo'],
                'upload_url': album_url
            },
            'comment_groups': comment_urls
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== 4. СОХРАНИТЬ РЕЗУЛЬТАТ ЗАГРУЗКИ ====================
@app.route('/api/save-result', methods=['POST'])
def save_result():
    """Сохранить результат загрузки от браузера"""
    try:
        data = request.json
        session_id = data.get('session_id')
        row_index = data.get('row_index')
        main_photo_result = data.get('main_photo_result')
        comment_results = data.get('comment_results', [])
        errors = data.get('errors', [])
        
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        csv_data = session_data.get('csv_data', [])
        row = csv_data[row_index]
        
        result = {
            'row_index': row_index,
            'main_photo': row['main_photo'],
            'description': row['description'],
            'success': len(errors) == 0 and main_photo_result is not None,
            'main_photo_result': main_photo_result,
            'comment_results': comment_results,
            'errors': errors
        }
        
        if 'results' not in session_data:
            session_data['results'] = []
        
        session_data['results'].append(result)
        session_data['current_row'] = row_index + 1
        set_session(session_id, session_data)
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== 5. ФИНАЛЬНЫЙ ОТЧЕТ ====================
@app.route('/api/finalize/<session_id>', methods=['GET'])
def finalize(session_id):
    """Получить финальный отчет"""
    try:
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        results = session_data.get('results', [])
        csv_data = session_data.get('csv_data', [])
        required_files = session_data.get('required_files', [])
        
        # Статистика
        successful = sum(1 for r in results if r.get('success', False))
        
        # Какие файлы реально загружены
        uploaded_files = set()
        for result in results:
            if result.get('main_photo_result'):
                uploaded_files.add(result['main_photo'])
            for comment in result.get('comment_results', []):
                for photo in comment.get('photos', []):
                    uploaded_files.add(photo.get('name'))
        
        missing_files = set(required_files) - uploaded_files
        
        report = {
            'session_id': session_id,
            'statistics': {
                'total_rows': len(csv_data),
                'processed_rows': len(results),
                'successful_rows': successful,
                'failed_rows': len(results) - successful
            },
            'files': {
                'required_count': len(required_files),
                'uploaded_count': len(uploaded_files),
                'missing_count': len(missing_files),
                'missing_files': list(missing_files)[:50]
            },
            'errors': [e for r in results for e in r.get('errors', [])][:50]
        }
        
        return jsonify({'success': True, 'report': report})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== 6. ОТМЕНА ====================
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