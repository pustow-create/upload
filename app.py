import os
import csv
import json
import requests
import time
import threading
import base64
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
# Пул потоков для параллельной загрузки
executor = ThreadPoolExecutor(max_workers=20)

# ==================== КЭШ URL ====================
url_cache = {}
url_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 минут

def get_cached_url(cache_key, api_func, *args, **kwargs):
    """Получить URL из кэша или запросить новый"""
    with url_cache_lock:
        cached = url_cache.get(cache_key)
        if cached and time.time() - cached['time'] < CACHE_TTL:
            return cached['url']
    
    try:
        url = api_func(*args, **kwargs)
        with url_cache_lock:
            url_cache[cache_key] = {'url': url, 'time': time.time()}
        return url
    except Exception as e:
        print(f"❌ Ошибка получения URL: {e}")
        raise

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

# ==================== БЫСТРЫЙ ПАРСИНГ ====================
def parse_config(content):
    """Сверхбыстрый парсинг конфига"""
    config = {}
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except:
            content = content.decode('windows-1251')
    
    for line in content.split('\n'):
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            config[key.strip().upper()] = value.strip()
    return config

def parse_csv(content):
    """Максимально быстрый парсинг CSV"""
    if isinstance(content, bytes):
        # Пробуем все кодировки быстро
        for enc in ['windows-1251', 'utf-8-sig', 'utf-8']:
            try:
                content = content.decode(enc)
                break
            except:
                continue
    
    lines = [line.rstrip('\r') for line in content.split('\n') if line.strip()]
    if not lines:
        return []
    
    # Определяем начало данных
    start_idx = 0
    delimiter = '|'
    
    if lines[0].startswith('sep='):
        delimiter = lines[0].split('=')[1]
        start_idx = 2 if len(lines) > 1 and 'файл' in lines[1].lower() else 1
    elif lines and 'файл' in lines[0].lower():
        start_idx = 1
    
    csv_data = []
    for line in lines[start_idx:]:
        if not line:
            continue
        parts = line.split(delimiter)
        if parts and parts[0].strip():
            main_photo = parts[0].strip()
            description = parts[1].strip() if len(parts) > 1 else ''
            comment_photos = []
            if len(parts) > 2 and parts[2].strip():
                # Максимум 4 фото для скорости
                comment_photos = [p.strip() for p in parts[2].split(';') if p.strip()][:4]
            csv_data.append({
                'main_photo': main_photo,
                'description': description,
                'comment_photos': comment_photos
            })
    
    return csv_data

# ==================== VK API ФУНКЦИИ ====================
def get_album_upload_server(access_token, album_id, group_id=None):
    """Получить сервер для загрузки в альбом"""
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION,
        'album_id': album_id
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    response = requests.get('https://api.vk.com/method/photos.getUploadServer', 
                          params=params, timeout=10)
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response']['upload_url']

def get_wall_upload_server(access_token, group_id=None):
    """Получить сервер для загрузки на стену"""
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    response = requests.get('https://api.vk.com/method/photos.getWallUploadServer', 
                          params=params, timeout=10)
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response']['upload_url']

