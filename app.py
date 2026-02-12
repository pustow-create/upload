import os
import csv
import json
import requests
import time
import threading
import io
from datetime import timedelta
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==================== НАСТРОЙКА ====================
app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'proxy-secret-key')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['JSON_AS_ASCII'] = False

VK_API_VERSION = "5.131"
sessions = {}
session_lock = threading.Lock()

# ==================== ОПТИМИЗАЦИЯ ЗАПРОСОВ ====================
def create_session_with_retries():
    """Создает сессию с повторными попытками и keep-alive"""
    session = requests.Session()
    retry = Retry(
        total=2,
        read=2,
        connect=2,
        backoff_factor=0.2,
        status_forcelist=(500, 502, 503, 504)
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10
    )
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# Сессии для разных целей
vk_session = create_session_with_retries()
upload_session = create_session_with_retries()

# ==================== ХРАНЕНИЕ СЕССИЙ ====================
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

# ==================== ПАРСИНГ ====================
def parse_config(content):
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

def parse_csv(content):
    """Парсинг CSV с поддержкой Windows-1251 и UTF-8"""
    if isinstance(content, bytes):
        try:
            content = content.decode('windows-1251')
            print("✅ CSV в кодировке Windows-1251")
        except UnicodeDecodeError:
            try:
                content = content.decode('utf-8-sig')
                print("✅ CSV в кодировке UTF-8 with BOM")
            except UnicodeDecodeError:
                content = content.decode('utf-8')
                print("✅ CSV в кодировке UTF-8")
    
    lines = [line.rstrip('\r') for line in content.split('\n') if line.strip()]
    if not lines:
        return []
    
    delimiter = '|'
    start_idx = 0
    
    if lines[0].startswith('sep='):
        delimiter = lines[0].split('=')[1].strip()
        start_idx = 1
        print(f"✅ Разделитель: '{delimiter}'")
    
    if start_idx < len(lines):
        header = lines[start_idx].lower()
        if any(x in header for x in ['файл изображения', 'файл', 'image']):
            print(f"✅ Пропущен заголовок: {lines[start_idx][:50]}...")
            start_idx += 1
    
    csv_data = []
    for i in range(start_idx, len(lines)):
        line = lines[i].strip()
        if not line:
            continue
        
        parts = line.split(delimiter)
        if len(parts) >= 1:
            main_photo = parts[0].strip()
            description = parts[1].strip() if len(parts) > 1 else ''
            comment_photos = parts[2].strip().split(';') if len(parts) > 2 and parts[2].strip() else []
            comment_photos = [p.strip() for p in comment_photos if p.strip()]
            
            if main_photo:
                csv_data.append({
                    'main_photo': main_photo,
                    'description': description,
                    'comment_photos': comment_photos
                })
                print(f"📄 CSV строка {len(csv_data)}: {main_photo} - {description[:50]}...")
    
    print(f"📊 Всего загружено записей: {len(csv_data)}")
    return csv_data

# ==================== ПРОКСИ-ФУНКЦИИ ДЛЯ VK ====================
def proxy_upload_to_album(upload_url, file_data, filename):
    """Загрузка фото в альбом"""
    files = {'file1': (filename, file_data, 'image/jpeg')}
    response = upload_session.post(upload_url, files=files, timeout=60)
    response.raise_for_status()
    return response.json()

def proxy_upload_to_wall(upload_url, file_data, filename):
    """Загрузка фото на стену"""
    files = {'photo': (filename, file_data, 'image/jpeg')}
    response = upload_session.post(upload_url, files=files, timeout=60)
    response.raise_for_status()
    return response.json()

def proxy_get_upload_server(access_token, album_id, group_id=None):
    """Получение сервера для загрузки в альбом"""
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION,
        'album_id': album_id
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    response = vk_session.get('https://api.vk.com/method/photos.getUploadServer', params=params, timeout=30)
    response.raise_for_status()
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response']['upload_url']

