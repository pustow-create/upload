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
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        
        # Разбиваем по разделителю
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

# ==================== АНАЛИЗ ФАЙЛОВ (БЕЗ УЧЕТА РЕГИСТРА) ====================
def analyze_files(csv_data, uploaded_files):
    """Анализ наличия файлов - сравниваем имена без учета регистра"""
    
    # Создаем словарь с именами файлов в нижнем регистре
    uploaded_files_lower = {}
    for original_name, file_data in uploaded_files.items():
        lower_name = original_name.lower()
        uploaded_files_lower[lower_name] = {
            'original_name': original_name,
            'data': file_data['data'],
            'size': file_data['size']
        }
    
    # ВСЕ загруженные файлы (оригинальные имена)
    all_uploaded = list(uploaded_files.keys())
    
    # Требуемые файлы из CSV
    required_files = set()
    required_files_lower = set()
    
    for row in csv_data:
        if row['main_photo']:
            required_files.add(row['main_photo'])
            required_files_lower.add(row['main_photo'].lower())
        for photo in row['comment_photos']:
            if photo:
                required_files.add(photo)
                required_files_lower.add(photo.lower())
    
    # Находим какие файлы есть (сравниваем в нижнем регистре)
    found_files = []
    missing_files = []
    
    for req_file in required_files:
        req_lower = req_file.lower()
        if req_lower in uploaded_files_lower:
            found_files.append({
                'csv_name': req_file,
                'actual_name': uploaded_files_lower[req_lower]['original_name']
            })
        else:
            missing_files.append(req_file)
    
    # Лишние файлы (которых нет в required_files_lower)
    extra_files = []
    for uploaded_file in all_uploaded:
        if uploaded_file.lower() not in required_files_lower:
            extra_files.append(uploaded_file)
    
    print(f"\n=== АНАЛИЗ ФАЙЛОВ (без учета регистра) ===")
    print(f"Требуется файлов: {len(required_files)}")
    print(f"Загружено файлов: {len(all_uploaded)}")
    print(f"Найдено совпадений: {len(found_files)}")
    print(f"Отсутствуют: {len(missing_files)}")
    print(f"Лишние: {len(extra_files)}")
    
    if found_files:
        print("\nСОВПАДЕНИЯ:")
        for f in found_files[:10]:
            print(f"  {f['csv_name']} -> {f['actual_name']}")
    
    return {
        'required_files': list(required_files),
        'uploaded_files': all_uploaded,
        'missing_files': missing_files,
        'extra_files': extra_files[:50],  # Ограничиваем вывод
        'required_count': len(required_files),
        'uploaded_count': len(all_uploaded),
        'missing_count': len(missing_files),
        'extra_count': len(extra_files),
        'found_files': found_files,
        'all_required_present': len(missing_files) == 0
    }, uploaded_files_lower

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
        
        try:
            response = requests.post(f"{self.api_url}{method}", data=params, timeout=30)
            response.raise_for_status()
            result = response.json()
            
            if 'error' in result:
                error_msg = result['error'].get('error_msg', 'Unknown')
                raise Exception(f"VK Error: {error_msg}")
            
            return result['response']
        except Exception as e:
            raise Exception(f"VK API Error: {str(e)}")
    
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
        uploaded_files_original = {}  # Сохраняем с оригинальными именами
        config_content = None
        csv_content = None
        
        files_list = request.files.getlist('files')
        print(f"\n{'='*60}")
        print(f"ЗАГРУЖЕНО ФАЙЛОВ: {len(files_list)}")
        print(f"{'='*60}")
        
        for file in files_list:
            original_name = file.filename
            print(f"  Файл: '{original_name}'")
            
            # Ищем config.txt
            name_lower = original_name.lower()
            if name_lower == 'config.txt' or (name_lower.endswith('.txt') and 'config' in name_lower):
                config_content = file.read()
                print(f"  ✅ CONFIG: {original_name}")
                continue
            
            # Ищем CSV
            if name_lower.endswith('.csv'):
                csv_content = file.read()
                print(f"  ✅ CSV: {original_name}")
                continue
            
            # Это фото - сохраняем с ОРИГИНАЛЬНЫМ именем
            file.seek(0)
            file_data = io.BytesIO(file.read())
            file_data.seek(0)
            
            # Сохраняем с оригинальным именем
            uploaded_files_original[original_name] = {
                'data': file_data,
                'name': original_name,
                'size': len(file_data.getvalue())
            }
            
            # Также сохраняем в нижнем регистре для поиска
            lower_name = original_name.lower()
            uploaded_files[lower_name] = {
                'original_name': original_name,
                'data': file_data,
                'size': len(file_data.getvalue())
            }
            print(f"  📸 ФОТО: {original_name} -> сохранено как '{lower_name}'")
        
        # Проверяем config.txt
        if not config_content:
            print("\n❌ CONFIG.TXT НЕ НАЙДЕН!")
            return jsonify({
                'success': False, 
                'error': 'Не найден файл config.txt'
            }), 400
        
        # Проверяем CSV
        if not csv_content:
            print("\n❌ CSV ФАЙЛ НЕ НАЙДЕН!")
            return jsonify({
                'success': False, 
                'error': 'Не найден CSV файл'
            }), 400
        
        # Загружаем конфиг
        config = load_config_from_file(config_content)
        
        # Проверяем обязательные ключи
        if 'ACCESS_TOKEN' not in config:
            return jsonify({
                'success': False, 
                'error': 'В config.txt отсутствует ACCESS_TOKEN'
            }), 400
        
        if 'ALBUM_ID' not in config:
            return jsonify({
                'success': False, 
                'error': 'В config.txt отсутствует ALBUM_ID'
            }), 400
        
        # Парсим CSV
        csv_data = parse_csv_content(csv_content)
        
        if not csv_data:
            return jsonify({
                'success': False, 
                'error': 'CSV файл пуст'
            }), 400
        
        print(f"\n{'='*60}")
        print(f"ДАННЫЕ ИЗ CSV ({len(csv_data)} записей)")
        print(f"{'='*60}")
        for i, row in enumerate(csv_data):
            print(f"  Строка {i+1}:")
            print(f"    Основное: {row['main_photo']}")
            print(f"    Комментарии: {row['comment_photos']}")
        
        # Анализируем файлы (без учета регистра)
        analysis, uploaded_files_lower = analyze_files(csv_data, uploaded_files_original)
        
        # Обновляем uploaded_files в сессии - теперь с поиском по нижнему регистру
        session_uploaded_files = {}
        for lower_name, file_info in uploaded_files.items():
            session_uploaded_files[lower_name] = file_info
        
        print(f"\n{'='*60}")
        print(f"СЕССИЯ БУДЕТ СОЗДАНА")
        print(f"{'='*60}")
        print(f"  Файлов в сессии: {len(session_uploaded_files)}")
        
        # Создаем сессию
        session_id = str(int(time.time() * 1000))
        session_data = {
            'config': config,
            'csv_data': csv_data,
            'uploaded_files': session_uploaded_files,  # Ключи в нижнем регистре
            'uploaded_files_original': uploaded_files_original,  # Оригинальные имена
            'analysis': analysis,
            'current_row': 0,
            'results': [],
            'start_time': time.time()
        }
        
        set_session(session_id, session_data)
        print(f"\n✅ СЕССИЯ СОЗДАНА: {session_id}")
        print(f"{'='*60}\n")
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'total_rows': len(csv_data),
            'file_analysis': analysis
        })
        
    except Exception as e:
        print(f"\n❌ Ошибка инициализации: {str(e)}")
        import traceback
        traceback.print_exc()
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
        uploaded_files = session_data['uploaded_files']  # Ключи в нижнем регистре
        
        print(f"\n{'='*60}")
        print(f"ОБРАБОТКА СТРОКИ {row_index + 1}")
        print(f"{'='*60}")
        print(f"  Основное фото: '{row['main_photo']}'")
        print(f"  Поиск: '{row['main_photo'].lower()}'")
        print(f"  Фото в комментариях: {row['comment_photos']}")
        
        result = {
            'row_index': row_index,
            'main_photo': row['main_photo'],
            'description': row['description'],
            'success': False,
            'main_photo_result': None,
            'comment_results': [],
            'errors': []
        }
        
        # 1. ЗАГРУЗКА ОСНОВНОГО ФОТО - ИЩЕМ В НИЖНЕМ РЕГИСТРЕ
        main_photo = row['main_photo']
        main_photo_lower = main_photo.lower()
        
        print(f"\n  Поиск файла '{main_photo_lower}'...")
        
        if main_photo_lower in uploaded_files:
            file_info = uploaded_files[main_photo_lower]
            actual_name = file_info['original_name']
            print(f"  ✅ Файл НАЙДЕН: {actual_name}")
            
            try:
                # Инициализируем VK загрузчик
                uploader = VKUploader(
                    config['ACCESS_TOKEN'],
                    config.get('GROUP_ID')
                )
                
                # Получаем сервер для загрузки
                upload_server = uploader.get_album_upload_server(config['ALBUM_ID'])
                print(f"  Получен upload server")
                
                # Создаем копию файла для загрузки
                file_data = file_info['data']
                file_data.seek(0)
                
                upload_file = io.BytesIO(file_data.read())
                upload_file.seek(0)
                
                # Загружаем на сервер VK
                files = {'file1': (actual_name, upload_file, 'image/jpeg')}
                upload_response = requests.post(
                    upload_server['upload_url'],
                    files=files,
                    timeout=60
                )
                upload_response.raise_for_status()
                upload_result = upload_response.json()
                print(f"  Фото загружено на сервер VK")
                
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
                    print(f"  ✅ Основное фото ЗАГРУЖЕНО!")
                    
                    # 2. ЗАГРУЗКА ФОТО ДЛЯ КОММЕНТАРИЕВ
                    comment_photos = row['comment_photos']
                    
                    if comment_photos:
                        groups = split_into_groups(comment_photos, 2)
                        print(f"\n  Комментариев групп: {len(groups)}")
                        
                        for g_idx, group in enumerate(groups):
                            print(f"\n    Группа {g_idx + 1}: {group}")
                            group_result = {
                                'group_index': g_idx,
                                'photos': [],
                                'success': False
                            }
                            
                            for photo_name in group:
                                photo_lower = photo_name.lower()
                                print(f"      Поиск '{photo_lower}'...")
                                
                                if photo_lower in uploaded_files:
                                    photo_info_file = uploaded_files[photo_lower]
                                    photo_actual_name = photo_info_file['original_name']
                                    print(f"      ✅ Найден: {photo_actual_name}")
                                    
                                    try:
                                        wall_server = uploader.get_wall_upload_server()
                                        
                                        photo_data = photo_info_file['data']
                                        photo_data.seek(0)
                                        
                                        wall_file = io.BytesIO(photo_data.read())
                                        wall_file.seek(0)
                                        
                                        files = {'photo': (photo_actual_name, wall_file, 'image/jpeg')}
                                        wall_response = requests.post(
                                            wall_server['upload_url'],
                                            files=files,
                                            timeout=60
                                        )
                                        wall_response.raise_for_status()
                                        wall_result = wall_response.json()
                                        
                                        wall_save = uploader.save_wall_photo(
                                            wall_result['server'],
                                            wall_result['photo'],
                                            wall_result['hash']
                                        )
                                        
                                        if wall_save and len(wall_save) > 0:
                                            wall_info = wall_save[0]
                                            group_result['photos'].append({
                                                'name': photo_actual_name,
                                                'photo_id': wall_info['id'],
                                                'owner_id': wall_info['owner_id'],
                                                'vk_url': f"photo{wall_info['owner_id']}_{wall_info['id']}"
                                            })
                                            print(f"      ✅ Загружено")
                                        
                                        time.sleep(0.3)
                                        
                                    except Exception as e:
                                        error_msg = f"{photo_name}: {str(e)}"
                                        print(f"      ❌ {error_msg}")
                                        group_result['errors'] = group_result.get('errors', []) + [error_msg]
                                        result['errors'].append(error_msg)
                                else:
                                    error_msg = f"Файл {photo_name} не найден"
                                    print(f"      ❌ {error_msg}")
                                    group_result['errors'] = group_result.get('errors', []) + [error_msg]
                                    result['errors'].append(error_msg)
                            
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
                                    print(f"      ✅ Комментарий создан")
                                    
                                except Exception as e:
                                    error_msg = f"Ошибка комментария: {str(e)}"
                                    print(f"      ❌ {error_msg}")
                                    group_result['errors'] = group_result.get('errors', []) + [error_msg]
                                    result['errors'].append(error_msg)
                            
                            result['comment_results'].append(group_result)
                            time.sleep(0.5)
                
            except Exception as e:
                error_msg = f"Ошибка загрузки: {str(e)}"
                print(f"  ❌ {error_msg}")
                result['errors'].append(error_msg)
        else:
            error_msg = f"Файл {main_photo} не найден (искали '{main_photo_lower}')"
            print(f"  ❌ {error_msg}")
            result['errors'].append(error_msg)
            
            # Показываем доступные файлы для отладки
            print(f"\n  Доступные файлы (первые 10):")
            for i, name in enumerate(list(uploaded_files.keys())[:10]):
                print(f"    {i+1}. {name}")
        
        # Сохраняем результат
        session_data['results'].append(result)
        session_data['current_row'] = row_index + 1
        set_session(session_id, session_data)
        
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
        print(f"\n❌ Ошибка обработки строки {row_index}: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ТЕСТ VK ====================
@app.route('/api/test-vk', methods=['POST'])
def test_vk():
    try:
        for file in request.files.getlist('files'):
            name_lower = file.filename.lower()
            if name_lower == 'config.txt' or (name_lower.endswith('.txt') and 'config' in name_lower):
                config = load_config_from_file(file.read())
                token = config.get('ACCESS_TOKEN')
                
                if not token:
                    return jsonify({'success': False, 'error': 'Нет ACCESS_TOKEN'}), 400
                
                response = requests.post(
                    'https://api.vk.com/method/users.get',
                    data={'access_token': token, 'v': VK_API_VERSION},
                    timeout=10
                )
                result = response.json()
                
                if 'error' in result:
                    return jsonify({'success': False, 'error': result['error']['error_msg']}), 400
                
                return jsonify({
                    'success': True,
                    'message': 'Подключение к VK успешно',
                    'user_info': result['response'][0]
                })
        
        return jsonify({'success': False, 'error': 'Не найден config.txt'}), 400
        
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
        
        return jsonify({'success': True, 'report': report})
        
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
