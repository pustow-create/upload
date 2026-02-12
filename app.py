import os
import csv
import json
import requests
import time
import threading
from flask import Flask, render_template, request, jsonify
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

# ==================== ОПТИМИЗАЦИЯ ====================
def create_session():
    session = requests.Session()
    retry = Retry(total=2, read=2, connect=2, backoff_factor=0.2)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount('https://', adapter)
    return session

vk_session = create_session()
upload_session = create_session()

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

# ==================== ПАРСИНГ ====================
def parse_config(content):
    config = {}
    if isinstance(content, bytes):
        content = content.decode('utf-8', errors='ignore')
    
    for line in content.split('\n'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        config[key.strip().upper()] = value.strip()
    return config

def parse_csv(content):
    if isinstance(content, bytes):
        for enc in ['windows-1251', 'utf-8-sig', 'utf-8']:
            try:
                content = content.decode(enc)
                print(f"✅ CSV: {enc}")
                break
            except UnicodeDecodeError:
                continue
    
    lines = [line.rstrip('\r') for line in content.split('\n') if line.strip()]
    if not lines:
        return []
    
    delimiter = '|'
    start = 0
    
    if lines[0].startswith('sep='):
        delimiter = lines[0].split('=')[1].strip()
        start = 1
    
    if start < len(lines) and 'файл' in lines[start].lower():
        start += 1
    
    data = []
    for i in range(start, len(lines)):
        parts = lines[i].strip().split(delimiter)
        if len(parts) >= 1 and parts[0].strip():
            main_photo = parts[0].strip()
            description = parts[1].strip() if len(parts) > 1 else ''
            comment_photos = parts[2].strip().split(';') if len(parts) > 2 and parts[2].strip() else []
            comment_photos = [p.strip() for p in comment_photos if p.strip()]
            
            data.append({
                'main_photo': main_photo,
                'description': description,
                'comment_photos': comment_photos
            })
    
    print(f"📊 Записей: {len(data)}")
    return data

# ==================== VK API ====================
def proxy_get_upload_server(access_token, album_id, group_id=None):
    params = {'access_token': access_token, 'v': VK_API_VERSION, 'album_id': album_id}
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    r = vk_session.get('https://api.vk.com/method/photos.getUploadServer', params=params, timeout=30)
    return r.json()['response']['upload_url']

def proxy_get_wall_upload_server(access_token, group_id=None):
    params = {'access_token': access_token, 'v': VK_API_VERSION}
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    r = vk_session.get('https://api.vk.com/method/photos.getWallUploadServer', params=params, timeout=30)
    return r.json()['response']['upload_url']

def proxy_upload_to_album(upload_url, file_data, filename):
    r = upload_session.post(upload_url, files={'file1': (filename, file_data, 'image/jpeg')}, timeout=60)
    return r.json()

def proxy_upload_to_wall(upload_url, file_data, filename):
    r = upload_session.post(upload_url, files={'photo': (filename, file_data, 'image/jpeg')}, timeout=60)
    return r.json()

def proxy_save_album_photo(access_token, server, photos_list, hash_value, album_id, group_id=None, description=""):
    params = {
        'access_token': access_token, 'v': '5.131', 'album_id': album_id,
        'server': server, 'photos_list': photos_list, 'hash': hash_value,
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    if description.strip():
        params['caption'] = description.strip()
    
    r = vk_session.get('https://api.vk.com/method/photos.save', params=params, timeout=30)
    return r.json()['response']

def proxy_save_wall_photo(access_token, server, photo, hash_value, group_id=None):
    params = {
        'access_token': access_token, 'v': VK_API_VERSION,
        'server': server, 'photo': photo, 'hash': hash_value
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    r = vk_session.post('https://api.vk.com/method/photos.saveWallPhoto', data=params, timeout=30)
    return r.json()['response']

def proxy_create_comment(access_token, owner_id, photo_id, attachments, group_id=None):
    if group_id:
        owner_id = -abs(int(group_id))
    
    params = {
        'access_token': access_token, 'v': VK_API_VERSION,
        'owner_id': owner_id, 'photo_id': photo_id,
        'attachments': ','.join(attachments), 'from_group': 1
    }
    if group_id:
        params['group_id'] = abs(int(group_id))
    
    r = vk_session.post('https://api.vk.com/method/photos.createComment', data=params, timeout=30)
    return {'comment_id': r.json()['response']}

# ==================== МАРШРУТЫ ====================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/api/test-vk', methods=['POST'])
def test_vk():
    try:
        for f in request.files.getlist('files'):
            if f.filename.lower() == 'config.txt':
                config = parse_config(f.read())
                token = config.get('ACCESS_TOKEN')
                if token:
                    r = vk_session.get('https://api.vk.com/method/users.get', 
                                      params={'access_token': token, 'v': VK_API_VERSION}, timeout=10)
                    user = r.json()['response'][0]
                    return jsonify({'success': True, 'user': user})
        return jsonify({'success': False, 'error': 'Config not found'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/analyze', methods=['POST'])
def analyze():
    try:
        config_data = None
        csv_data = None
        
        for f in request.files.getlist('files'):
            name = f.filename.lower()
            if name == 'config.txt':
                config_data = f.read()
            elif name.endswith('.csv'):
                csv_data = f.read()
        
        if not config_data or not csv_data:
            return jsonify({'success': False, 'error': 'Нет config.txt или CSV'}), 400
        
        config = parse_config(config_data)
        if 'ACCESS_TOKEN' not in config or 'ALBUM_ID' not in config:
            return jsonify({'success': False, 'error': 'Нет токена или альбома'}), 400
        
        rows = parse_csv(csv_data)
        if not rows:
            return jsonify({'success': False, 'error': 'CSV пуст'}), 400
        
        files = set()
        for r in rows:
            files.add(r['main_photo'])
            files.update(r['comment_photos'])
        
        sid = str(int(time.time() * 1000))
        set_session(sid, {
            'config': config,
            'csv_data': rows,
            'required_files': list(files),
            'total_rows': len(rows),
            'uploaded_files': set(),
            'start_time': time.time()
        })
        
        return jsonify({'success': True, 'session_id': sid, 'total_rows': len(rows)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get-upload-urls/<sid>/<int:idx>', methods=['GET'])
def get_upload_urls(sid, idx):
    try:
        sess = get_session(sid)
        if not sess:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        row = sess['csv_data'][idx]
        cfg = sess['config']
        
        album_url = proxy_get_upload_server(cfg['ACCESS_TOKEN'], cfg['ALBUM_ID'], cfg.get('GROUP_ID'))
        wall_url = proxy_get_wall_upload_server(cfg['ACCESS_TOKEN'], cfg.get('GROUP_ID'))
        
        groups = []
        for i in range(0, len(row['comment_photos']), 2):
            groups.append({'group': row['comment_photos'][i:i+2], 'upload_url': wall_url})
        
        return jsonify({
            'success': True,
            'row_index': idx,
            'description': row['description'],
            'main_photo': {'filename': row['main_photo'], 'upload_url': album_url},
            'comment_groups': groups
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/proxy/upload-album', methods=['POST'])
def upload_album():
    try:
        fid = request.form.get('session_id')
        name = request.form.get('filename')
        url = request.form.get('upload_url')
        desc = request.form.get('description', '')
        
        sess = get_session(fid)
        if not sess or 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Ошибка'}), 400
        
        up = proxy_upload_to_album(url, request.files['file'].read(), name)
        sv = proxy_save_album_photo(sess['config']['ACCESS_TOKEN'], up['server'], 
                                   up['photos_list'], up['hash'], sess['config']['ALBUM_ID'],
                                   sess['config'].get('GROUP_ID'), desc)
        
        with session_lock:
            sess.setdefault('uploaded_files', set()).add(name)
        
        return jsonify({'success': True, 'photo': sv[0]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/proxy/upload-wall', methods=['POST'])
def upload_wall():
    try:
        fid = request.form.get('session_id')
        name = request.form.get('filename')
        url = request.form.get('upload_url')
        
        sess = get_session(fid)
        if not sess or 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Ошибка'}), 400
        
        up = proxy_upload_to_wall(url, request.files['file'].read(), name)
        sv = proxy_save_wall_photo(sess['config']['ACCESS_TOKEN'], up['server'],
                                  up['photo'], up['hash'], sess['config'].get('GROUP_ID'))
        
        with session_lock:
            sess.setdefault('uploaded_files', set()).add(name)
        
        return jsonify({'success': True, 'photo': sv[0]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/proxy/create-comment', methods=['POST'])
def create_comment():
    try:
        data = request.json
        sess = get_session(data['session_id'])
        if not sess:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        cfg = sess['config']
        res = proxy_create_comment(cfg['ACCESS_TOKEN'], data['owner_id'], 
                                  data['photo_id'], data.get('attachments', []), cfg.get('GROUP_ID'))
        return jsonify({'success': True, 'comment_id': res['comment_id']})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/save-result', methods=['POST'])
def save_result():
    try:
        data = request.json
        sess = get_session(data['session_id'])
        if not sess:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        row = sess['csv_data'][data['row_index']]
        uploaded = {row['main_photo']} if data.get('main_photo_result') else set()
        
        for c in data.get('comment_results', []):
            uploaded.update(p['name'] for p in c.get('photos', []))
        
        with session_lock:
            sess.setdefault('uploaded_files', set()).update(uploaded)
            sess.setdefault('results', []).append({
                'row_index': data['row_index'],
                'main_photo': row['main_photo'],
                'success': not data.get('errors') and data.get('main_photo_result') is not None,
                'uploaded': list(uploaded)
            })
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/finalize/<sid>', methods=['GET'])
def finalize(sid):
    try:
        sess = get_session(sid)
        if not sess:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        uploaded = sess.get('uploaded_files', set())
        required = set(sess.get('required_files', []))
        missing = required - uploaded
        
        return jsonify({'success': True, 'report': {
            'statistics': {
                'total_rows': len(sess.get('csv_data', [])),
                'processed_rows': len(sess.get('results', [])),
                'total_time': f"{time.time() - sess.get('start_time', 0):.1f}с"
            },
            'files': {
                'required_count': len(required),
                'uploaded_count': len(uploaded),
                'missing_count': len(missing),
                'missing_files': sorted(missing)[:50]
            }
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/cancel/<sid>', methods=['POST'])
def cancel(sid):
    delete_session(sid)
    return jsonify({'success': True})

# ==================== ЗАПУСК ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Сервер запущен на порту {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
