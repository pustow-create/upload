import os
import io
import csv
import json
import requests
from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
import tempfile
from werkzeug.utils import secure_filename
import time

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-123")
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB max
CORS(app)

# Конфигурация VK API
VK_API_VERSION = "5.199"
VK_ALBUM_ID = None
VK_ACCESS_TOKEN = None
VK_GROUP_ID = None

def load_config_from_file(config_content):
    """Парсинг конфигурации из текста config.txt"""
    config = {}
    lines = config_content.decode('utf-8').split('\n')
    for line in lines:
        line = line.strip()
        if line and '=' in line:
            key, value = line.split('=', 1)
            config[key.strip()] = value.strip()
    return config

def parse_csv_content(csv_content, delimiter='|'):
    """Парсинг CSV с определением разделителя"""
    # Удаляем BOM если есть
    content = csv_content.decode('utf-8-sig')
    
    # Пропускаем строку sep=|
    lines = content.strip().split('\n')
    if lines[0].startswith('sep='):
        delimiter = lines[0].split('=')[1]
        lines = lines[1:]
    
    # Парсим CSV
    csv_data = []
    for line in lines:
        if line.strip():
            # Убираем лишние пробелы
            parts = [part.strip() for part in line.split(delimiter)]
            if len(parts) >= 2:
                csv_data.append({
                    'main_photo': parts[0],
                    'description': parts[1] if len(parts) > 1 else '',
                    'comment_photos': parts[2].split(';') if len(parts) > 2 and parts[2] else []
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
        
        response = requests.post(f"{self.api_url}{method}", data=params)
        result = response.json()
        
        if 'error' in result:
            raise Exception(f"VK API Error: {result['error']}")
        
        return result['response']
    
    def get_album_upload_server(self, album_id):
        """Получить сервер для загрузки фото в альбом"""
        params = {
            'album_id': album_id
        }
        if self.group_id:
            params['group_id'] = abs(self.group_id)  # ID группы отрицательный
        
        return self._call_api('photos.getUploadServer', params)
    
    def get_wall_upload_server(self):
        """Получить сервер для загрузки фото на стену (для комментариев)"""
        params = {}
        if self.group_id:
            params['group_id'] = abs(self.group_id)
        
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
            params['group_id'] = abs(self.group_id)
        
        return self._call_api('photos.save', params)
    
    def save_wall_photo(self, server, photo, hash_value):
        """Сохранить фото для стены (комментариев)"""
        params = {
            'server': server,
            'photo': photo,
            'hash': hash_value
        }
        if self.group_id:
            params['group_id'] = abs(self.group_id)
        
        return self._call_api('photos.saveWallPhoto', params)
    
    def create_album_comment(self, owner_id, photo_id, message="", attachments=None):
        """Создать комментарий к фото в альбоме"""
        params = {
            'owner_id': owner_id,
            'photo_id': photo_id,
            'message': message
        }
        
        if attachments:
            params['attachments'] = ','.join(attachments)
        
        return self._call_api('photos.createComment', params)

def process_upload_stream(file_storage):
    """Обработка файла в потоковом режиме без сохранения на диск"""
    # Используем временный файл в памяти
    temp_file = tempfile.SpooledTemporaryFile(max_size=10*1024*1024)  # 10MB в памяти
    
    # Копируем поток
    chunk_size = 8192
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

@app.route('/api/check-files', methods=['POST'])
def check_files():
    """Проверка наличия необходимых файлов"""
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
            elif filename.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                photo_files[file_storage.filename] = file_storage
        
        # Проверяем наличие обязательных файлов
        if not config_file:
            return jsonify({'error': 'Не найден файл config.txt'}), 400
        
        if not csv_file:
            return jsonify({'error': 'Не найден CSV файл'}), 400
        
        return jsonify({
            'success': True,
            'config_found': True,
            'csv_found': True,
            'photo_count': len(photo_files)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/start-upload', methods=['POST'])
def start_upload():
    """Начало процесса загрузки"""
    try:
        # Инициализируем сессию
        session.clear()
        
        # Получаем все файлы
        uploaded_files = request.files.getlist('files')
        
        # Разделяем файлы
        config_content = None
        csv_content = None
        files_dict = {}
        
        for file_storage in uploaded_files:
            filename = file_storage.filename
            
            if filename.lower() == 'config.txt':
                config_content = file_storage.read()
            elif filename.lower().endswith('.csv'):
                csv_content = file_storage.read()
            else:
                # Сохраняем файлы в памяти для быстрого доступа
                files_dict[filename] = file_storage
        
        if not config_content or not csv_content:
            return jsonify({'error': 'Не найдены config.txt или CSV файл'}), 400
        
        # Загружаем конфиг
        config = load_config_from_file(config_content)
        
        global VK_ACCESS_TOKEN, VK_GROUP_ID, VK_ALBUM_ID
        
        VK_ACCESS_TOKEN = config.get('ACCESS_TOKEN')
        VK_GROUP_ID = int(config.get('GROUP_ID', 0))
        VK_ALBUM_ID = int(config.get('ALBUM_ID', 0))
        
        if not VK_ACCESS_TOKEN:
            return jsonify({'error': 'В config.txt не указан ACCESS_TOKEN'}), 400
        
        # Парсим CSV
        csv_data = parse_csv_content(csv_content)
        
        # Сохраняем данные в сессии
        session['csv_data'] = csv_data
        session['files_dict_keys'] = list(files_dict.keys())
        session['current_row'] = 0
        session['uploaded_main_photos'] = {}
        session['uploaded_comment_photos'] = {}
        
        # Сохраняем сами файлы в сессии (имя -> файловый объект)
        # Важно: храним только имена, сами файлы будут передаваться в каждом запросе
        session['files_info'] = {name: {'size': 0} for name in files_dict.keys()}
        
        return jsonify({
            'success': True,
            'total_rows': len(csv_data),
            'total_files': len(files_dict)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload-row', methods=['POST'])
def upload_row():
    """Обработка одной строки CSV"""
    try:
        row_index = request.form.get('row_index', type=int)
        if row_index is None:
            return jsonify({'error': 'Не указан row_index'}), 400
        
        # Получаем данные из сессии
        csv_data = session.get('csv_data', [])
        if row_index >= len(csv_data):
            return jsonify({'error': 'Неверный индекс строки'}), 400
        
        row = csv_data[row_index]
        
        # Получаем файлы из запроса
        uploaded_files = request.files.getlist('files')
        files_dict = {f.filename: f for f in uploaded_files}
        
        # Инициализируем VKUploader
        uploader = VKUploader(VK_ACCESS_TOKEN, VK_GROUP_ID)
        
        results = {
            'row_index': row_index,
            'main_photo': None,
            'comment_groups': []
        }
        
        # 1. Загружаем основное фото в альбом
        main_photo_name = row['main_photo']
        if main_photo_name in files_dict:
            print(f"Загрузка основного фото: {main_photo_name}")
            
            # Получаем сервер для загрузки в альбом
            upload_server_info = uploader.get_album_upload_server(VK_ALBUM_ID)
            
            # Загружаем файл на сервер VK
            file_storage = files_dict[main_photo_name]
            temp_file = process_upload_stream(file_storage)
            
            files = {'file1': (main_photo_name, temp_file, 'image/jpeg')}
            upload_response = requests.post(upload_server_info['upload_url'], files=files)
            upload_result = upload_response.json()
            
            # Сохраняем фото в альбоме
            save_result = uploader.save_album_photo(
                upload_result['server'],
                upload_result['photos_list'],
                upload_result['hash'],
                VK_ALBUM_ID
            )
            
            if save_result:
                photo_info = save_result[0]
                owner_id = photo_info['owner_id']
                photo_id = photo_info['id']
                
                results['main_photo'] = {
                    'owner_id': owner_id,
                    'photo_id': photo_id,
                    'name': main_photo_name
                }
                
                print(f"Основное фото загружено: photo{owner_id}_{photo_id}")
        
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
                        
                        # Получаем сервер для загрузки на стену
                        upload_server_info = uploader.get_wall_upload_server()
                        
                        # Загружаем файл
                        file_storage = files_dict[photo_name]
                        temp_file = process_upload_stream(file_storage)
                        
                        files = {'photo': (photo_name, temp_file, 'image/jpeg')}
                        upload_response = requests.post(upload_server_info['upload_url'], files=files)
                        upload_result = upload_response.json()
                        
                        # Сохраняем фото для стены
                        save_result = uploader.save_wall_photo(
                            upload_result['server'],
                            upload_result['photo'],
                            upload_result['hash']
                        )
                        
                        if save_result:
                            photo_info = save_result[0]
                            group_photos.append({
                                'owner_id': photo_info['owner_id'],
                                'photo_id': photo_info['id'],
                                'name': photo_name
                            })
                
                if group_photos:
                    results['comment_groups'].append({
                        'group_id': group_index,
                        'photos': group_photos
                    })
        
        # Обновляем сессию
        if 'uploaded_main_photos' not in session:
            session['uploaded_main_photos'] = {}
        
        if results['main_photo']:
            session['uploaded_main_photos'][str(row_index)] = results['main_photo']
        
        session.modified = True
        
        return jsonify(results)
    
    except Exception as e:
        print(f"Ошибка при загрузке строки {row_index}: {str(e)}")
        return jsonify({'error': str(e), 'row_index': row_index}), 500

@app.route('/api/create-comments', methods=['POST'])
def create_comments():
    """Создание комментариев для загруженных фото"""
    try:
        row_index = request.form.get('row_index', type=int)
        if row_index is None:
            return jsonify({'error': 'Не указан row_index'}), 400
        
        # Получаем данные из сессии
        csv_data = session.get('csv_data', [])
        uploaded_main_photos = session.get('uploaded_main_photos', {})
        
        if row_index >= len(csv_data):
            return jsonify({'error': 'Неверный индекс строки'}), 400
        
        row = csv_data[row_index]
        main_photo_info = uploaded_main_photos.get(str(row_index))
        
        if not main_photo_info:
            return jsonify({'error': 'Основное фото не загружено'}), 400
        
        # Инициализируем VKUploader
        uploader = VKUploader(VK_ACCESS_TOKEN, VK_GROUP_ID)
        
        # Получаем данные из запроса
        comment_groups_data = request.form.get('comment_groups')
        if comment_groups_data:
            comment_groups = json.loads(comment_groups_data)
        else:
            comment_groups = []
        
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
                            message="",  # Пустое сообщение, только фото
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
            'comments_created': len(created_comments)
        })
    
    except Exception as e:
        print(f"Ошибка при создании комментариев: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/get-progress', methods=['GET'])
def get_progress():
    """Получение текущего прогресса"""
    current_row = session.get('current_row', 0)
    total_rows = len(session.get('csv_data', []))
    
    return jsonify({
        'current_row': current_row,
        'total_rows': total_rows,
        'progress': (current_row / total_rows * 100) if total_rows > 0 else 0
    })

@app.route('/api/reset', methods=['POST'])
def reset():
    """Сброс сессии"""
    session.clear()
    return jsonify({'success': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
