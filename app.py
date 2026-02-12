import os
import csv
import json
import requests
import time
import threading
import asyncio
import aiohttp
from datetime import timedelta
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

# ==================== НАСТРОЙКА ====================
app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'proxy-secret-key')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['JSON_AS_ASCII'] = False

VK_API_VERSION = "5.131"
sessions = {}
session_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=20)  # Пул потоков для параллельной загрузки

# Кэш для URL загрузки
upload_url_cache = {}
cache_lock = threading.Lock()

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
        sessions.pop(session_id, None)

# ==================== ОПТИМИЗИРОВАННЫЙ ПАРСИНГ ====================
def parse_config(content):
    """Быстрый парсинг конфига"""
    config = {}
    if isinstance(content, bytes):
        # Пробуем UTF-8, если ошибка - windows-1251
        try:
            content = content.decode('utf-8')
        except:
            content = content.decode('windows-1251')
    
    for line in content.splitlines():
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            config[key.strip().upper()] = value.strip()
    return config

def parse_csv(content):
    """Ультра-быстрый парсинг CSV"""
    if isinstance(content, bytes):
        # Пробуем windows-1251, потом utf-8-sig, потом utf-8
        for enc in ['windows-1251', 'utf-8-sig', 'utf-8']:
            try:
                content = content.decode(enc)
                break
            except:
                continue
    
    lines = [line.rstrip('\r') for line in content.splitlines() if line.strip()]
    if not lines:
        return []
    
    # Определяем разделитель и начало данных
    start_idx = 0
    delimiter = '|'
    if lines[0].startswith('sep='):
        delimiter = lines[0].split('=')[1]
        start_idx = 2 if 'файл' in lines[1].lower() else 1
    elif 'файл' in lines[0].lower():
        start_idx = 1
    
    csv_data = []
    for line in lines[start_idx:]:
        parts = line.split(delimiter)
        if parts[0].strip():
            main_photo = parts[0].strip()
            description = parts[1].strip() if len(parts) > 1 else ''
            comment_photos = []
            if len(parts) > 2 and parts[2].strip():
                comment_photos = [p.strip() for p in parts[2].split(';') if p.strip()]
            csv_data.append({
                'main_photo': main_photo,
                'description': description,
                'comment_photos': comment_photos[:4]  # Ограничиваем до 4 фото для скорости
            })
    
    return csv_data

# ==================== КЭШИРОВАННЫЕ VK API ====================
@lru_cache(maxsize=32)
def get_cached_upload_server(access_token, album_id, group_id=None):
    """Кэширование URL загрузки на 10 минут"""
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION,
        'album_id': album_id
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    response = requests.get('https://api.vk.com/method/photos.getUploadServer', 
                          params=params, timeout=5)
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response']['upload_url']

@lru_cache(maxsize=32)
def get_cached_wall_upload_server(access_token, group_id=None):
    """Кэширование URL загрузки на стену"""
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    response = requests.get('https://api.vk.com/method/photos.getWallUploadServer', 
                          params=params, timeout=5)
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response']['upload_url']

