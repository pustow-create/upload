// static/script.js
document.addEventListener('DOMContentLoaded', function() {
    console.log('VK Photo Uploader loaded');
    
    // Проверка поддержки Clipboard API
    if (navigator.clipboard) {
        console.log('Clipboard API доступен');
    }
    
    // Подсчет символов в textarea
    const textareas = document.querySelectorAll('textarea');
    textareas.forEach(textarea => {
        const counter = document.createElement('div');
        counter.className = 'char-counter';
        counter.style.fontSize = '0.8rem';
        counter.style.color = '#6c757d';
        counter.style.textAlign = 'right';
        counter.style.marginTop = '5px';
        
        textarea.parentNode.appendChild(counter);
        
        function updateCounter() {
            const length = textarea.value.length;
            counter.textContent = `${length} символов`;
            
            if (length > 10000) {
                counter.style.color = '#dc3545';
            } else if (length > 5000) {
                counter.style.color = '#ffc107';
            } else {
                counter.style.color = '#6c757d';
            }
        }
        
        textarea.addEventListener('input', updateCounter);
        updateCounter();
    });
    
    // Валидация формы
    const forms = document.querySelectorAll('form');
    forms.forEach(form => {
        form.addEventListener('submit', function(e) {
            const textareas = this.querySelectorAll('textarea[required]');
            let isValid = true;
            
            textareas.forEach(textarea => {
                if (!textarea.value.trim()) {
                    isValid = false;
                    textarea.style.borderColor = '#dc3545';
                    
                    if (!textarea.nextElementSibling?.classList.contains('error-message')) {
                        const error = document.createElement('div');
                        error.className = 'error-message';
                        error.style.color = '#dc3545';
                        error.style.fontSize = '0.9rem';
                        error.style.marginTop = '5px';
                        error.textContent = 'Это поле обязательно для заполнения';
                        textarea.parentNode.appendChild(error);
                    }
                } else {
                    textarea.style.borderColor = '#dee2e6';
                    const error = textarea.parentNode.querySelector('.error-message');
                    if (error) {
                        error.remove();
                    }
                }
            });
            
            if (!isValid) {
                e.preventDefault();
                
                // Прокрутка к первой ошибке
                const firstError = this.querySelector('.error-message');
                if (firstError) {
                    firstError.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            }
        });
    });
    
    // Анимация загрузки кнопок
    const buttons = document.querySelectorAll('.btn');
    buttons.forEach(button => {
        button.addEventListener('click', function(e) {
            if (this.type === 'submit' || this.href) {
                // Добавляем анимацию загрузки
                const originalHTML = this.innerHTML;
                this.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Обработка...';
                this.disabled = true;
                
                // Восстанавливаем через 5 секунд (на случай если что-то пошло не так)
                setTimeout(() => {
                    this.innerHTML = originalHTML;
                    this.disabled = false;
                }, 5000);
            }
        });
    });
    
    // Подсветка синтаксиса для примеров кода
    const codeBlocks = document.querySelectorAll('pre');
    codeBlocks.forEach(block => {
        // Добавляем кнопку копирования для блоков кода
        if (!block.parentNode.querySelector('.copy-code-btn')) {
            const copyBtn = document.createElement('button');
            copyBtn.className = 'btn btn-small copy-code-btn';
            copyBtn.innerHTML = '<i class="fas fa-copy"></i> Копировать';
            copyBtn.style.position = 'absolute';
            copyBtn.style.top = '10px';
            copyBtn.style.right = '10px';
            
            copyBtn.addEventListener('click', function() {
                const text = block.textContent;
                navigator.clipboard.writeText(text).then(() => {
                    const originalHTML = this.innerHTML;
                    this.innerHTML = '<i class="fas fa-check"></i> Скопировано!';
                    this.classList.add('btn-success');
                    
                    setTimeout(() => {
                        this.innerHTML = originalHTML;
                        this.classList.remove('btn-success');
                    }, 2000);
                });
            });
            
            block.style.position = 'relative';
            block.style.paddingTop = '40px';
            block.parentNode.style.position = 'relative';
            block.parentNode.appendChild(copyBtn);
        }
    });
    
    // Сохранение введенных данных в localStorage
    const saveToStorage = debounce(function() {
        const inputs = document.querySelectorAll('textarea, input[type="text"]');
        const data = {};
        
        inputs.forEach(input => {
            if (input.name) {
                data[input.name] = input.value;
            }
        });
        
        if (Object.keys(data).length > 0) {
            localStorage.setItem('vk_uploader_form_data', JSON.stringify(data));
        }
    }, 1000);
    
    // Восстановление данных из localStorage
    const savedData = localStorage.getItem('vk_uploader_form_data');
    if (savedData) {
        try {
            const data = JSON.parse(savedData);
            Object.keys(data).forEach(key => {
                const input = document.querySelector(`[name="${key}"]`);
                if (input && !input.value) {
                    input.value = data[key];
                    
                    // Запускаем событие input для обновления счетчиков
                    input.dispatchEvent(new Event('input'));
                }
            });
        } catch (e) {
            console.log('Не удалось восстановить данные:', e);
        }
    }
    
    // Слушаем изменения в полях ввода
    document.addEventListener('input', saveToStorage);
    
    // Очистка localStorage при успешной отправке формы
    forms.forEach(form => {
        form.addEventListener('submit', function() {
            localStorage.removeItem('vk_uploader_form_data');
        });
    });
});

// Функция debounce для оптимизации
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// Отображение текущего времени
function updateClock() {
    const clockElement = document.getElementById('current-time');
    if (clockElement) {
        const now = new Date();
        clockElement.textContent = now.toLocaleTimeString('ru-RU');
    }
}

// Обновляем время каждую секунду
setInterval(updateClock, 1000);
updateClock();
