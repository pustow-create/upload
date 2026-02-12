@app.route('/api/upload-batch', methods=['POST'])
def upload_batch():
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'Нет данных'}), 400
        
        # ДИАГНОСТИКА: выводим что пришло
        print(f"\n📦 ПОЛУЧЕН ЗАПРОС:")
        print(f"  session_id: {data.get('session_id')}")
        print(f"  row_index: {data.get('row_index')}")
        print(f"  files тип: {type(data.get('files'))}")
        print(f"  files: {data.get('files')}")
        
        session_id = data.get('session_id')
        row_index = data.get('row_index')
        
        # ВАЖНО: проверяем что files это список и он не undefined
        files = data.get('files')
        if files is None:
            files = []
        if not isinstance(files, list):
            files = []
        
        # Дополнительная проверка
        if len(files) == 0:
            print("⚠️ ВНИМАНИЕ: files - пустой список!")
        
        session_data = get_session(session_id)
        if not session_data:
            return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
        
        config = session_data.get('config', {})
        csv_data = session_data.get('csv_data', [])
        
        if row_index >= len(csv_data):
            return jsonify({'success': False, 'error': 'Неверный индекс строки'}), 400
            
        row = csv_data[row_index]
        
        print(f"\n🚀 Строка {row_index + 1}/{session_data['total_rows']}")
        print(f"📸 Главное фото: {row['main_photo']}")
        print(f"🖼️ Фото для комментария: {len(row['comment_photos'])}")
        print(f"📦 Получено файлов: {len(files)}")
        
        # === ПОЛУЧАЕМ URL ===
        try:
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
        except Exception as e:
            print(f"❌ Ошибка получения URL: {e}")
            return jsonify({'success': False, 'error': f'Ошибка получения URL: {str(e)}'}), 500
        
        # === ПОДГОТОВКА К ЗАГРУЗКЕ ===
        upload_tasks = []
        main_file_found = False
        comment_files_found = []
        
        # Ищем главное фото - с проверкой на undefined
        for f in files:
            # Важно: проверяем что f это словарь и у него есть поле filename
            if not isinstance(f, dict):
                print(f"⚠️ Пропускаем не-словарь: {f}")
                continue
                
            filename = f.get('filename')
            file_data = f.get('data')
            
            if not filename or not file_data:
                print(f"⚠️ Пропускаем файл без имени или данных: {f}")
                continue
                
            if filename == row['main_photo']:
                try:
                    # Проверяем формат data URL
                    if ',' in file_data:
                        base64_data = file_data.split(',')[1]
                    else:
                        base64_data = file_data
                    
                    file_data_binary = base64.b64decode(base64_data)
                    upload_tasks.append((album_url, file_data_binary, filename, False))
                    main_file_found = True
                    print(f"✅ Главное фото найдено: {filename}")
                except Exception as e:
                    print(f"❌ Ошибка декодирования главного фото {filename}: {e}")
                break
        
        # Ищем фото для комментариев
        for comment_photo in row['comment_photos']:
            for f in files:
                if not isinstance(f, dict):
                    continue
                    
                filename = f.get('filename')
                file_data = f.get('data')
                
                if not filename or not file_data:
                    continue
                    
                if filename == comment_photo:
                    try:
                        if ',' in file_data:
                            base64_data = file_data.split(',')[1]
                        else:
                            base64_data = file_data
                            
                        file_data_binary = base64.b64decode(base64_data)
                        upload_tasks.append((wall_url, file_data_binary, filename, True))
                        comment_files_found.append(filename)
                        print(f"✅ Фото комментария найдено: {filename}")
                    except Exception as e:
                        print(f"❌ Ошибка декодирования фото комментария {filename}: {e}")
                    break
        
        if not main_file_found:
            return jsonify({
                'success': False, 
                'error': f'Не найдено главное фото: {row["main_photo"]}. Доступны: {[f.get("filename") for f in files if isinstance(f, dict)]}'
            }), 400
        
        # === ПАРАЛЛЕЛЬНАЯ ЗАГРУЗКА ===
        print(f"⏫ Загрузка {len(upload_tasks)} фото...")
        upload_results = []
        upload_errors = []
        
        with ThreadPoolExecutor(max_workers=min(10, len(upload_tasks))) as executor:
            futures = []
            for task in upload_tasks:
                future = executor.submit(upload_photo, task[0], task[1], task[2], task[3])
                futures.append(future)
            
            for future in as_completed(futures):
                result = future.result()
                if 'error' in result:
                    upload_errors.append(result['error'])
                else:
                    upload_results.append(result)
        
        print(f"✅ Загружено: {len(upload_results)}/{len(upload_tasks)}")
        if upload_errors:
            print(f"❌ Ошибки загрузки: {upload_errors}")
        
        if not upload_results:
            return jsonify({'success': False, 'error': 'Не удалось загрузить ни одного фото', 'details': upload_errors}), 500
        
        # === СОХРАНЕНИЕ ФОТО ===
        main_photo_result = None
        comment_results = []
        errors = []
        
        # Сохраняем главное фото
        album_save_results = [r for r in upload_results if 'photos_list' in r]
        if album_save_results:
            try:
                photo = save_album_photo(
                    config['ACCESS_TOKEN'],
                    album_save_results[0]['server'],
                    album_save_results[0]['photos_list'],
                    album_save_results[0]['hash'],
                    config['ALBUM_ID'],
                    config.get('GROUP_ID'),
                    row['description']
                )
                main_photo_result = {
                    'id': photo['id'],
                    'owner_id': photo['owner_id'],
                    'name': row['main_photo']
                }
                print(f"✅ Главное фото сохранено: ID {photo['id']}")
            except Exception as e:
                errors.append(f"Ошибка сохранения главного фото: {str(e)}")
        
        # Сохраняем фото для комментариев
        wall_save_results = [r for r in upload_results if 'photo' in r]
        for i, result in enumerate(wall_save_results):
            try:
                photo_name = comment_files_found[i] if i < len(comment_files_found) else f'comment_{i}'
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
                    'name': photo_name
                })
                print(f"✅ Фото комментария сохранено: ID {photo['id']}")
            except Exception as e:
                errors.append(f"Ошибка сохранения фото комментария {photo_name}: {str(e)}")
        
        # === СОЗДАНИЕ КОММЕНТАРИЯ ===
        comment_id = None
        if comment_results and main_photo_result and not errors:
            try:
                attachments = []
                for photo in comment_results:
                    attachments.append(f"photo{photo['owner_id']}_{photo['photo_id']}")
                
                owner_id = main_photo_result['owner_id']
                if config.get('GROUP_ID'):
                    owner_id = -abs(int(config['GROUP_ID']))
                
                comment_id = create_comment(
                    config['ACCESS_TOKEN'],
                    owner_id,
                    main_photo_result['id'],
                    attachments,
                    config.get('GROUP_ID')
                )
                print(f"✅ Комментарий создан: ID {comment_id}")
            except Exception as e:
                errors.append(f"Ошибка создания комментария: {str(e)}")
        
        # === СОХРАНЕНИЕ РЕЗУЛЬТАТА ===
        result_data = {
            'row_index': row_index,
            'main_photo': row['main_photo'],
            'description': row['description'][:50] + '...' if len(row['description']) > 50 else row['description'],
            'success': len(errors) == 0 and main_photo_result is not None,
            'main_photo_result': main_photo_result,
            'comment_results': comment_results,
            'comment_id': comment_id,
            'errors': errors,
            'upload_stats': {
                'total_files': len(upload_tasks),
                'uploaded': len(upload_results),
                'failed': len(upload_errors)
            }
        }
        
        session_data['results'].append(result_data)
        session_data['current_row'] = row_index + 1
        set_session(session_id, session_data)
        
        return jsonify({
            'success': True,
            'result': result_data,
            'progress': {
                'current': session_data['current_row'],
                'total': session_data['total_rows']
            }
        })
        
    except Exception as e:
        print(f"❌ Ошибка upload-batch: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# Добавляем диагностический эндпоинт
@app.route('/api/debug-session/<session_id>', methods=['GET'])
def debug_session(session_id):
    """Диагностика сессии"""
    session_data = get_session(session_id)
    if not session_data:
        return jsonify({'success': False, 'error': 'Сессия не найдена'}), 404
    
    # Безопасно получаем данные
    csv_data = session_data.get('csv_data', [])
    results = session_data.get('results', [])
    
    # Первые 3 строки CSV для диагностики
    sample_rows = []
    for i, row in enumerate(csv_data[:3]):
        sample_rows.append({
            'index': i,
            'main_photo': row.get('main_photo', ''),
            'description': row.get('description', '')[:30],
            'comment_photos_count': len(row.get('comment_photos', []))
        })
    
    return jsonify({
        'success': True,
        'session_id': session_id,
        'total_rows': session_data.get('total_rows', 0),
        'processed_rows': len(results),
        'current_row': session_data.get('current_row', 0),
        'sample_rows': sample_rows,
        'has_cached_urls': bool(session_data.get('cached_urls')),
        'timestamp': session_data.get('_timestamp', 0)
    })