# ==================== ПАРАЛЛЕЛЬНАЯ ЗАГРУЗКА ====================
def upload_photo_sync(upload_url, file_data, filename, is_wall=False):
    """Синхронная загрузка одного фото"""
    try:
        files = {'file1' if not is_wall else 'photo': (filename, file_data, 'image/jpeg')}
        response = requests.post(upload_url, files=files, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {'error': str(e)}

def parallel_upload_photos(upload_urls_files):
    """Параллельная загрузка нескольких фото"""
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for upload_url, file_data, filename, is_wall in upload_urls_files:
            future = executor.submit(upload_photo_sync, upload_url, file_data, filename, is_wall)
            futures.append(future)
        
        results = []
        for future in as_completed(futures):
            results.append(future.result())
        return results

def save_album_photo(access_token, server, photos_list, hash_value, album_id, group_id=None, description=""):
    """Сохранить фото в альбом"""
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
    
    response = requests.get('https://api.vk.com/method/photos.save', params=params, timeout=10)
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response'][0]

def save_wall_photo(access_token, server, photo, hash_value, group_id=None):
    """Сохранить фото на стену"""
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION,
        'server': server,
        'photo': photo,
        'hash': hash_value
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    response = requests.post('https://api.vk.com/method/photos.saveWallPhoto', 
                           data=params, timeout=10)
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response'][0]

# ==================== ОПТИМИЗИРОВАННЫЕ МАРШРУТЫ ====================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'time': time.time()})

@app.route('/api/analyze', methods=['POST'])
def analyze():
    """Быстрый анализ файлов"""
    try:
        config_content = None
        csv_content = None
        
        for file in request.files.getlist('files'):
            filename = file.filename.lower()
            if 'config' in filename and filename.endswith('.txt'):
                config_content = file.read()
            elif filename.endswith('.csv'):
                csv_content = file.read()
        
        if not config_content or not csv_content:
            return jsonify({'success': False, 'error': 'Отсутствуют файлы'}), 400
        
        config = parse_config(config_content)
        if 'ACCESS_TOKEN' not in config or 'ALBUM_ID' not in config:
            return jsonify({'success': False, 'error': 'Нет ACCESS_TOKEN или ALBUM_ID'}), 400
        
        csv_data = parse_csv(csv_content)
        if not csv_data:
            return jsonify({'success': False, 'error': 'CSV пуст'}), 400
        
        # Быстрый сбор уникальных файлов
        required_files = set()
        for row in csv_data:
            required_files.add(row['main_photo'])
            required_files.update(row['comment_photos'])
        
        # Предзагружаем URL загрузки
        try:
            album_url = get_cached_upload_server(
                config['ACCESS_TOKEN'], 
                config['ALBUM_ID'], 
                config.get('GROUP_ID')
            )
            wall_url = get_cached_wall_upload_server(
                config['ACCESS_TOKEN'], 
                config.get('GROUP_ID')
            )
        except:
            album_url = wall_url = None
        
        session_id = str(int(time.time() * 1000))
        session_data = {
            'config': config,
            'csv_data': csv_data,
            'required_files': list(required_files),
            'total_rows': len(csv_data),
            'current_row': 0,
            'results': [],
            'start_time': time.time(),
            'cached_urls': {
                'album': album_url,
                'wall': wall_url
            }
        }
        set_session(session_id, session_data)
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'total_rows': len(csv_data),
            'required_count': len(required_files)
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/upload-batch', methods=['POST'])
def upload_batch():
    """Пакетная загрузка всех фото одной строки"""
    try:
        data = request.json
        session_id = data.get('session_id')
        row_index = data.get('row_index')
        files = data.get('files', [])
        
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        config = session_data.get('config', {})
        csv_data = session_data.get('csv_data', [])
        row = csv_data[row_index]
        
        # Получаем или используем кэшированные URL
        album_url = session_data.get('cached_urls', {}).get('album')
        if not album_url:
            album_url = get_cached_upload_server(
                config['ACCESS_TOKEN'], 
                config['ALBUM_ID'], 
                config.get('GROUP_ID')
            )
        
        wall_url = session_data.get('cached_urls', {}).get('wall')
        if not wall_url:
            wall_url = get_cached_wall_upload_server(
                config['ACCESS_TOKEN'], 
                config.get('GROUP_ID')
            )
        
        # Подготавливаем данные для параллельной загрузки
        upload_tasks = []
        
        # Главное фото
        main_file = next((f for f in files if f['filename'] == row['main_photo']), None)
        if main_file:
            import base64
            file_data = base64.b64decode(main_file['data'].split(',')[1])
            upload_tasks.append((album_url, file_data, main_file['filename'], False))
        
        # Фото для комментариев
        comment_photos = []
        for comment_photo in row['comment_photos'][:4]:  # Максимум 4 фото
            comment_file = next((f for f in files if f['filename'] == comment_photo), None)
            if comment_file:
                file_data = base64.b64decode(comment_file['data'].split(',')[1])
                upload_tasks.append((wall_url, file_data, comment_file['filename'], True))
                comment_photos.append(comment_photo)
        
        # Параллельная загрузка всех фото
        upload_results = parallel_upload_photos(upload_tasks)
        
        # Обработка результатов
        main_photo_result = None
        comment_results = []
        errors = []
        
        for i, result in enumerate(upload_results):
            if 'error' in result:
                errors.append(f"Ошибка загрузки: {result['error']}")
                continue
                
            if i == 0:  # Главное фото
                try:
                    photo = save_album_photo(
                        config['ACCESS_TOKEN'],
                        result['server'],
                        result['photos_list'],
                        result['hash'],
                        config['ALBUM_ID'],
                        config.get('GROUP_ID'),
                        row['description']
                    )
                    main_photo_result = {
                        'id': photo['id'],
                        'owner_id': photo['owner_id'],
                        'name': row['main_photo']
                    }
                except Exception as e:
                    errors.append(f"Ошибка сохранения главного фото: {str(e)}")
            else:  # Фото для комментариев
                try:
                    photo = save_wall_photo(
                        config['ACCESS_TOKEN'],
                        result['server'],
                        result['photo'],
                        result['hash'],
                        config.get('GROUP_ID')
                    )
                    comment_results.append({
                        'photo_id': photo['id'],
                        'owner_id': photo['owner_id'],
                        'name': comment_photos[i-1] if i-1 < len(comment_photos) else 'unknown'
                    })
                except Exception as e:
                    errors.append(f"Ошибка сохранения фото комментария: {str(e)}")
        
        # Создаем комментарий, если есть фото и главное фото загружено
        comment_id = None
        if comment_results and main_photo_result and not errors:
            try:
                attachments = [f"photo{photo['owner_id']}_{photo['photo_id']}" 
                             for photo in comment_results]
                
                owner_id = -abs(int(config.get('GROUP_ID', main_photo_result['owner_id'])))
                
                params = {
                    'access_token': config['ACCESS_TOKEN'],
                    'v': VK_API_VERSION,
                    'owner_id': owner_id,
                    'photo_id': main_photo_result['id'],
                    'message': '',
                    'attachments': ','.join(attachments),
                    'from_group': 1
                }
                if config.get('GROUP_ID'):
                    params['group_id'] = abs(int(config['GROUP_ID']))
                
                response = requests.post('https://api.vk.com/method/photos.createComment', 
                                       data=params, timeout=10)
                result = response.json()
                if 'error' not in result:
                    comment_id = result['response']
            except Exception as e:
                errors.append(f"Ошибка создания комментария: {str(e)}")
        
        # Сохраняем результат
        result_data = {
            'row_index': row_index,
            'main_photo': row['main_photo'],
            'description': row['description'],
            'success': len(errors) == 0 and main_photo_result is not None,
            'main_photo_result': main_photo_result,
            'comment_results': comment_results,
            'comment_id': comment_id,
            'errors': errors
        }
        
        session_data['results'].append(result_data)
        session_data['current_row'] = row_index + 1
        set_session(session_id, session_data)
        
        return jsonify({
            'success': True,
            'result': result_data,
            'progress': f"{session_data['current_row']}/{session_data['total_rows']}"
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/status/<session_id>', methods=['GET'])
def get_status(session_id):
    """Быстрый статус сессии"""
    session_data = get_session(session_id)
    if not session_data:
        return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
    
    results = session_data.get('results', [])
    total = session_data.get('total_rows', 0)
    
    return jsonify({
        'success': True,
        'processed': len(results),
        'total': total,
        'successful': sum(1 for r in results if r.get('success', False)),
        'failed': sum(1 for r in results if not r.get('success', False)),
        'progress': f"{len(results)}/{total}"
    })

@app.route('/api/finalize/<session_id>', methods=['GET'])
def finalize(session_id):
    """Финальный отчет"""
    session_data = get_session(session_id)
    if not session_data:
        return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
    
    results = session_data.get('results', [])
    csv_data = session_data.get('csv_data', [])
    
    successful = sum(1 for r in results if r.get('success', False))
    
    # Очищаем кэш
    delete_session(session_id)
    
    return jsonify({
        'success': True,
        'report': {
            'statistics': {
                'total_rows': len(csv_data),
                'processed_rows': len(results),
                'successful_rows': successful,
                'failed_rows': len(results) - successful
            },
            'time_elapsed': time.time() - session_data.get('start_time', time.time())
        }
    })

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
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
