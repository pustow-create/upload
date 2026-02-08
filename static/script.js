// static/script.js
document.addEventListener('DOMContentLoaded', function() {
    console.log('VK Photo Uploader loaded');
    
    // Обработка выбора файлов
    const fileInputs = document.querySelectorAll('input[type="file"]');
    
    fileInputs.forEach(input => {
        input.addEventListener('change', function(e) {
            const fileName = e.target.files[0] 
                ? e.target.files[0].name 
                : 'Выберите файл';
            
            // Находим ближайший span для отображения имени файла
            const label = this.closest('.file-label');
            if (label) {
                const span = label.querySelector('span');
                if (span) {
                    span.textContent = fileName;
                }
            }
        });
    });
    
    // Обработка множественного выбора фотографий
    const photoInput = document.getElementById('photo_files');
    if (photoInput) {
        photoInput.addEventListener('change', function(e) {
            const fileList = document.getElementById('fileList');
            if (fileList) {
                fileList.innerHTML = '';
                
                if (e.target.files.length > 0) {
                    const list = document.createElement('ul');
                    list.style.listStyle = 'none';
                    list.style.paddingLeft = '0';
                    list.style.marginTop = '10px';
                    
                    const maxFilesToShow = 5;
                    const filesToShow = Math.min(e.target.files.length, maxFilesToShow);
                    
                    for (let i = 0; i < filesToShow; i++) {
                        const li = document.createElement('li');
                        li.textContent = `📷 ${e.target.files[i].name}`;
                        li.style.padding = '5px 0';
                        li.style.borderBottom = '1px solid #eee';
                        list.appendChild(li);
                    }
                    
                    if (e.target.files.length > maxFilesToShow) {
                        const li = document.createElement('li');
                        li.textContent = `... и ещё ${e.target.files.length - maxFilesToShow} файлов`;
                        li.style.padding = '5px 0';
                        li.style.color = '#666';
                        list.appendChild(li);
                    }
                    
                    fileList.appendChild(list);
                }
            }
        });
    }
    
    // Обработка отправки формы
    const uploadForm = document.getElementById('uploadForm');
    if (uploadForm) {
        uploadForm.addEventListener('submit', function() {
            const submitBtn = document.getElementById('submitBtn');
            const loading = document.getElementById('loading');
            
            if (submitBtn) submitBtn.style.display = 'none';
            if (loading) loading.style.display = 'flex';
        });
    }
    
    // Логика для страницы результатов
    if (window.location.pathname.includes('/result')) {
        checkUploadStatus();
    }
});

function checkUploadStatus() {
    const sessionId = document.querySelector('meta[name="session-id"]')?.content;
    if (!sessionId) return;
    
    let checkCount = 0;
    const maxChecks = 60; // Максимум 2 минуты
    
    function pollStatus() {
        if (checkCount >= maxChecks) {
            console.log('Max polling attempts reached');
            return;
        }
        
        fetch(`/status/${sessionId}`)
            .then(response => {
                if (!response.ok) {
                    throw new Error('Network response was not ok');
                }
                return response.json();
            })
            .then(data => {
                updateStatusDisplay(data);
                
                // Если обработка еще не завершена, продолжаем опрос
                if (data.status === 'processing' && checkCount < maxChecks) {
                    checkCount++;
                    setTimeout(pollStatus, 2000); // Опрос каждые 2 секунды
                }
            })
            .catch(error => {
                console.error('Error checking status:', error);
                
                // Повторяем попытку через 5 секунд при ошибке
                if (checkCount < maxChecks) {
                    checkCount++;
                    setTimeout(pollStatus, 5000);
                }
            });
    }
    
    pollStatus();
}

function updateStatusDisplay(data) {
    // Обновляем прогресс-бар
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');
    
    if (progressFill) {
        progressFill.style.width = `${data.progress || 0}%`;
    }
    
    if (progressText) {
        progressText.textContent = `${data.progress || 0}%`;
    }
    
    // Обновляем сообщение
    const statusMessage = document.getElementById('statusMessage');
    if (statusMessage) {
        statusMessage.textContent = data.message || 'Обработка...';
    }
    
    // Обновляем иконку статуса
    const statusIcon = document.querySelector('.status-header h2 i');
    if (statusIcon) {
        if (data.status === 'processing') {
            statusIcon.className = 'fas fa-spinner fa-spin';
            statusIcon.style.color = '';
        } else if (data.status === 'success') {
            statusIcon.className = 'fas fa-check-circle';
            statusIcon.style.color = '#28a745';
        } else if (data.status === 'error') {
            statusIcon.className = 'fas fa-exclamation-circle';
            statusIcon.style.color = '#dc3545';
        }
    }
    
    // Показываем результат если есть
    if (data.result) {
        const resultOutput = document.getElementById('resultOutput');
        const outputContent = document.getElementById('outputContent');
        
        if (resultOutput) resultOutput.style.display = 'block';
        if (outputContent) outputContent.textContent = data.result;
    }
    
    // Показываем кнопки действий если обработка завершена
    if (data.status === 'success' || data.status === 'error') {
        const resultActions = document.getElementById('resultActions');
        if (resultActions) resultActions.style.display = 'flex';
    }
}