def proxy_get_wall_upload_server(access_token, group_id=None):
    """Получение сервера для загрузки на стену"""
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    response = vk_session.get('https://api.vk.com/method/photos.getWallUploadServer', params=params, timeout=30)
    response.raise_for_status()
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response']['upload_url']

def proxy_save_album_photo(access_token, server, photos_list, hash_value, album_id, group_id=None, description=""):
    """Сохранить фото в альбоме с описанием"""
    params = {
        'access_token': access_token,
        'v': '5.131',
        'album_id': album_id,
        'server': server,
        'photos_list': photos_list,
        'hash': hash_value,
    }
    
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    if description and description.strip():
        params['caption'] = description.strip()
    
    response = vk_session.get('https://api.vk.com/method/photos.save', params=params, timeout=30)
    response.raise_for_status()
    result = response.json()
    
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response']

def proxy_save_wall_photo(access_token, server, photo, hash_value, group_id=None):
    """Сохранить фото для стены"""
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION,
        'server': server,
        'photo': photo,
        'hash': hash_value
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    response = vk_session.post('https://api.vk.com/method/photos.saveWallPhoto', data=params, timeout=30)
    response.raise_for_status()
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response']

def proxy_create_comment(access_token, owner_id, photo_id, attachments, group_id=None):
    """Создание комментария от имени группы"""
    if group_id:
        owner_id = -abs(int(group_id))
    
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION,
        'owner_id': owner_id,
        'photo_id': photo_id,
        'message': '',
        'attachments': ','.join(attachments),
        'from_group': 1
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    response = vk_session.post('https://api.vk.com/method/photos.createComment', data=params, timeout=30)
    response.raise_for_status()
    result = response.json()
    
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    
    return {'comment_id': result['response']}

# ==================== ОСНОВНЫЕ МАРШРУТЫ ====================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'time': time.time()})

@app.route('/api/test-vk', methods=['POST'])
def test_vk():
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
        
        params = {'access_token': token, 'v': VK_API_VERSION}
        response = vk_session.get('https://api.vk.com/method/users.get', params=params, timeout=10)
        result = response.json()
        
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']['error_msg']}), 400
        
        return jsonify({'success': True, 'user': result['response'][0]})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/analyze', methods=['POST'])