def upload_photo(upload_url, file_data, filename, is_wall=False):
    """Загрузить одно фото"""
    try:
        field_name = 'file1' if not is_wall else 'photo'
        files = {field_name: (filename, file_data, 'image/jpeg')}
        response = requests.post(upload_url, files=files, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {'error': str(e)}

def save_album_photo(access_token, server, photos_list, hash_value, album_id, group_id=None, description=""):
    """Сохранить фото в альбоме"""
    params = {
        'access_token': access_token,
        'v': VK_API_VERSION,
        'album_id': album_id,
        'server': server,
        'photos_list': photos_list,
        'hash': hash_value,
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    if description:
        params['caption'] = description[:100]  # Ограничиваем длину
    
    response = requests.get('https://api.vk.com/method/photos.save', params=params, timeout=15)
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
                           data=params, timeout=15)
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response'][0]

def create_comment(access_token, owner_id, photo_id, attachments, group_id=None):
    """Создать комментарий к фото"""
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
    
    response = requests.post('https://api.vk.com/method/photos.createComment', 
                           data=params, timeout=15)
    result = response.json()
    if 'error' in result:
        raise Exception(f"VK Error: {result['error']['error_msg']}")
    return result['response']

# ==================== ОСНОВНЫЕ МАРШРУТЫ ====================
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
            return jsonify({'success': False, 'error': 'Не найдены config.txt или CSV файл'}), 400
        
        config = parse_config(config_content)
        if 'ACCESS_TOKEN' not in config:
            return jsonify({'success': False, 'error': 'Нет ACCESS_TOKEN в config.txt'}), 400
        if 'ALBUM_ID' not in config:
            return jsonify({'success': False, 'error': 'Нет ALBUM_ID в config.txt'}), 400
        
        csv_data = parse_csv(csv_content)
        if not csv_data:
            return jsonify({'success': False, 'error': 'CSV файл пуст или имеет неверный формат'}), 400
        
        # Быстрый сбор уникальных файлов
        required_files = set()
        for row in csv_data:
            required_files.add(row['main_photo'])
            required_files.update(row['comment_photos'])
        
        # Предзагружаем URL в кэш
        session_id = str(int(time.time() * 1000))
        
        try:
            album_cache_key = f"album_{config['ACCESS_TOKEN'][:10]}_{config['ALBUM_ID']}_{config.get('GROUP_ID', '')}"
            album_url = get_cached_url(album_cache_key, get_album_upload_server, 
                                     config['ACCESS_TOKEN'], config['ALBUM_ID'], config.get('GROUP_ID'))
            
            wall_cache_key = f"wall_{config['ACCESS_TOKEN'][:10]}_{config.get('GROUP_ID', '')}"
            wall_url = get_cached_url(wall_cache_key, get_wall_upload_server, 
                                    config['ACCESS_TOKEN'], config.get('GROUP_ID'))
        except Exception as e:
            album_url = None
            wall_url = None
            print(f"⚠️ Не удалось предзагрузить URL: {e}")
        
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
    """Пакетная загрузка - ВСЕ ФОТО ОДНИМ ЗАПРОСОМ"""
    try:
        data = request.json
        session_id = data.get('session_id')
        row_index = data.get('row_index')
        files_data = data.get('files', [])
        
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        config = session_data.get('config', {})
        csv_data = session_data.get('csv_data', [])
        row = csv_data[row_index]
        
        print(f"\n🚀 Пакетная загрузка строки {row_index + 1}/{session_data['total_rows']}")
        print(f"📸 Главное: {row['main_photo']}")
        print(f"🖼️ Комментарии: {len(row['comment_photos'])} фото")
        
        # === 1. ПОЛУЧАЕМ URL ДЛЯ ЗАГРУЗКИ (из кэша или новые) ===
        album_cache_key = f"album_{config['ACCESS_TOKEN'][:10]}_{config['ALBUM_ID']}_{config.get('GROUP_ID', '')}"
        album_url = session_data['cached_urls'].get('album')
        if not album_url:
            album_url = get_cached_url(album_cache_key, get_album_upload_server, 
                                     config['ACCESS_TOKEN'], config['ALBUM_ID'], config.get('GROUP_ID'))
        
        wall_cache_key = f"wall_{config['ACCESS_TOKEN'][:10]}_{config.get('GROUP_ID', '')}"
        wall_url = session_data['cached_urls'].get('wall')
        if not wall_url:
            wall_url = get_cached_url(wall_cache_key, get_wall_upload_server, 
                                    config['ACCESS_TOKEN'], config.get('GROUP_ID'))
        
        # === 2. ПАРАЛЛЕЛЬНАЯ ЗАГРУЗКА ВСЕХ ФОТО ===
        upload_tasks = []
        file_map = {}
        
        # Подготавливаем главное фото
        main_file_data = None
        for f in files_data:
            if f['filename'] == row['main_photo']:
                main_file_data = base64.b64decode(f['data'].split(',')[1])
                upload_tasks.append((album_url, main_file_data, f['filename'], False))
                file_map[f['filename']] = 'main'
                break
        
        # Подготавливаем фото для комментариев
        comment_photos = []
        for comment_photo in row['comment_photos']:
            for f in files_data:
                if f['filename'] == comment_photo:
                    file_data = base64.b64decode(f['data'].split(',')[1])
                    upload_tasks.append((wall_url, file_data, f['filename'], True))
                    comment_photos.append(f['filename'])
                    file_map[f['filename']] = 'comment'
                    break
        
        # Параллельная загрузка
        print(f"⏫ Загрузка {len(upload_tasks)} фото параллельно...")
        start_upload = time.time()
        
        upload_results = []
        with ThreadPoolExecutor(max_workers=min(10, len(upload_tasks))) as executor:
            futures = []
            for task in upload_tasks:
                future = executor.submit(upload_photo, task[0], task[1], task[2], task[3])
                futures.append(future)
            
            for future in as_completed(futures):
                upload_results.append(future.result())
        
        print(f"✅ Загрузка завершена за {time.time() - start_upload:.1f}с")
        
        # === 3. СОХРАНЕНИЕ ФОТО (параллельно) ===
        main_photo_result = None
        comment_results = []
        errors = []
        
        # Разделяем результаты
        album_results = [r for r in upload_results if 'server' in r and 'photos_list' in r]
        wall_results = [r for r in upload_results if 'server' in r and 'photo' in r]
        
        # Сохраняем главное фото
        if album_results:
            try:
                main_photo_result = save_album_photo(
                    config['ACCESS_TOKEN'],
                    album_results[0]['server'],
                    album_results[0]['photos_list'],
                    album_results[0]['hash'],
                    config['ALBUM_ID'],
                    config.get('GROUP_ID'),
                    row['description']
                )
                main_photo_result = {
                    'id': main_photo_result['id'],
                    'owner_id': main_photo_result['owner_id'],
                    'name': row['main_photo']
                }
                print(f"✅ Главное фото сохранено, ID: {main_photo_result['id']}")
            except Exception as e:
                errors.append(f"Ошибка сохранения главного фото: {str(e)}")
        
        # Сохраняем фото для комментариев (параллельно)
        if wall_results:
            print(f"💾 Сохранение {len(wall_results)} фото комментариев...")
            
            def save_wall_photo_task(result, photo_name):
                try:
                    photo = save_wall_photo(
                        config['ACCESS_TOKEN'],
                        result['server'],
                        result['photo'],
                        result['hash'],
                        config.get('GROUP_ID')
                    )
                    return {
                        'photo_id': photo['id'],
                        'owner_id': photo['owner_id'],
                        'name': photo_name
                    }
                except Exception as e:
                    return {'error': str(e), 'name': photo_name}
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = []
                for i, result in enumerate(wall_results):
                    photo_name = comment_photos[i] if i < len(comment_photos) else f'photo_{i}'
                    futures.append(executor.submit(save_wall_photo_task, result, photo_name))
                
                for future in as_completed(futures):
                    result = future.result()
                    if 'error' in result:
                        errors.append(f"Ошибка сохранения {result['name']}: {result['error']}")
                    else:
                        comment_results.append(result)
            
            print(f"✅ Сохранено {len(comment_results)} фото комментариев")
        
        # === 4. СОЗДАНИЕ КОММЕНТАРИЯ ===
        comment_id = None
        if comment_results and main_photo_result and not errors:
            try:
                attachments = [f"photo{photo['owner_id']}_{photo['photo_id']}" 
                             for photo in comment_results]
                
                owner_id = -abs(int(config.get('GROUP_ID', main_photo_result['owner_id'])))
                
                comment_id = create_comment(
                    config['ACCESS_TOKEN'],
                    owner_id,
                    main_photo_result['id'],
                    attachments,
                    config.get('GROUP_ID')
                )
                print(f"✅ Комментарий создан, ID: {comment_id}")
            except Exception as e:
                errors.append(f"Ошибка создания комментария: {str(e)}")
        
        # === 5. СОХРАНЯЕМ РЕЗУЛЬТАТ ===
        result_data = {
            'row_index': row_index,
            'main_photo': row['main_photo'],
            'description': row['description'],
            'success': len(errors) == 0 and main_photo_result is not None,
            'main_photo_result': main_photo_result,
            'comment_results': comment_results,
            'comment_id': comment_id,
            'errors': errors,
            'upload_time': time.time() - start_upload
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
        print(f"❌ Ошибка пакетной загрузки: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/status/<session_id>', methods=['GET'])
def get_status(session_id):
    """Быстрый статус обработки"""
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
        'progress': f"{len(results)}/{total}"
    })

@app.route('/api/finalize/<session_id>', methods=['GET'])
def finalize(session_id):
    """Завершение сессии"""
    session_data = get_session(session_id)
    if not session_data:
        return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
    
    results = session_data.get('results', [])
    total = session_data.get('total_rows', 0)
    
    successful = sum(1 for r in results if r.get('success', False))
    failed = len(results) - successful
    
    elapsed = time.time() - session_data.get('start_time', time.time())
    
    # Очищаем данные
    delete_session(session_id)
    
    return jsonify({
        'success': True,
        'report': {
            'total': total,
            'processed': len(results),
            'successful': successful,
            'failed': failed,
            'time_elapsed': round(elapsed, 1),
            'avg_time_per_row': round(elapsed / len(results), 1) if results else 0
        }
    })

@app.route('/api/cancel/<session_id>', methods=['POST'])
def cancel(session_id):
    """Отмена сессии"""
    delete_session(session_id)
    return jsonify({'success': True})

# ==================== ЗАПУСК ====================
if __name__ == '__main__':
    os.makedirs('static', exist_ok=True)
    os.makedirs('templates', exist_ok=True)
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Запуск сервера на порту {port}")
    print(f"📁 Ожидаем файлы для загрузки в VK")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