def analyze():
    try:
        config_content = None
        csv_content = None
        
        for file in request.files.getlist('files'):
            filename = file.filename.lower()
            if filename == 'config.txt' or (filename.endswith('.txt') and 'config' in filename):
                config_content = file.read()
            elif filename.endswith('.csv'):
                csv_content = file.read()
        
        if not config_content or not csv_content:
            return jsonify({'success': False, 'error': 'Не найдены config.txt или CSV файл'}), 400
        
        config = parse_config(config_content)
        if 'ACCESS_TOKEN' not in config or 'ALBUM_ID' not in config:
            return jsonify({'success': False, 'error': 'Нет ACCESS_TOKEN или ALBUM_ID'}), 400
        
        csv_data = parse_csv(csv_content)
        if not csv_data:
            return jsonify({'success': False, 'error': 'CSV пуст'}), 400
        
        required_files = set()
        for row in csv_data:
            required_files.add(row['main_photo'])
            required_files.update(row['comment_photos'])
        
        session_id = str(int(time.time() * 1000))
        session_data = {
            'config': config,
            'csv_data': csv_data,
            'required_files': list(required_files),
            'total_rows': len(csv_data),
            'current_row': 0,
            'results': [],
            'start_time': time.time(),
            'uploaded_files': set()  # ОТСЛЕЖИВАЕМ РЕАЛЬНО ЗАГРУЖЕННЫЕ ФАЙЛЫ
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
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get-upload-urls/<session_id>/<int:row_index>', methods=['GET'])
def get_upload_urls(session_id, row_index):
    try:
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        csv_data = session_data.get('csv_data', [])
        if row_index >= len(csv_data):
            return jsonify({'success': False, 'error': 'Неверный индекс'}), 400
        
        row = csv_data[row_index]
        config = session_data.get('config', {})
        
        album_url = proxy_get_upload_server(
            config['ACCESS_TOKEN'], 
            config['ALBUM_ID'], 
            config.get('GROUP_ID')
        )
        
        # ОДИН URL для всех комментариев в строке
        wall_upload_url = proxy_get_wall_upload_server(
            config['ACCESS_TOKEN'], 
            config.get('GROUP_ID')
        )
        
        comment_urls = []
        for i in range(0, len(row['comment_photos']), 2):
            group = row['comment_photos'][i:i+2]
            comment_urls.append({
                'group': group,
                'upload_url': wall_upload_url
            })
        
        return jsonify({
            'success': True,
            'row_index': row_index,
            'description': row['description'],
            'main_photo': {
                'filename': row['main_photo'],
                'upload_url': album_url
            },
            'comment_groups': comment_urls,
            'wall_upload_url': wall_upload_url  # Отправляем отдельно для удобства
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/proxy/upload-album', methods=['POST'])
def proxy_upload_album():
    try:
        session_id = request.form.get('session_id')
        filename = request.form.get('filename')
        upload_url = request.form.get('upload_url')
        description = request.form.get('description', '')
        
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Нет файла'}), 400
        
        file = request.files['file']
        file_data = file.read()
        
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        config = session_data.get('config', {})
        
        upload_result = proxy_upload_to_album(upload_url, file_data, filename)
        save_result = proxy_save_album_photo(
            config['ACCESS_TOKEN'], 
            upload_result['server'], 
            upload_result['photos_list'],
            upload_result['hash'], 
            config['ALBUM_ID'], 
            config.get('GROUP_ID'), 
            description
        )
        
        # ОТМЕЧАЕМ ФАЙЛ КАК ЗАГРУЖЕННЫЙ
        with session_lock:
            session_data.setdefault('uploaded_files', set()).add(filename)
        
        return jsonify({'success': True, 'photo': save_result[0]})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/proxy/upload-wall', methods=['POST'])
def proxy_upload_wall():
    try:
        session_id = request.form.get('session_id')
        filename = request.form.get('filename')
        upload_url = request.form.get('upload_url')
        
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Нет файла'}), 400
        
        file = request.files['file']
        file_data = file.read()
        
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        config = session_data.get('config', {})
        
        upload_result = proxy_upload_to_wall(upload_url, file_data, filename)
        save_result = proxy_save_wall_photo(
            config['ACCESS_TOKEN'], 
            upload_result['server'], 
            upload_result['photo'],
            upload_result['hash'], 
            config.get('GROUP_ID')
        )
        
        # ОТМЕЧАЕМ ФАЙЛ КАК ЗАГРУЖЕННЫЙ
        with session_lock:
            session_data.setdefault('uploaded_files', set()).add(filename)
        
        return jsonify({'success': True, 'photo': save_result[0]})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/proxy/create-comment', methods=['POST'])
def proxy_create_comment_endpoint():
    try:
        data = request.json
        session_id = data.get('session_id')
        owner_id = data.get('owner_id')
        photo_id = data.get('photo_id')
        attachments = data.get('attachments', [])
        
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        config = session_data.get('config', {})
        group_id = config.get('GROUP_ID')
        
        result = proxy_create_comment(
            config['ACCESS_TOKEN'], 
            owner_id, 
            photo_id, 
            attachments, 
            group_id
        )
        
        return jsonify({'success': True, 'comment_id': result.get('comment_id')})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/save-result', methods=['POST'])
def save_result():
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
        
        row = session_data['csv_data'][row_index]
        
        # СОБИРАЕМ РЕАЛЬНО ЗАГРУЖЕННЫЕ ФАЙЛЫ
        uploaded_in_row = set()
        if main_photo_result:
            uploaded_in_row.add(row['main_photo'])
        
        for comment in comment_results:
            for photo in comment.get('photos', []):
                uploaded_in_row.add(photo.get('name'))
        
        # Обновляем глобальный список загруженных файлов
        with session_lock:
            session_data.setdefault('uploaded_files', set()).update(uploaded_in_row)
        
        # ПРОВЕРЯЕМ, ВСЕ ЛИ ФАЙЛЫ ИЗ СТРОКИ ЗАГРУЖЕНЫ
        expected_files = set([row['main_photo']] + row['comment_photos'])
        missing_files = expected_files - uploaded_in_row
        
        if missing_files:
            print(f"⚠️ В строке {row_index} не хватает: {missing_files}")
            # НЕ ДОБАВЛЯЕМ ОШИБКУ, ПРОСТО ЛОГИРУЕМ
        
        result = {
            'row_index': row_index,
            'main_photo': row['main_photo'],
            'description': row['description'],
            'success': len(errors) == 0 and main_photo_result is not None,
            'main_photo_result': main_photo_result,
            'comment_results': comment_results,
            'errors': errors,
            'uploaded_files': list(uploaded_in_row)
        }
        
        session_data.setdefault('results', []).append(result)
        session_data['current_row'] = row_index + 1
        set_session(session_id, session_data)
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/finalize/<session_id>', methods=['GET'])
def finalize(session_id):
    try:
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        results = session_data.get('results', [])
        csv_data = session_data.get('csv_data', [])
        required_files = set(session_data.get('required_files', []))
        
        # ИСПОЛЬЗУЕМ РЕАЛЬНО ЗАГРУЖЕННЫЕ ФАЙЛЫ ИЗ СЕССИИ
        uploaded_files = session_data.get('uploaded_files', set())
        
        successful = sum(1 for r in results if r.get('success'))
        
        # ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА ПО РЕЗУЛЬТАТАМ
        for r in results:
            if r.get('main_photo_result'):
                uploaded_files.add(r['main_photo'])
            for c in r.get('comment_results', []):
                for p in c.get('photos', []):
                    uploaded_files.add(p.get('name'))
        
        missing_files = required_files - uploaded_files
        
        # ЛОГИРУЕМ ПОДРОБНОСТИ
        print(f"\n📊 ИТОГОВЫЙ ОТЧЕТ:")
        print(f"  Всего требуется: {len(required_files)}")
        print(f"  Загружено: {len(uploaded_files)}")
        print(f"  Не хватает: {len(missing_files)}")
        if missing_files:
            print(f"  Список: {sorted(missing_files)}")
        
        elapsed = time.time() - session_data.get('start_time', time.time())
        
        return jsonify({'success': True, 'report': {
            'session_id': session_id,
            'statistics': {
                'total_rows': len(csv_data),
                'processed_rows': len(results),
                'successful_rows': successful,
                'failed_rows': len(results) - successful,
                'total_time': f"{elapsed:.1f}с",
                'avg_time_per_row': f"{elapsed/len(results):.1f}с" if results else "0с"
            },
            'files': {
                'required_count': len(required_files),
                'uploaded_count': len(uploaded_files),
                'missing_count': len(missing_files),
                'missing_files': sorted(list(missing_files))[:50]
            }
        }})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/cancel/<session_id>', methods=['POST'])
def cancel(session_id):
    delete_session(session_id)
    return jsonify({'success': True})

# ==================== ЗАПУСК ====================
if __name__ == '__main__':
    os.makedirs('static', exist_ok=True)
    os.makedirs('templates', exist_ok=True)
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Запуск сервера на порту {port}")
    print(f"⚡ Keep-Alive: Да, Пул соединений: 10")
    print(f"📁 Главная: http://localhost:{port}/")
    app.run(host='0.0.0.0', port=port, debug=False)
