#!/usr/bin/env python3
import os
import re
import json
import time
import glob
import signal
import threading
import urllib.request
import urllib.error
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# --- ГЛОБАЛЬНЫЕ НАСТРОЙКИ ---
SETTINGS_FILE = "proxy_settings.json"
LOG_FILE = "proxy.log"
CACHE_FILE = "catalog_cache.json"
ROUTERAI_BASE_URL = "https://routerai.ru/api/v1"

# Блокировка для потокобезопасной записи в лог
log_lock = threading.Lock()
# Блокировка для потокобезопасной записи кэша каталога
cache_lock = threading.Lock()

default_settings = {
    "api_key": "ВАШ_API_КЛЮЧ_ЗДЕСЬ",
    "port": 5000,
    "selected_model": None,
    "selected_provider": None,
    "frozen_prices": {},
    "is_blocked": False,
    "block_reason": "",
    "check_interval_sec": 600,
    "filter_context": "",
    "filter_model_id": "",
    "filter_input_price": "",
    "filter_output_price": "",
    "filter_cache_price": "",
    "logging_enabled": True,
    "auto_open_on_block": False,
    "opencode_config_path": "",
    "opencode_config_allow_replace": False
}


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(default_settings, f, indent=2, ensure_ascii=False)
        return default_settings
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_port():
    """Возвращает порт прокси из настроек (по умолчанию 5000)."""
    try:
        port = int(load_settings().get("port", 5000))
    except Exception:
        port = 5000
    return port if 1 <= port <= 65535 else 5000


def get_base_url():
    """Возвращает базовый URL панели управления, например http://127.0.0.1:5000."""
    return f"http://127.0.0.1:{get_port()}"


# --- ФОНОВЫЙ МОНИТОРИНГ ЦЕН ---
# Предыдущее состояние блокировки — чтобы логировать переход один раз
_monitor_prev_blocked = None


def price_monitor_loop():
    global _monitor_prev_blocked
    while True:
        try:
            settings = load_settings()
            model = settings.get("selected_model")
            provider = settings.get("selected_provider")
            frozen = settings.get("frozen_prices")

            # При активной блокировке проверку цен не выполняем вообще
            # (никаких обращений к RouterAI), пока блокировка не будет снята
            if settings.get("is_blocked"):
                if _monitor_prev_blocked is not True:
                    write_log("Монитор", "Проверка цен приостановлена: активна блокировка. "
                                         "Выберите провайдера, чтобы применить новые цены и возобновить.")
                _monitor_prev_blocked = True
            else:
                if _monitor_prev_blocked is True:
                    write_log("Монитор", "Проверка цен возобновлена.")
                _monitor_prev_blocked = False

                if model and provider and frozen:
                    url = f"{ROUTERAI_BASE_URL}/models/{model}/endpoints"
                    req = urllib.request.Request(url, headers={"User-Agent": "RouterAI-Proxy/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    endpoints = data.get("data", {}).get("endpoints", [])

                    current_ep = next((ep for ep in endpoints if ep.get("tag") == provider), None)

                    if current_ep:
                        prices = current_ep.get("pricing") or {}
                        curr_prompt = prices.get("prompt", 0)
                        curr_comp = prices.get("completion", 0)
                        curr_cache = prices.get("input_cache_read", 0)
                        curr_cache_write = prices.get("input_cache_write", 0)

                        # Проверяем основные цены
                        price_increased = (curr_prompt > frozen.get("prompt", 0) or
                                curr_comp > frozen.get("completion", 0) or
                                curr_cache > frozen.get("input_cache_read", 0) or
                                curr_cache_write > frozen.get("input_cache_write", 0))

                        # Проверяем дополнительные цены из variable_pricings
                        variable_pricings = current_ep.get("variable_pricings") or []
                        frozen_variable = frozen.get("variable_pricings", [])
                        for vp in variable_pricings:
                            frozen_vp = next((fvp for fvp in frozen_variable if fvp.get("threshold") == vp.get("threshold")), None)
                            if frozen_vp:
                                if (vp.get("prompt", 0) > frozen_vp.get("prompt", 0) or
                                    vp.get("completion", 0) > frozen_vp.get("completion", 0) or
                                    vp.get("input_cache_read", 0) > frozen_vp.get("input_cache_read", 0)):
                                    price_increased = True
                                    break

                        if price_increased:
                            # Формируем детали изменений
                            changes = []
                            if curr_prompt > frozen.get("prompt", 0):
                                changes.append(f"Вх: {frozen['prompt']*1e6:.2f}→{curr_prompt*1e6:.2f}₽")
                            if curr_comp > frozen.get("completion", 0):
                                changes.append(f"Вых: {frozen['completion']*1e6:.2f}→{curr_comp*1e6:.2f}₽")
                            if curr_cache > frozen.get("input_cache_read", 0):
                                changes.append(f"Кэш чтения: {frozen['input_cache_read']*1e6:.2f}→{curr_cache*1e6:.2f}₽")
                            if curr_cache_write > frozen.get("input_cache_write", 0):
                                changes.append(f"Кэш записи: {frozen.get('input_cache_write', 0)*1e6:.2f}→{curr_cache_write*1e6:.2f}₽")

                            # Проверяем дополнительные цены
                            for vp in variable_pricings:
                                frozen_vp = next((fvp for fvp in frozen_variable if fvp.get("threshold") == vp.get("threshold")), None)
                                if frozen_vp:
                                    if vp.get("prompt", 0) > frozen_vp.get("prompt", 0):
                                        changes.append(f"От {vp.get('threshold', 0)//1000}K Вх: {frozen_vp['prompt']*1e6:.2f}→{vp['prompt']*1e6:.2f}₽")
                                    if vp.get("completion", 0) > frozen_vp.get("completion", 0):
                                        changes.append(f"От {vp.get('threshold', 0)//1000}K Вых: {frozen_vp['completion']*1e6:.2f}→{vp['completion']*1e6:.2f}₽")
                                    if vp.get("input_cache_read", 0) > frozen_vp.get("input_cache_read", 0):
                                        changes.append(f"От {vp.get('threshold', 0)//1000}K Кэш: {frozen_vp['input_cache_read']*1e6:.2f}→{vp['input_cache_read']*1e6:.2f}₽")

                            settings["is_blocked"] = True
                            settings["block_reason"] = "ЦЕНА ВЫРОСЛА! " + "; ".join(changes)
                            settings["price_changes"] = changes
                            save_settings(settings)
                            write_log("Монитор", settings["block_reason"])
        except Exception as e:
            print(f"Ошибка мониторинга цен: {e}")

        try:
            settings = load_settings()
            time.sleep(settings.get("check_interval_sec", 600))
        except Exception as e:
            print(f"Ошибка в цикле мониторинга при ожидании: {e}")
            time.sleep(600)


monitor_thread = threading.Thread(target=price_monitor_loop, daemon=True)
monitor_thread.start()

# --- ВЕБ-ИНТЕРФЕЙС УПРАВЛЕНИЯ С ТОЧЕЧНЫМИ ФИЛЬТРАМИ ПРОВАЙДЕРОВ ---
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <title>RouterAI Точечный Подбор Провайдеров</title>
    <style>
        body { font-family: sans-serif; max-width: 1250px; margin: 30px auto; padding: 0 20px; background: #f4f6f9; color: #333; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
        .filter-panel { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; background: #e9ecef; padding: 20px; border-radius: 8px; margin-bottom: 25px; }
        .filter-group { display: flex; flex-direction: column; }
        .filter-group label { font-size: 13px; font-weight: bold; margin-bottom: 5px; color: #495057; }
        .filter-group input { padding: 8px; border: 1px solid #ced4da; border-radius: 4px; font-size: 14px; }
        .model-section { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); margin-bottom: 25px; border-left: 5px solid #007bff; }
        .btn { background: #007bff; color: white; padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; font-size: 13px;}
        .btn-success { background: #28a745; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }
        th, td { padding: 8px 12px; border-bottom: 1px solid #ddd; text-align: left; }
        th { background: #f8f9fa; color: #555; }
        .alert { padding: 15px; background: #f8d7da; color: #721c24; border-radius: 4px; border: 1px solid #f5c6cb; margin-bottom: 20px; }
        .model-title { margin: 0 0 10px 0; color: #111; font-size: 18px; }
        .model-id { color: #666; font-size: 13px; font-weight: normal; margin-left: 10px; }
        .toggle-hidden-btn { background: none; border: none; color: #007bff; cursor: pointer; font-size: 13px; padding: 0; margin-bottom: 10px; display: none; align-items: center; font-weight: bold; }
        .toggle-hidden-btn:hover { text-decoration: underline; }
        .toggle-hidden-btn .arrow { margin-right: 5px; transition: transform 0.2s; display: inline-block; }
        .toggle-hidden-btn.expanded .arrow { transform: rotate(90deg); }
        .filtered-out { color: cadetblue; }
        /* Единый стиль значений хоста и тега в блоке профиля */
        .profile-val { font-size: 16px; font-weight: bold; }
    </style>
    <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
</head>
<body>
    <h1>Параметрический подбор конкретных провайдеров RouterAI</h1>

    <div style="display: flex; gap: 20px; align-items: stretch; margin-bottom: 20px;">
        <div class="card" id="profile-info" style="flex: 1; margin-bottom: 0;">
            <h3>Зафиксированный рабочий профиль в OpenCode</h3>
            <!-- PROFILE_INFO -->
        </div>
        <div class="card" style="width: 28%; margin-bottom: 0;">
            <h3>Настройки</h3>
            <label style="font-weight: normal; cursor: pointer; display: block; margin-bottom: 10px;">
                <input type="checkbox" id="logging-toggle" <!-- LOGGING_CHECKED --> onchange="toggleLogging(this.checked)"> Включить запись в proxy.log
            </label>
            <label style="font-weight: normal; cursor: pointer; display: block;">
                <input type="checkbox" id="auto-open-toggle" <!-- AUTO_OPEN_CHECKED --> onchange="toggleAutoOpen(this.checked)"> Авто-открытие панели при блокировке
            </label>
            <hr style="border: none; border-top: 1px solid #ddd; margin: 12px 0;">
            <button class="btn" style="width: 100%;" onclick="exportOpencodeConfig(this)" title="Создать opencode.jsonc в папке скрипта для работы через прокси">Выгрузить конфиг OpenCode</button>
            <small id="export-result" style="display: block; margin-top: 8px; color: #666;"></small>
            <hr style="border: none; border-top: 1px solid #ddd; margin: 12px 0;">
            <label style="font-weight: normal; cursor: pointer; display: block; margin-bottom: 8px;">
                <input type="checkbox" id="opencode-replace-toggle" <!-- OPENCODE_REPLACE_CHECKED --> onchange="toggleOpencodeReplace(this.checked)"> Разрешить изменение существующего конфига
            </label>
            <label style="font-size: 13px; color: #495057; display: block; margin-bottom: 4px;">Путь к конфигу OpenCode</label>
            <input type="text" id="opencode-config-path" placeholder="~/.config/opencode/opencode.jsonc" value="<!-- OPENCODE_CONFIG_PATH -->" style="width: 100%; box-sizing: border-box; padding: 6px; border: 1px solid #ced4da; border-radius: 4px; font-size: 13px; margin-bottom: 8px;" disabled onchange="saveOpencodePath(this.value)">
            <button class="btn" id="opencode-update-btn" style="width: 100%;" onclick="updateOpencodeConfig(this)" title="Обновить порт и лимиты в существующем конфиге OpenCode" disabled>Обновить конфиг OpenCode</button>
            <small id="opencode-update-result" style="display: block; margin-top: 8px; color: #666;"></small>
        </div>
    </div>

    <!-- ПАНЕЛЬ ФИЛЬТРАЦИИ ДО КОНКРЕТНОГО ПРОВАЙДЕРА -->
    <div class="filter-panel">
        <div class="filter-group">
            <label>Фильтр по ID модели</label>
            <input type="text" id="filter-model-id" placeholder="например: gpt" value="<!-- FILTER_MODEL_ID -->" oninput="applyFilters()">
        </div>
        <div class="filter-group">
            <label>Минимум Контекста (тыс. ток.)</label>
            <input type="number" id="filter-context" placeholder="Например: 262" value="<!-- FILTER_CONTEXT -->" oninput="applyFilters()">
        </div>
        <div class="filter-group">
            <label>Макс. цена Входа (за 1М ток.)</label>
            <input type="number" id="filter-input-price" step="0.5" placeholder="Макс ₽ за вход" value="<!-- FILTER_INPUT_PRICE -->" oninput="applyFilters()">
        </div>
        <div class="filter-group">
            <label>Макс. цена Выхода (за 1М ток.)</label>
            <input type="number" id="filter-output-price" step="0.5" placeholder="Макс ₽ за выход" value="<!-- FILTER_OUTPUT_PRICE -->" oninput="applyFilters()">
        </div>
        <div class="filter-group">
            <label>Макс. цена Кэша (за 1М ток.)</label>
            <input type="number" id="filter-cache-price" step="0.5" placeholder="Макс ₽ за кэш" value="<!-- FILTER_CACHE_PRICE -->" oninput="applyFilters()">
        </div>
    </div>

    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
        <h2 style="margin: 0;">Список отфильтрованных провайдеров:</h2>
        <div style="display: flex; align-items: center; gap: 12px;">
            <small style="color: #666;">Данные моделей обновлены: <span id="cache-date"><!-- CACHE_DATE --></span></small>
            <button class="btn" onclick="loadCatalog()" title="Загрузить список моделей и провайдеров">Обновить модели</button>
        </div>
    </div>
    <div id="catalog-container">
        <p style="color: gray; font-style: italic; text-align: center; padding: 40px;">Список моделей не загружен. Нажмите «Обновить модели» для загрузки.</p>
        <!-- CATALOG_DATA -->
    </div>

    <script>
    function saveFilters() {
        const filters = {
            modelId: document.getElementById('filter-model-id').value,
            context: document.getElementById('filter-context').value,
            inputPrice: document.getElementById('filter-input-price').value,
            outputPrice: document.getElementById('filter-output-price').value,
            cachePrice: document.getElementById('filter-cache-price').value
        };
        localStorage.setItem('routerai-filters', JSON.stringify(filters));
        saveFiltersAsync(filters);
    }

    function toggleLogging(enabled) {
        const formData = new URLSearchParams();
        formData.append('enabled', enabled ? 'true' : 'false');
        fetch('/api/toggle_logging', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
            body: formData.toString()
        }).catch(() => {});
    }

    function toggleAutoOpen(enabled) {
        const formData = new URLSearchParams();
        formData.append('enabled', enabled ? 'true' : 'false');
        fetch('/api/toggle_auto_open', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
            body: formData.toString()
        }).catch(() => {});
    }

    function exportOpencodeConfig(btn) {
        const result = document.getElementById('export-result');
        result.style.color = '#666';
        result.textContent = 'Выгрузка...';
        fetch('/api/export_opencode_config', {method: 'POST', cache: 'no-store'})
            .then(r => r.json())
            .then(data => {
                if (data.status === 'ok') {
                    result.style.color = '#28a745';
                    result.textContent = 'Создан: ' + data.path;
                } else {
                    result.style.color = '#721c24';
                    result.textContent = 'Ошибка: ' + (data.error || 'неизвестно');
                }
            })
            .catch(() => {
                result.style.color = '#721c24';
                result.textContent = 'Ошибка запроса';
            });
    }

    function toggleOpencodeReplace(enabled) {
        // Поле пути и кнопка обновления активны только при включённом чекбоксе
        document.getElementById('opencode-config-path').disabled = !enabled;
        document.getElementById('opencode-update-btn').disabled = !enabled;

        const formData = new URLSearchParams();
        formData.append('enabled', enabled ? 'true' : 'false');
        fetch('/api/toggle_opencode_replace', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
            body: formData.toString()
        }).catch(() => {});
    }

    function saveOpencodePath(path) {
        const result = document.getElementById('opencode-update-result');
        result.style.color = '#666';
        result.textContent = 'Сохранение пути...';
        const formData = new URLSearchParams();
        formData.append('path', path || '');
        fetch('/api/save_opencode_path', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
            body: formData.toString()
        })
            .then(r => r.json())
            .then(data => {
                result.style.color = data.status === 'ok' ? '#28a745' : '#721c24';
                result.textContent = data.status === 'ok' ? 'Путь сохранён' : ('Ошибка: ' + (data.error || ''));
            })
            .catch(() => {
                result.style.color = '#721c24';
                result.textContent = 'Ошибка сохранения пути';
            });
    }

    function updateOpencodeConfig(btn) {
        const result = document.getElementById('opencode-update-result');
        result.style.color = '#666';
        result.textContent = 'Обновление...';
        fetch('/api/update_opencode_config', {method: 'POST', cache: 'no-store'})
            .then(r => r.json())
            .then(data => {
                if (data.status === 'ok') {
                    result.style.color = '#28a745';
                    result.textContent = data.message;
                } else {
                    result.style.color = '#721c24';
                    result.textContent = 'Ошибка: ' + (data.error || 'неизвестно');
                }
            })
            .catch(() => {
                result.style.color = '#721c24';
                result.textContent = 'Ошибка запроса';
            });
    }

    function loadFilters() {
        const saved = localStorage.getItem('routerai-filters');
        if (saved) {
            try {
                const filters = JSON.parse(saved);
                if (filters.modelId) document.getElementById('filter-model-id').value = filters.modelId;
                if (filters.context) document.getElementById('filter-context').value = filters.context;
                if (filters.inputPrice) document.getElementById('filter-input-price').value = filters.inputPrice;
                if (filters.outputPrice) document.getElementById('filter-output-price').value = filters.outputPrice;
                if (filters.cachePrice) document.getElementById('filter-cache-price').value = filters.cachePrice;
                return true;
            } catch (e) {
                return false;
            }
        }
        return false;
    }

    function saveFiltersAsync(filters) {
        const formData = new URLSearchParams();
        formData.append('filter_model_id', filters.modelId || '');
        formData.append('filter_context', filters.context || '');
        formData.append('filter_input_price', filters.inputPrice || '');
        formData.append('filter_output_price', filters.outputPrice || '');
        formData.append('filter_cache_price', filters.cachePrice || '');

        fetch('/api/save_filters', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: formData.toString()
        }).catch(() => {});
    }

    function applyFilters() {
        saveFilters();
        const modelIdFilter = (document.getElementById('filter-model-id').value || '').trim().toLowerCase();
        const reqContextK = parseFloat(document.getElementById('filter-context').value) || 0;
        const maxInput = parseFloat(document.getElementById('filter-input-price').value) || Infinity;
        const maxOutput = parseFloat(document.getElementById('filter-output-price').value) || Infinity;
        const maxCache = parseFloat(document.getElementById('filter-cache-price').value) || Infinity;

        const modelSections = document.querySelectorAll('.model-section');
        modelSections.forEach(section => {
            // Фильтр по ID модели — скрывает всю секцию при несовпадении
            const modelIdCell = section.querySelector('.model-id code');
            const modelId = modelIdCell ? modelIdCell.textContent.trim().toLowerCase() : '';
            if (modelIdFilter && !modelId.includes(modelIdFilter)) {
                section.style.display = 'none';
                return;
            }

            const rows = section.querySelectorAll('.provider-row');
            let visibleRowsInModel = 0;
            let hiddenRowsInModel = 0;

            const toggleBtn = section.querySelector('.toggle-hidden-btn');
            const isCurrentlyExpanded = toggleBtn ? toggleBtn.classList.contains('expanded') : false;

            rows.forEach(row => {
                const rowContextRaw = parseFloat(row.getAttribute('data-context-raw')) || 0;
                const rowContextK = rowContextRaw / 1000;
                const rowInput = parseFloat(row.getAttribute('data-input')) || 0;
                const rowOutput = parseFloat(row.getAttribute('data-output')) || 0;
                const rowCache = parseFloat(row.getAttribute('data-cache')) || 0;

                const matchesContext = (rowContextK >= reqContextK);
                const matchesPrices = (rowInput <= maxInput) && (rowOutput <= maxOutput) && (rowCache <= maxCache);
                const isMatched = matchesContext && matchesPrices;

                const tagCell = row.querySelector('td:nth-child(2) code');
                const providerTag = tagCell ? tagCell.textContent : '';
                const assocRows = section.querySelectorAll('.variable-pricing-row[data-provider-assoc="' + providerTag + '"]');

                if (isMatched) {
                    row.classList.remove('filtered-out');
                    row.style.display = '';
                    assocRows.forEach(r => { r.classList.remove('filtered-out'); r.style.display = ''; });
                    visibleRowsInModel++;
                } else {
                    row.classList.add('filtered-out');
                    assocRows.forEach(r => r.classList.add('filtered-out'));
                    if (isCurrentlyExpanded) {
                        row.style.display = '';
                        assocRows.forEach(r => r.style.display = '');
                    } else {
                        row.style.display = 'none';
                        assocRows.forEach(r => r.style.display = 'none');
                    }
                    hiddenRowsInModel++;
                }
            });

            // Управление кнопкой «Показать остальных»
            if (toggleBtn) {
                if (hiddenRowsInModel > 0 && visibleRowsInModel > 0) {
                    toggleBtn.style.display = 'flex';
                    toggleBtn.setAttribute('data-count', hiddenRowsInModel);
                    if (!isCurrentlyExpanded) {
                        toggleBtn.innerHTML = '<span class="arrow">▶</span> Показать остальных (' + hiddenRowsInModel + ')';
                    }
                } else {
                    toggleBtn.style.display = 'none';
                    toggleBtn.classList.remove('expanded');
                }
            }

            if (visibleRowsInModel > 0 || (hiddenRowsInModel > 0 && isCurrentlyExpanded)) {
                section.style.display = '';
            } else {
                section.style.display = 'none';
            }
        });
    }

    function toggleHiddenProviders(btn) {
        const section = btn.closest('.model-section');
        const isExpanded = btn.classList.contains('expanded');
        btn.classList.toggle('expanded');
        const count = btn.getAttribute('data-count') || '0';

        if (isExpanded) {
            btn.innerHTML = '<span class="arrow">▶</span> Показать остальных (' + count + ')';
            section.querySelectorAll('.filtered-out').forEach(item => item.style.display = 'none');
        } else {
            btn.innerHTML = '<span class="arrow">▼</span> Скрыть остальных';
            section.querySelectorAll('.filtered-out').forEach(item => item.style.display = '');
        }
    }

    document.addEventListener('DOMContentLoaded', function() {
        // Инициализируем кнопки-стрелки для каталога, отрендеренного из кэша
        document.querySelectorAll('.model-section').forEach(section => {
            if (!section.querySelector('.toggle-hidden-btn')) {
                const btn = document.createElement('button');
                btn.className = 'toggle-hidden-btn';
                btn.setAttribute('onclick', 'toggleHiddenProviders(this)');
                btn.innerHTML = '<span class="arrow">▶</span> Показать остальных';
                section.appendChild(btn);
            }
        });
        if (loadFilters()) {
            applyFilters();
        }
        checkPriceChanges();
        setInterval(checkPriceChanges, 30000);
    });

    function insertToggleButtons() {
        document.querySelectorAll('.model-section').forEach(section => {
            if (!section.querySelector('.toggle-hidden-btn')) {
                const btn = document.createElement('button');
                btn.className = 'toggle-hidden-btn';
                btn.setAttribute('onclick', 'toggleHiddenProviders(this)');
                btn.innerHTML = '<span class="arrow">▶</span> Показать остальных';
                section.appendChild(btn);
            }
        });
    }

    function updateCacheDate() {
        fetch('/api/cache_date', {cache: 'no-store'})
            .then(r => r.json())
            .then(data => {
                const el = document.getElementById('cache-date');
                if (el && data.cache_date) el.textContent = data.cache_date;
            })
            .catch(() => {});
    }

    function loadCatalog() {
        const container = document.getElementById('catalog-container');
        // Каталог из кэша уже отрендерен при открытии страницы —
        // кнопка выполняет полное обновление из API
        container.innerHTML = '<p style="color: gray; text-align: center; padding: 40px;">Загрузка моделей из RouterAI...</p>';
        fetch('/api/catalog?refresh=1', {cache: 'no-store'})
            .then(r => r.text())
            .then(html => {
                container.innerHTML = html;
                insertToggleButtons();
                applyFilters();
                updateCacheDate();
            })
            .catch(() => {
                container.innerHTML = '<p style="color: red; text-align: center; padding: 40px;">Ошибка загрузки каталога. Проверьте proxy.log</p>';
            });
    }

    function selectProvider(modelId, providerTag, prompt, completion, cache) {
        const formData = new URLSearchParams();
        formData.append('model_id', modelId);
        formData.append('provider_tag', providerTag);
        formData.append('prompt', prompt);
        formData.append('completion', completion);
        formData.append('cache', cache);

        fetch('/api/select', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
            body: formData.toString()
        })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'ok') {
                updateProfileBlock(data.profile);
                highlightSelectedRow(modelId, providerTag);
            }
        })
        .catch(() => {});
    }

    function updateProfileBlock(profile) {
        const block = document.getElementById('profile-info');
        if (!block || !profile) return;
        const name = (profile.model_name && profile.model_name !== profile.model)
            ? profile.model_name : profile.model;
        const ctx = (profile.context && profile.context !== '—')
            ? '<p><strong>Доступный Контекст:</strong> <strong>' + profile.context + '</strong></p>' : '';
        const valStyle = 'profile-val';
        const provName = profile.provider_name
            ? '<span class="' + valStyle + '">' + profile.provider_name + '</span>' +
              (profile.quantization ? ' <small style="color:gray;">' + profile.quantization + '</small>' : '')
            : '';
        const tagDisplay = '<span class="' + valStyle + '">' + profile.provider + '</span>';
        const extraHtml = (profile.extra_prices && profile.extra_prices.trim())
            ? '<p><strong>Дополнительно:</strong><br>' + profile.extra_prices.replace(/^<br>/, '') + '</p>' : '';
        block.innerHTML = '<h3>Зафиксированный рабочий профиль в OpenCode</h3>' +
            '<p style="margin-bottom: 10px;">' +
                '<span style="font-size: 16px;"><strong>' + name + '</strong></span>' +
                '<span style="color: #666; font-size: 13px; margin-left: 10px;">ID: <code>' + profile.model + '</code></span>' +
            '</p>' +
            '<p><strong>Провайдер (Хост):</strong> ' + provName + '</p>' +
            '<p><strong>Тег провайдера:</strong> ' + tagDisplay + '</p>' +
            ctx +
            '<p><strong>Цена фиксации (за 1М токенов):</strong> ' +
            'Вх: <span style="color:#28a745;font-weight:bold;">' + profile.prompt + ' ₽</span> | ' +
            'Вых: <span style="color:#28a745;font-weight:bold;">' + profile.completion + ' ₽</span> | ' +
            'Кэш чтения: <span style="color:#28a745;font-weight:bold;">' + profile.cache + ' ₽</span> | ' +
            'Кэш записи: <span style="color:#28a745;font-weight:bold;">' + (profile.cache_write || '0.00') + ' ₽</span></p>' +
            extraHtml +
            '<p style="margin-top: 8px;">' +
                '<a href="https://routerai.ru/models/' + profile.model + '" target="_blank" style="font-size: 13px; color: #007bff; text-decoration: none;">[Страница модели ↗]</a>' +
                '<a href="https://routerai.ru/api/v1/models/' + profile.model + '/endpoints" target="_blank" style="font-size: 13px; color: #28a745; text-decoration: none; margin-left: 10px;">[Endpoints ↗]</a>' +
            '</p>';
    }

    function highlightSelectedRow(modelId, providerTag) {
        const rows = document.querySelectorAll('.provider-row');
        rows.forEach(row => {
            row.style.background = '';
            // Подсвечиваем только строку с совпадением И модели, И провайдера
            const tagCell = row.querySelector('td:nth-child(2) code');
            const modelSection = row.closest('.model-section');
            const modelIdCell = modelSection ? modelSection.querySelector('.model-id code') : null;
            const rowModelId = modelIdCell ? modelIdCell.textContent.trim() : '';
            if (tagCell && tagCell.textContent === providerTag && rowModelId === modelId) {
                row.style.background = '#e2f0d9';
            }
        });
    }

    function checkPriceChanges() {
        fetch('/api/price_changes')
            .then(r => r.json())
            .then(data => {
                const profileBlock = document.getElementById('profile-info');
                const statusId = 'profile-block-status';

                // Удаляем старый статус из блока профиля
                const oldStatus = document.getElementById(statusId);
                if (oldStatus) oldStatus.remove();

                // На случай, если баннер остался от прошлой версии — убираем
                const oldBanner = document.getElementById('price-change-banner');
                if (oldBanner) oldBanner.remove();

                // Статус блокировки — только в блоке профиля
                if (data.is_blocked && data.block_reason && profileBlock) {
                    const el = document.createElement('p');
                    el.id = statusId;
                    el.style.cssText = 'padding:12px;background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;border-radius:4px;margin-top:10px;';
                    el.innerHTML = '<strong>[ЗАПРОСЫ ЗАБЛОКИРОВАНЫ]</strong><br>' + data.block_reason +
                        '<br><small>Выберите модель ниже, чтобы применить новые цены и снять блокировку.</small>';
                    profileBlock.appendChild(el);
                }
            })
            .catch(() => {});
    }
    </script>
</body>
</html>
"""


def format_variable_pricings(variable_pricings, provider_name="", provider_tag=""):
    """Форматирует дополнительные цены строго для конкретного провайдера."""
    if not variable_pricings:
        return ""
    
    rows = []
    for vp in variable_pricings:
        pricing_type = vp.get("type", "")
        prompt = vp.get("prompt", 0)
        completion = vp.get("completion", 0)
        cache_read = vp.get("input_cache_read", 0)
        cache_write = vp.get("input_cache_write", 0)
        
        # Определяем текст тарифа в зависимости от типа
        if pricing_type == "time-of-day":
            utc_start = vp.get("utc_start", 0)
            utc_end = vp.get("utc_end", 0)
            # Переводим UTC в московское время (UTC+3)
            msk_start = (utc_start + 3) % 24
            msk_end = (utc_end + 3) % 24
            start_str = f"{msk_start:02d}:00"
            end_str = f"{msk_end:02d}:00" if utc_end != 0 else "00:00"
            threshold_text = f"{start_str}–{end_str} МСК"
        elif pricing_type == "prompt-threshold":
            threshold = vp.get("threshold")
            threshold_text = f"От {threshold // 1000}K" if threshold else "—"
        else:
            threshold_text = pricing_type
        
        rows.append(f'''
                    <tr class="variable-pricing-row" style="background: #fff3cd;" data-provider-assoc="{provider_tag}">
                        <td colspan="3" style="font-size: 13px; color: #856404;">{provider_name}</td>
                        <td style="text-align: center; color: #666; font-size: 14px;">{threshold_text}</td>
                        <td style="text-align: right; font-size: 14px;">{prompt * 1000000:.2f}</td>
                        <td style="text-align: right; font-size: 14px;">{completion * 1000000:.2f}</td>
                        <td style="text-align: right; font-size: 14px;">{cache_read * 1000000:.2f}</td>
                        <td style="text-align: right; font-size: 14px;">{cache_write * 1000000:.2f}</td>
                        <td style="text-align: right; font-size: 14px;"></td>
                        <td style="font-size: 14px;"></td>
                    </tr>''')
    
    return "".join(rows)


def format_price_cell(ep, price_key):
    """Форматирует цену с учётом типа единицы (token, request и т.д.)."""
    pricing = ep.get("pricing") or {}
    pricing_units = ep.get("pricing_units") or {}
    value = pricing.get(price_key, 0)
    unit = pricing_units.get(price_key, "token")
    
    if unit == "request":
        return f"{value:.2f} ₽/вызов"
    else:
        return f"{value * 1000000:.2f}"


# Соответствие ключей pricing русским названиям, множителям и единицам
# (порядок — как в справочнике RouterAI)
# Примечание: prompt/completion/input_cache_read/input_cache_write выводятся
# отдельными колонками, поэтому здесь отсутствуют.
PRICE_META = {
    "request":            ("Запрос",                1.0, "1 запрос"),
    "image":              ("Входящие изображения",  1e3, "1К токенов"),
    "audio":              ("Входящие аудио",        1e6, "1М токенов"),
    "internal_reasoning": ("Размышления",           1e6, "1М токенов"),
    "web_search":         ("Веб-поиск",             1e3, "1К вызовов"),
    "input_audio_cache":  ("Кэш: аудио",            1e6, "1М токенов"),
}
# Ключи, которые выводятся отдельными колонками и не должны попадать в «Дополнительно»
BASE_PRICE_KEYS = {"prompt", "completion", "input_cache_read", "input_cache_write"}


def format_extra_prices(ep, inline=False):
    """Форматирует дополнительные цены с русскими названиями и корректными единицами.

    inline=False — подпись и значение на разных строках (для узкой колонки таблицы)
    inline=True  — подпись и значение в одной строке (для блока профиля)

    Ключи, не описанные в PRICE_META, не отбрасываются, а выводятся
    с техническим именем — чтобы новая цена у RouterAI не потерялась молча.
    """
    pricing = ep.get("pricing") or {}
    pricing_units = ep.get("pricing_units") or {}
    separator = " " if inline else "<br>"

    parts = []
    handled = set(PRICE_META) | BASE_PRICE_KEYS

    # Сначала известные ключи в порядке справочника
    for key, (label, mult, unit) in PRICE_META.items():
        if key not in pricing:
            continue
        value = pricing.get(key) or 0
        parts.append(f"{label}:{separator}{value * mult:.2f} ₽/{unit}")

    # Затем всё неопознанное — чтобы ничего не исчезало
    for key, value in pricing.items():
        if key in handled or value in (None, 0):
            continue
        unit = pricing_units.get(key, "token")
        # Мелкие значения не округляем в ноль
        shown = f"{value:.2f}" if abs(value) >= 0.01 else f"{value:.6f}".rstrip("0").rstrip(".")
        if unit == "request":
            parts.append(f"{key}:{separator}{shown} ₽/вызов")
        elif unit == "second":
            parts.append(f"{key}:{separator}{shown} ₽/сек")
        elif unit == "token":
            parts.append(f"{key}:{separator}{value * 1000000:.2f} ₽/1М токенов")
        else:
            # Неизвестная единица — показываем значение как есть, без догадок
            parts.append(f"{key}:{separator}{shown} ₽/{unit}")

    return "<br>".join(parts)


def get_cache_info():
    """Возвращает дату последнего обновления файла кэша моделей."""
    try:
        if os.path.exists(CACHE_FILE):
            mtime = os.path.getmtime(CACHE_FILE)
            from datetime import datetime
            return datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M:%S")
        return "кэш отсутствует"
    except Exception:
        return "недоступно"


def get_model_display_name(model_id, catalog_data):
    """Возвращает название модели по её ID из каталога. Если не найдено — сам ID."""
    if not model_id:
        return ""
    for item in catalog_data:
        if item.get("id") == model_id:
            return item.get("name", model_id)
    return model_id


def get_selected_endpoint(model_id, provider_tag, catalog_data):
    """Возвращает endpoint выбранного провайдера для модели из каталога."""
    if not model_id or not provider_tag:
        return {}
    for item in catalog_data:
        if item.get("id") == model_id:
            for ep in item.get("endpoints", []):
                if ep.get("tag") == provider_tag:
                    return ep
    return {}


def render_html(settings, catalog_data):
    html = HTML_TEMPLATE

    # Значения фильтров из настроек
    html = html.replace("<!-- FILTER_MODEL_ID -->", str(settings.get("filter_model_id", "")))
    html = html.replace("<!-- FILTER_CONTEXT -->", str(settings.get("filter_context", "")))
    html = html.replace("<!-- FILTER_INPUT_PRICE -->", str(settings.get("filter_input_price", "")))
    html = html.replace("<!-- FILTER_OUTPUT_PRICE -->", str(settings.get("filter_output_price", "")))
    html = html.replace("<!-- FILTER_CACHE_PRICE -->", str(settings.get("filter_cache_price", "")))

    # Состояние чекбоксов
    html = html.replace("<!-- LOGGING_CHECKED -->", "checked" if settings.get("logging_enabled", True) else "")
    html = html.replace("<!-- AUTO_OPEN_CHECKED -->", "checked" if settings.get("auto_open_on_block", False) else "")
    html = html.replace("<!-- OPENCODE_REPLACE_CHECKED -->", "checked" if settings.get("opencode_config_allow_replace", False) else "")
    html = html.replace("<!-- OPENCODE_CONFIG_PATH -->", str(settings.get("opencode_config_path", "")))

    # Дата обновления кэша моделей
    cache_date = get_cache_info()
    html = html.replace("<!-- CACHE_DATE -->", cache_date)

    # Профиль
    if settings.get("selected_model") and settings.get("selected_provider"):
        frozen = settings.get("frozen_prices", {})
        model_name = get_model_display_name(settings["selected_model"], catalog_data)
        selected_ep = get_selected_endpoint(settings["selected_model"], settings["selected_provider"], catalog_data)
        context_len = selected_ep.get("context_length") or 0
        provider_name = selected_ep.get("provider_name") or ""
        quantization = selected_ep.get("quantization") or ""
        prov_display = f'<span class="profile-val">{provider_name}</span>' if provider_name else ""
        if quantization:
            prov_display += f' <small style="color:gray;">{quantization}</small>'
        tag_display = f'<span class="profile-val">{settings["selected_provider"]}</span>'
        extra_prices = format_extra_prices(selected_ep, inline=True)
        extra_html = f'<p><strong>Дополнительно:</strong><br>{extra_prices}</p>' if extra_prices else ''
        context_html = f'<p><strong>Доступный Контекст:</strong> <strong>{context_len // 1000}K</strong> токенов</p>' if context_len else ''
        links_html = f'''
            <p style="margin-top: 8px;">
                <a href="https://routerai.ru/models/{settings["selected_model"]}" target="_blank" style="font-size: 13px; color: #007bff; text-decoration: none;">[Страница модели ↗]</a>
                <a href="https://routerai.ru/api/v1/models/{settings["selected_model"]}/endpoints" target="_blank" style="font-size: 13px; color: #28a745; text-decoration: none; margin-left: 10px;">[Endpoints ↗]</a>
            </p>'''
        profile_html = f'''
            <p style="margin-bottom: 10px;">
                <span style="font-size: 16px;"><strong>{model_name}</strong></span>
                <span style="color: #666; font-size: 13px; margin-left: 10px;">ID: <code>{settings["selected_model"]}</code></span>
            </p>
            <p><strong>Провайдер (Хост):</strong> {prov_display}</p>
            <p><strong>Тег провайдера:</strong> {tag_display}</p>
            {context_html}'''
        if frozen:
            profile_html += f'''
            <p><strong>Цена фиксации (за 1М токенов):</strong>
                Вх: <span style="color:#28a745; font-weight:bold;">{frozen.get("prompt", 0) * 1000000:.2f} ₽</span> |
                Вых: <span style="color:#28a745; font-weight:bold;">{frozen.get("completion", 0) * 1000000:.2f} ₽</span> |
                Кэш чтения: <span style="color:#28a745; font-weight:bold;">{frozen.get("input_cache_read", 0) * 1000000:.2f} ₽</span> |
                Кэш записи: <span style="color:#28a745; font-weight:bold;">{frozen.get("input_cache_write", 0) * 1000000:.2f} ₽</span>
            </p>'''
        profile_html += extra_html
        profile_html += links_html
        if settings.get("is_blocked"):
            profile_html += f'''
            <p id="profile-block-status" style="padding:12px;background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;border-radius:4px;margin-top:10px;">
                <strong>[ЗАПРОСЫ ЗАБЛОКИРОВАНЫ]</strong><br>{settings.get("block_reason", "")}<br>
                <small>Выберите модель ниже, чтобы применить новые цены и снять блокировку.</small>
            </p>'''
        html = html.replace("<!-- PROFILE_INFO -->", profile_html)
    else:
        html = html.replace("<!-- PROFILE_INFO -->", '''
            <p style="color: #856404; background: #fff3cd; border: 1px solid #ffeeba; border-radius: 4px; padding: 12px; margin: 0;">
                <strong>Модель не выбрана.</strong> Нажмите «Обновить модели», задайте фильтры и нажмите «Выбрать» напротив нужного провайдера.
            </p>''')

    # Каталог
    catalog_html = ""
    for item in catalog_data:
        created = item.get("created")
        if created:
            from datetime import datetime
            date_str = datetime.fromtimestamp(created).strftime("%d.%m.%Y")
        else:
            date_str = ""
        description = item.get("description", "")
        catalog_html += f'''
        <div class="model-section">
            <h3 class="model-title">{item["name"]} <span class="model-id">ID: <code>{item["id"]}</code></span>
            <span style="margin-left: 15px; font-size: 13px; color: #666;">{date_str}</span>
            <a href="https://routerai.ru/models/{item["id"]}" target="_blank" style="margin-left: 15px; font-size: 13px; color: #007bff; text-decoration: none;">[Страница модели ↗]</a>
            <a href="https://routerai.ru/api/v1/models/{item["id"]}/endpoints" target="_blank" style="margin-left: 10px; font-size: 13px; color: #28a745; text-decoration: none;">[Endpoints ↗]</a></h3>
            {f'<details style="margin-bottom: 10px;"><summary style="cursor: pointer; color: #666; font-size: 13px;">Описание</summary><p style="margin: 5px 0 0 0; font-size: 13px; color: #555;">{description}</p></details>' if description else ''}
            <table>
                <thead>
                    <tr>
                        <th>Провайдер (Хост)</th>
                        <th>Тег провайдера</th>
                        <th>Доступный Контекст</th>
                        <th>Тариф</th>
                        <th style="text-align: right;">Вход (₽/1М)</th>
                        <th style="text-align: right;">Выход (₽/1М)</th>
                        <th style="text-align: right;">Кэш: чтение (₽/1М)</th>
                        <th style="text-align: right;">Кэш: запись (₽/1М)</th>
                        <th style="text-align: right; min-width: 180px;">Дополнительно</th>
                        <th>Действие</th>
                    </tr>
                </thead>
                <tbody>'''
        for ep in item["endpoints"]:
            selected_style = ' style="background: #e2f0d9;"' if (
                settings.get("selected_model") == item["id"] and
                settings.get("selected_provider") == ep.get("tag")
            ) else ""
            vp_rows = format_variable_pricings(ep.get("variable_pricings"), ep.get("provider_name", ""), ep.get("tag", ""))
            extra_prices = format_extra_prices(ep)
            catalog_html += f'''
                    <tr class="provider-row"{selected_style}
                        data-context-raw="{ep.get("context_length") or 0}"
                        data-input="{(ep.get("pricing") or {}).get("prompt", 0) * 1000000}"
                        data-output="{(ep.get("pricing") or {}).get("completion", 0) * 1000000}"
                        data-cache="{(ep.get("pricing") or {}).get("input_cache_read", 0) * 1000000}">
                        <td><strong>{ep.get("provider_name", "")}</strong> <small style="color:gray;">{ep.get("quantization", "")}</small></td>
                        <td><code>{ep.get("tag", "")}</code></td>
                        <td><strong>{(ep.get("context_length") or 0) // 1000}K</strong> токенов</td>
                        <td style="text-align: center; font-size: 14px;">Базовый</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "prompt")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "completion")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "input_cache_read")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "input_cache_write")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_extra_prices(ep)}</td>
                        <td>
                            <button class="btn" onclick="selectProvider('{item["id"]}', '{ep.get("tag", "")}', {(ep.get("pricing") or {}).get("prompt", 0)}, {(ep.get("pricing") or {}).get("completion", 0)}, {(ep.get("pricing") or {}).get("input_cache_read", 0)})">Выбрать</button>
                        </td>
                    </tr>{vp_rows}'''
        catalog_html += '''
                </tbody>
            </table>
        </div>'''
    html = html.replace("<!-- CATALOG_DATA -->", catalog_html)
    # Если каталог отрендерен — убираем заглушку «не загружен»
    if catalog_html:
        html = html.replace(
            '<p style="color: gray; font-style: italic; text-align: center; padding: 40px;">Список моделей не загружен. Нажмите «Обновить модели» для загрузки.</p>',
            '')

    return html


def write_log(tag, msg):
    """Запись лога в файл с учётом настройки logging_enabled."""
    try:
        settings = load_settings()
        if not settings.get("logging_enabled", True):
            return
    except Exception:
        pass
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] [{tag}] {msg}\n")
    except Exception:
        pass


def load_catalog_cache():
    """Загружает каталог из файла кэша. Возвращает [] если кэша нет."""
    try:
        with cache_lock:
            if not os.path.exists(CACHE_FILE):
                return []
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        return data.get("catalog", [])
    except Exception as e:
        write_log("Каталог", f"Ошибка чтения кэша: {e}")
        return []


def save_catalog_cache(catalog_data):
    """Сохраняет каталог в файл кэша с отметкой времени."""
    try:
        with cache_lock:
            from datetime import datetime
            payload = {
                "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "models_count": len(catalog_data),
                "catalog": catalog_data
            }
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        write_log("Каталог", f"Кэш сохранён: {len(catalog_data)} моделей")
    except Exception as e:
        write_log("Каталог", f"Ошибка сохранения кэша: {e}")


def fetch_catalog():
    catalog_data = []
    try:
        write_log("Каталог", "Запрос /models...")
        url = f"{ROUTERAI_BASE_URL}/models"
        req = urllib.request.Request(url, headers={"User-Agent": "RouterAI-Proxy/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        all_models = data.get("data", [])
        write_log("Каталог", f"Получено моделей: {len(all_models)} (тело ответа опущено)")
        
        def fetch_endpoints(m):
            model_id = m["id"]
            try:
                url = f"{ROUTERAI_BASE_URL}/models/{model_id}/endpoints"
                req = urllib.request.Request(url, headers={"User-Agent": "RouterAI-Proxy/1.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                endpoints = data.get("data", {}).get("endpoints", [])
                if endpoints:
                    return {
                        "id": model_id,
                        "name": m.get("name", model_id),
                        "endpoints": endpoints,
                        "created": m.get("created"),
                        "description": m.get("description", "")
                    }
            except Exception as e:
                write_log("Каталог", f"Ошибка загрузки endpoints для {model_id}: {type(e).__name__}: {e}")
                return None
        
        # Параллельная загрузка endpoints с сохранением порядка
        import concurrent.futures
        results_map = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_model = {executor.submit(fetch_endpoints, m): m for m in all_models}
            for future in concurrent.futures.as_completed(future_to_model):
                result = future.result()
                if result:
                    results_map[result["id"]] = result
        
        # Восстанавливаем исходный порядок моделей
        for m in all_models:
            model_id = m["id"]
            if model_id in results_map:
                catalog_data.append(results_map[model_id])
        write_log("Каталог", f"Загружено моделей: {len(catalog_data)}")
        # Сохраняем в файл кэша — он перезаписывается при каждом обновлении
        if catalog_data:
            save_catalog_cache(catalog_data)
    except Exception as e:
        write_log("Каталог", f"Ошибка загрузки каталога: {e}")
    return catalog_data


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        # Очищаем путь от лишних слэшей на конце для стабильного роутинга
        clean_path = parsed.path.rstrip('/')
        write_log("Запрос", f"GET {self.path} от {self.client_address[0]}")

        if clean_path == "" or clean_path == "/":
            self.handle_index()
        elif clean_path == "/v1/models":
            self.handle_list_models()
        elif clean_path == "/api/price_changes":
            self.handle_price_changes()
        elif clean_path == "/api/catalog":
            self.handle_api_catalog()
        elif clean_path == "/api/cache_date":
            self.handle_api_cache_date()
        else:
            write_log("Запрос", f"GET {clean_path} -> 404 (неизвестный путь)")
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        # Очищаем путь от лишних слэшей на конце
        clean_path = parsed.path.rstrip('/')
        content_length = int(self.headers.get("Content-Length", 0))
        write_log("Запрос", f"POST {self.path} от {self.client_address[0]} (Content-Length: {content_length})")

        if clean_path == "/select":
            self.handle_api_select()
        elif clean_path == "/api/select":
            self.handle_api_select()
        elif clean_path == "/unblock":
            self.handle_unblock()
        elif clean_path == "/save_filters":
            self.handle_save_filters()
        elif clean_path == "/api/save_filters":
            self.handle_api_save_filters()
        elif clean_path == "/api/toggle_logging":
            self.handle_api_toggle_logging()
        elif clean_path == "/api/toggle_auto_open":
            self.handle_api_toggle_auto_open()
        elif clean_path == "/api/export_opencode_config":
            self.handle_api_export_opencode_config()
        elif clean_path == "/api/update_opencode_config":
            self.handle_api_update_opencode_config()
        elif clean_path == "/api/toggle_opencode_replace":
            self.handle_api_toggle_opencode_replace()
        elif clean_path == "/api/save_opencode_path":
            self.handle_api_save_opencode_path()
        elif clean_path == "/v1/chat/completions":  # Работает при любых хвостах пути
            self.handle_chat_completions()
        else:
            write_log("Запрос", f"POST {clean_path} -> 404 (неизвестный путь)")
            self.send_error(404)

    def handle_index(self):
        try:
            settings = load_settings()
            # Рендерим каталог из файла кэша — без запроса к API
            cached_catalog = load_catalog_cache()
            html = render_html(settings, cached_catalog)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print(f"Ошибка при отображении главной страницы: {e}")

    def handle_list_models(self):
        try:
            mock_models = {"data": [{"id": "routerai/proxy-model", "object": "model"}]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(mock_models).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_api_toggle_auto_open(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            params = parse_qs(body)
            enabled = params.get("enabled", ["false"])[0].lower() == "true"

            settings = load_settings()
            settings["auto_open_on_block"] = enabled
            save_settings(settings)
            write_log("Прокси", f"Авто-открытие при блокировке: {'включено' if enabled else 'выключено'}")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "auto_open_on_block": enabled}).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_api_toggle_logging(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            params = parse_qs(body)
            enabled = params.get("enabled", ["false"])[0].lower() == "true"

            settings = load_settings()
            settings["logging_enabled"] = enabled
            save_settings(settings)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "logging_enabled": enabled}).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_api_cache_date(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(json.dumps({"cache_date": get_cache_info()}, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass

    def _send_json(self, payload, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def handle_api_toggle_opencode_replace(self):
        """Вкл/выкл разрешение на изменение существующего конфига OpenCode."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            params = parse_qs(self.rfile.read(content_length).decode("utf-8"))
            enabled = params.get("enabled", ["false"])[0].lower() == "true"

            settings = load_settings()
            settings["opencode_config_allow_replace"] = enabled
            save_settings(settings)
            write_log("Прокси", f"Разрешение на правку конфига OpenCode: {'включено' if enabled else 'выключено'}")
            self._send_json({"status": "ok", "allow_replace": enabled})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._send_json({"status": "error", "error": str(e)}, 500)

    def handle_api_save_opencode_path(self):
        """Сохраняет путь к существующему конфигу OpenCode."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            params = parse_qs(self.rfile.read(content_length).decode("utf-8"))
            path = (params.get("path", [""])[0] or "").strip()

            settings = load_settings()
            settings["opencode_config_path"] = path
            save_settings(settings)
            write_log("Прокси", f"Путь к конфигу OpenCode: {path or '(очищен)'}")
            self._send_json({"status": "ok", "path": path})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._send_json({"status": "error", "error": str(e)}, 500)

    def handle_api_update_opencode_config(self):
        """Обновляет порт и лимиты в существующем opencode.jsonc (хирургическая правка)."""
        try:
            settings = load_settings()

            # 1. Явное разрешение от пользователя
            if not settings.get("opencode_config_allow_replace"):
                self._send_json({
                    "status": "error",
                    "error": "Замена не разрешена — включите чекбокс «Разрешить изменение существующего конфига»"
                }, 403)
                return

            # 2. Путь обязан быть указан
            raw_path = (settings.get("opencode_config_path") or "").strip()
            if not raw_path:
                self._send_json({"status": "error", "error": "Путь к конфигу OpenCode не указан"}, 400)
                return

            # 3. Файл должен существовать — мы правим, а не создаём
            cfg_path = os.path.abspath(os.path.expanduser(raw_path))
            if not os.path.isfile(cfg_path):
                self._send_json({"status": "error", "error": f"Файл не найден: {cfg_path}"}, 404)
                return

            # 4. Нужны выбранные модель и провайдер — от них зависят лимиты
            model_id = settings.get("selected_model")
            if not model_id:
                self._send_json({"status": "error", "error": "Модель не выбрана в веб-панели управления"}, 400)
                return

            ep = get_selected_endpoint(model_id, settings.get("selected_provider"), load_catalog_cache())
            new_context = ep.get("context_length") or 1048576
            new_output = ep.get("max_completion_tokens") or 131000
            port = get_port()

            with open(cfg_path, "r", encoding="utf-8") as f:
                text = f.read()
            original = text

            # --- Правка порта в baseURL (по нашей подписи 127.0.0.1:<port>/v1) ---
            port_changed = False
            old_port = None
            m = re.search(r'("baseURL"\s*:\s*"http://127\.0\.0\.1)(?::(\d+))?(/v1")', text)
            if m:
                old_port = int(m.group(2)) if m.group(2) else 80
                if old_port != port:
                    text = text[:m.start()] + m.group(1) + f":{port}" + m.group(3) + text[m.end():]
                    port_changed = True

            # --- Правка лимитов внутри блока нашей модели ---
            limits_changed = False
            old_ctx = old_out = None
            key_idx = text.find('"routerai/proxy-model"')
            if key_idx != -1:
                window_end = min(key_idx + 1500, len(text))
                window = text[key_idx:window_end]
                lm = re.search(r'"limit"\s*:\s*\{', window)
                if lm:
                    brace_start = key_idx + lm.end() - 1
                    brace_end = window.find("}", lm.end() - 1)
                    if brace_end != -1:
                        abs_end = key_idx + brace_end
                        block = text[brace_start:abs_end + 1]

                        new_block = block
                        cm = re.search(r'"context"\s*:\s*(\d+)', new_block)
                        if cm:
                            old_ctx = int(cm.group(1))
                            new_block = (new_block[:cm.start()] + f'"context": {new_context}'
                                         + new_block[cm.end():])
                        om = re.search(r'"output"\s*:\s*(\d+)', new_block)
                        if om:
                            old_out = int(om.group(1))
                            new_block = (new_block[:om.start()] + f'"output": {new_output}'
                                         + new_block[om.end():])

                        if new_block != block:
                            text = text[:brace_start] + new_block + text[abs_end + 1:]
                            limits_changed = True

            found_any = (m is not None) or (key_idx != -1)
            if not found_any:
                self._send_json({
                    "status": "ok",
                    "message": "Ключи не найдены — используйте «Выгрузить конфиг OpenCode»"
                })
                return

            if text == original:
                self._send_json({"status": "ok", "message": "Уже актуально, изменений не требуется"})
                return

            # --- Бэкап перед первой записью ---
            backup_path = cfg_path + ".bak"
            if not os.path.exists(backup_path):
                with open(backup_path, "w", encoding="utf-8") as bf:
                    bf.write(original)

            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(text)

            changes = []
            if port_changed:
                changes.append(f"порт {old_port} → {port}")
            if limits_changed:
                if old_ctx is not None and old_ctx != new_context:
                    changes.append(f"context {old_ctx} → {new_context}")
                if old_out is not None and old_out != new_output:
                    changes.append(f"output {old_out} → {new_output}")

            msg = "Обновлено: " + ", ".join(changes) if changes else "Обновлено"
            write_log("Прокси", f"Конфиг OpenCode обновлён ({cfg_path}): {msg}; бэкап: {backup_path}")
            self._send_json({
                "status": "ok",
                "message": msg,
                "backup": backup_path,
                "path": cfg_path
            })
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            write_log("Прокси", f"Ошибка обновления конфига OpenCode: {e}")
            self._send_json({"status": "error", "error": str(e)}, 500)

    def handle_api_export_opencode_config(self):
        """Создаёт opencode.jsonc в папке скрипта для работы OpenCode через прокси."""
        try:
            settings = load_settings()
            model_id = settings.get("selected_model")

            # Берём контекст выбранной модели, чтобы limit был осмысленным
            context_len = 1048576
            if model_id:
                ep = get_selected_endpoint(model_id, settings.get("selected_provider"), load_catalog_cache())
                context_len = ep.get("context_length") or 1048576

            config = {
                "$schema": "https://opencode.ai",
                "disabled_providers": [],
                "provider": {
                    "custom": {
                        "name": "RouterAI",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": f"{get_base_url()}/v1",
                            "apiKey": "local-auth-not-needed"
                        },
                        "models": {
                            "routerai/proxy-model": {
                                "name": "RouterAI Контролируемая Модель",
                                "limit": {"context": context_len, "output": 131000}
                            }
                        }
                    }
                },
                "compaction": {"auto": True, "reserved": 15000}
            }

            out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opencode.jsonc")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)

            write_log("Прокси", f"Выгружен конфиг OpenCode: {out_path}")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "path": out_path,
                "context": context_len
            }, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            write_log("Прокси", f"Ошибка выгрузки конфига OpenCode: {e}")
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": str(e)}).encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_api_catalog(self):
        try:
            settings = load_settings()
            parsed = urlparse(self.path)
            # ?refresh=1 — принудительная загрузка из API; иначе — из файла кэша
            refresh = parse_qs(parsed.query).get("refresh", ["0"])[0] == "1"

            if refresh:
                catalog_data = fetch_catalog()
                if not catalog_data:
                    catalog_data = load_catalog_cache()
            else:
                catalog_data = load_catalog_cache()
                if not catalog_data:
                    # Кэша нет — загружаем из API впервые
                    catalog_data = fetch_catalog()

            # Рендерим только каталог без полной страницы
            catalog_html = ""
            for item in catalog_data:
                created = item.get("created")
                if created:
                    from datetime import datetime
                    date_str = datetime.fromtimestamp(created).strftime("%d.%m.%Y")
                else:
                    date_str = ""
                description = item.get("description", "")
                catalog_html += f'''
        <div class="model-section">
            <h3 class="model-title">{item["name"]} <span class="model-id">ID: <code>{item["id"]}</code></span>
            <span style="margin-left: 15px; font-size: 13px; color: #666;">{date_str}</span>
            <a href="https://routerai.ru/models/{item["id"]}" target="_blank" style="margin-left: 15px; font-size: 13px; color: #007bff; text-decoration: none;">[Страница модели ↗]</a>
            <a href="https://routerai.ru/api/v1/models/{item["id"]}/endpoints" target="_blank" style="margin-left: 10px; font-size: 13px; color: #28a745; text-decoration: none;">[Endpoints ↗]</a></h3>
            {f'<details style="margin-bottom: 10px;"><summary style="cursor: pointer; color: #666; font-size: 13px;">Описание</summary><p style="margin: 5px 0 0 0; font-size: 13px; color: #555;">{description}</p></details>' if description else ''}
            <table>
                <thead>
                    <tr>
                        <th>Провайдер (Хост)</th>
                        <th>Тег провайдера</th>
                        <th>Доступный Контекст</th>
                        <th>Тариф</th>
                        <th style="text-align: right;">Вход (₽/1М)</th>
                        <th style="text-align: right;">Выход (₽/1М)</th>
                        <th style="text-align: right;">Кэш: чтение (₽/1М)</th>
                        <th style="text-align: right;">Кэш: запись (₽/1М)</th>
                        <th style="text-align: right; min-width: 180px;">Дополнительно</th>
                        <th>Действие</th>
                    </tr>
                </thead>
                <tbody>'''
                for ep in item["endpoints"]:
                    selected_style = ' style="background: #e2f0d9;"' if (
                        settings.get("selected_model") == item["id"] and
                        settings.get("selected_provider") == ep.get("tag")
                    ) else ""
                    vp_rows = format_variable_pricings(ep.get("variable_pricings"), ep.get("provider_name", ""), ep.get("tag", ""))
                    extra_prices = format_extra_prices(ep)
                    catalog_html += f'''
                    <tr class="provider-row"{selected_style}
                        data-context-raw="{ep.get("context_length") or 0}"
                        data-input="{(ep.get("pricing") or {}).get("prompt", 0) * 1000000}"
                        data-output="{(ep.get("pricing") or {}).get("completion", 0) * 1000000}"
                        data-cache="{(ep.get("pricing") or {}).get("input_cache_read", 0) * 1000000}">
                        <td><strong>{ep.get("provider_name", "")}</strong> <small style="color:gray;">{ep.get("quantization", "")}</small></td>
                        <td><code>{ep.get("tag", "")}</code></td>
                        <td><strong>{(ep.get("context_length") or 0) // 1000}K</strong> токенов</td>
                        <td style="text-align: center; font-size: 14px;">Базовый</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "prompt")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "completion")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "input_cache_read")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "input_cache_write")}</td>
                        <td style="text-align: right; font-size: 14px;">{extra_prices}</td>
                        <td>
                            <button class="btn" onclick="selectProvider('{item["id"]}', '{ep.get("tag", "")}', {(ep.get("pricing") or {}).get("prompt", 0)}, {(ep.get("pricing") or {}).get("completion", 0)}, {(ep.get("pricing") or {}).get("input_cache_read", 0)})">Выбрать</button>
                        </td>
                    </tr>{vp_rows}'''
                catalog_html += '''
                </tbody>
            </table>
        </div>'''
            
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(catalog_html.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_price_changes(self):
        try:
            settings = load_settings()
            changes = settings.get("price_changes", [])
            is_blocked = settings.get("is_blocked", False)
            block_reason = settings.get("block_reason", "")
            frozen_prices = settings.get("frozen_prices", {})
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(json.dumps({
                "is_blocked": is_blocked,
                "block_reason": block_reason,
                "price_changes": changes,
                "frozen_prices": frozen_prices
            }, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_api_select(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        params = parse_qs(body)

        settings = load_settings()
        try:
            model_id = params.get("model_id", [""])[0]
            provider_tag = params.get("provider_tag", [""])[0]
            prompt = float(params.get("prompt", ["0"])[0])
            completion = float(params.get("completion", ["0"])[0])
            cache = float(params.get("cache", ["0"])[0])

            # Сохраняем параметры фильтрации
            settings["filter_model_id"] = params.get("filter_model_id", [""])[0]
            settings["filter_context"] = params.get("filter_context", [""])[0]
            settings["filter_input_price"] = params.get("filter_input_price", [""])[0]
            settings["filter_output_price"] = params.get("filter_output_price", [""])[0]
            settings["filter_cache_price"] = params.get("filter_cache_price", [""])[0]

            # Валидация: проверяем, что провайдер существует в каталоге
            url = f"{ROUTERAI_BASE_URL}/models/{model_id}/endpoints"
            req = urllib.request.Request(url, headers={"User-Agent": "RouterAI-Proxy/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            endpoints = data.get("data", {}).get("endpoints", [])

            valid_provider = False
            for ep in endpoints:
                if ep.get("tag") == provider_tag:
                    valid_provider = True
                    break

            if not valid_provider:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Выбранный провайдер не найден в каталоге!"}, ensure_ascii=False).encode("utf-8"))
                return

            # Находим выбранный endpoint для сохранения variable_pricings
            selected_ep = next((ep for ep in endpoints if ep.get("tag") == provider_tag), None)
            variable_pricings = (selected_ep or {}).get("variable_pricings") or []

            settings["selected_model"] = model_id
            settings["selected_provider"] = provider_tag
            settings["frozen_prices"] = {
                "prompt": prompt,
                "completion": completion,
                "input_cache_read": cache,
                "input_cache_write": float((selected_ep or {}).get("pricing", {}).get("input_cache_write", 0)),
                "variable_pricings": variable_pricings
            }
            settings["is_blocked"] = False
            settings["block_reason"] = ""
            save_settings(settings)

            # Возвращаем обновленные данные профиля
            cached = load_catalog_cache()
            model_name = get_model_display_name(model_id, cached)
            selected_ep = get_selected_endpoint(model_id, provider_tag, cached)
            context_len = selected_ep.get("context_length") or 0
            provider_name = selected_ep.get("provider_name") or ""
            quantization = selected_ep.get("quantization") or ""
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "profile": {
                    "model": model_id,
                    "model_name": model_name,
                    "provider": provider_tag,
                    "provider_name": provider_name,
                    "quantization": quantization,
                    "context": f"{context_len // 1000}K токенов" if context_len else "—",
                    "extra_prices": format_extra_prices(selected_ep, inline=True),
                    "prompt": f"{prompt * 1000000:.2f}",
                    "completion": f"{completion * 1000000:.2f}",
                    "cache": f"{cache * 1000000:.2f}",
                    "cache_write": f"{float((selected_ep or {}).get('pricing', {}).get('input_cache_write', 0)) * 1000000:.2f}"
                }
            }, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"Ошибка при выборе провайдера: {e}"}, ensure_ascii=False).encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_select(self):
        # Для совместимости со старыми формами — перенаправляем на /api/select
        self.handle_api_select()

    def handle_unblock(self):
        try:
            settings = load_settings()
            if settings["selected_model"] and settings["selected_provider"]:
                try:
                    url = f"{ROUTERAI_BASE_URL}/models/{settings['selected_model']}/endpoints"
                    req = urllib.request.Request(url, headers={"User-Agent": "RouterAI-Proxy/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    endpoints = data.get("data", {}).get("endpoints", [])
                    current_ep = next((ep for ep in endpoints if ep.get("tag") == settings["selected_provider"]), None)
                    if current_ep:
                        prices = current_ep.get("pricing") or {}
                        settings["frozen_prices"] = {
                            "prompt": prices.get("prompt", 0),
                            "completion": prices.get("completion", 0),
                            "input_cache_read": prices.get("input_cache_read", 0),
                            "input_cache_write": prices.get("input_cache_write", 0),
                            "variable_pricings": current_ep.get("variable_pricings") or []
                        }
                except Exception as e:
                    print(e)
            settings["is_blocked"] = False
            settings["block_reason"] = ""
            save_settings(settings)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<script>window.location.href='/';</script>")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_save_filters(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            params = parse_qs(body)

            settings = load_settings()
            settings["filter_model_id"] = params.get("filter_model_id", [""])[0]
            settings["filter_context"] = params.get("filter_context", [""])[0]
            settings["filter_input_price"] = params.get("filter_input_price", [""])[0]
            settings["filter_output_price"] = params.get("filter_output_price", [""])[0]
            settings["filter_cache_price"] = params.get("filter_cache_price", [""])[0]
            save_settings(settings)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<script>window.location.href='/';</script>")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_api_save_filters(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            params = parse_qs(body)

            settings = load_settings()
            settings["filter_model_id"] = params.get("filter_model_id", [""])[0]
            settings["filter_context"] = params.get("filter_context", [""])[0]
            settings["filter_input_price"] = params.get("filter_input_price", [""])[0]
            settings["filter_output_price"] = params.get("filter_output_price", [""])[0]
            settings["filter_cache_price"] = params.get("filter_cache_price", [""])[0]
            save_settings(settings)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_openai_error(self, status, message, err_type="invalid_request_error", code=None, add_hint=True):
        """Отправляет ошибку в стандартном формате OpenAI, который понимает @ai-sdk/openai-compatible.
        Всегда добавляет ссылку на главную страницу для выбора другого провайдера."""
        try:
            if add_hint:
                message = (f"{message} ... → выберите другого провайдера "
                           f"[Открыть панель выбора]({get_base_url()}/)")
            payload = {
                "error": {
                    "message": message,
                    "type": err_type,
                    "param": None,
                    "code": code
                }
            }
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_chat_completions(self):
        settings = load_settings()
        if settings.get("is_blocked"):
            write_log("Прокси", f"Запрос заблокирован: {settings['block_reason']}")
            # Авто-открытие панели управления в браузере (если включено)
            if settings.get("auto_open_on_block"):
                def _open_panel():
                    try:
                        webbrowser.open(get_base_url() + "/")
                        write_log("Прокси", "Авто-открытие: панель управления открыта в браузере")
                    except Exception as e:
                        write_log("Прокси", f"Авто-открытие не удалось: {e}")
                threading.Thread(target=_open_panel, daemon=True).start()
            self.send_openai_error(
                403,
                f"Запросы заблокированы прокси! Причина: {settings['block_reason']}",
                err_type="rate_limit_error",
                code="price_changed"
            )
            return
        if not settings.get("selected_model"):
            write_log("Прокси", "Отклонено: модель не выбрана")
            self.send_openai_error(400, "Модель не выбрана в веб-панели управления!")
            return
        if not settings.get("selected_provider"):
            write_log("Прокси", "Отклонено: провайдер не выбран")
            self.send_openai_error(400, "Провайдер не выбран в веб-панели управления!")
            return

        api_key = settings.get("api_key", "")
        if not api_key or api_key == "ВАШ_API_КЛЮЧ_ЗДЕСЬ":
            write_log("Прокси", "Отклонено: API ключ не настроен")
            self.send_openai_error(401, "API ключ не настроен! Укажите его в настройках.", err_type="authentication_error", code="invalid_api_key")
            return

        final_model = f"{settings['selected_model']}@provider={settings['selected_provider']}&allow_fallbacks=false"

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            incoming_data = json.loads(body.decode("utf-8"))
        except Exception:
            write_log("Прокси", "Отклонено: некорректный JSON в теле запроса")
            self.send_openai_error(400, "Некорректный JSON в теле запроса")
            return

        incoming_data["model"] = final_model
        original_model = body.decode("utf-8", errors="replace")
        try:
            req_model = json.loads(original_model).get("model", "?")
        except Exception:
            req_model = "?"
        msg_count = len(incoming_data.get("messages", []))
        write_log("Прокси", f"Запрос от клиента: model='{req_model}', сообщений={msg_count} -> подмена на '{final_model}', stream={incoming_data.get('stream', False)}")

        try:
            req = urllib.request.Request(
                f"{ROUTERAI_BASE_URL}/chat/completions",
                data=json.dumps(incoming_data).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "RouterAI-Proxy/1.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                ctype = resp.headers.get("Content-Type", "application/json")
                write_log("Прокси", f"Ответ RouterAI: статус {resp.status}, content-type={ctype}")
                self.send_response(resp.status)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                total_bytes = 0
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    self.wfile.write(chunk)
                write_log("Прокси", f"Стриминг завершён: отправлено {total_bytes} байт клиенту")
        except urllib.error.HTTPError as e:
            # Читаем и логируем начало тела ошибки, чтобы видеть реальную причину от RouterAI
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                err_body = "нет тела"
            write_log("Прокси", f"HTTP ошибка RouterAI: {e.code} {e.reason} | тело: {err_body}")
            try:
                # Пробуем извлечь сообщение из ответа RouterAI, иначе покажем статус
                try:
                    err_json = json.loads(err_body)
                    msg = err_json.get("error", {}).get("message") if isinstance(err_json.get("error"), dict) else err_json.get("error")
                    if not msg:
                        msg = f"RouterAI вернул {e.code}: {e.reason}"
                except Exception:
                    msg = f"RouterAI вернул {e.code}: {e.reason}"
                self.send_openai_error(e.code, msg, err_type="api_error", code=str(e.code))
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            write_log("Прокси", f"Ошибка проксирования: {e}")
            self.send_openai_error(500, f"Ошибка при проксировании запроса: {e}", err_type="api_error", code="proxy_error")


def prompt_open_browser():
    """Запрашивает открытие страницы в браузере в течение 5 секунд."""
    print("Открыть страницу управления в браузере? (Y/n): ", end="", flush=True)

    result = {"answer": None}

    def read_input():
        try:
            result["answer"] = input().strip().lower()
        except EOFError:
            result["answer"] = ""

    input_thread = threading.Thread(target=read_input, daemon=True)
    input_thread.start()
    input_thread.join(timeout=5)

    if input_thread.is_alive():
        print("\nВремя вышло. Страница не открыта.")
        return

    if result["answer"] in ("y", "yes", "д", "да", ""):
        print("Открываю страницу в браузере...")
        webbrowser.open(get_base_url())
    else:
        print("Страница не открыта.")


def _find_proxy_pids():
    """Возвращает список PID других запущенных экземпляров proxy.py.
    Точно определяет процессы: argv[0] — python-интерпретатор, proxy.py — в аргументах."""
    my_pid = os.getpid()
    found = []
    for pid_dir in glob.glob("/proc/[0-9]*"):
        pid = pid_dir.split("/")[-1]
        if not pid.isdigit() or int(pid) == my_pid:
            continue
        try:
            with open(f"{pid_dir}/cmdline", "rb") as f:
                raw = f.read().decode("utf-8", errors="replace")
            # В /proc/PID/cmdline аргументы разделены нулевыми байтами
            args = [a for a in raw.split("\x00") if a]
            if not args:
                continue
            # argv[0] должен быть python-интерпретатором (а не shell с упоминанием proxy.py)
            exe = os.path.basename(args[0])
            if "python" not in exe:
                continue
            # proxy.py должен быть среди аргументов
            if not any(os.path.basename(a) == "proxy.py" for a in args[1:]):
                continue
            found.append((int(pid), " ".join(args)))
        except Exception:
            continue
    return found


def _kill_windows_port_owner(port):
    """Windows: освобождает указанный порт, находя занявший его процесс.
    На Windows нет /proc, поэтому ищем через netstat и снимаем через taskkill."""
    import subprocess
    my_pid = str(os.getpid())
    killed = []
    try:
        out = subprocess.check_output(
            f"netstat -ano | findstr :{port}",
            shell=True, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="replace")

        # Последний столбец каждой строки — PID
        pids = set(re.findall(r"(\d+)\s*$", out, re.MULTILINE))
        for pid in pids:
            if not pid or pid in ("0", my_pid):
                continue
            rc = subprocess.call(
                f"taskkill /F /PID {pid}",
                shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL
            )
            if rc == 0:
                killed.append(pid)
    except Exception:
        pass
    return killed


def kill_existing_instances():
    """Ищет и снимает ранее запущенные экземпляры proxy.py перед стартом нового."""
    port = get_port()

    # На Windows /proc отсутствует — освобождаем порт через netstat + taskkill
    if os.name == "nt":
        killed = _kill_windows_port_owner(port)
        if killed:
            print(f"[Старт] Освобождён порт {port}, снято процессов: {len(killed)} (PID: {', '.join(killed)})")
            time.sleep(1)
        return []

    killed = []

    # Сначала мягко (SIGTERM)
    for pid, cmd in _find_proxy_pids():
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append((pid, cmd))
        except Exception:
            continue

    if killed:
        print(f"[Старт] Снято старых экземпляров proxy.py (SIGTERM): {len(killed)}")
        for pid, cmd in killed:
            print(f"        PID {pid}: {cmd[:80]}")
        time.sleep(1.5)  # даём время освободить порт

    # Если кто-то уцелел — жёстко (SIGKILL)
    survivors = _find_proxy_pids()
    if survivors:
        for pid, cmd in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"[Старт] Принудительно снят PID {pid} (SIGKILL)")
            except Exception:
                continue
        time.sleep(0.5)

    return killed


if __name__ == "__main__":
    PORT = get_port()

    # Снимаем ранее запущенные экземпляры, чтобы освободить порт
    kill_existing_instances()

    # Запускаем сервер в отдельном потоке
    try:
        server = HTTPServer(("127.0.0.1", PORT), ProxyHandler)
    except OSError as e:
        print(f"[Ошибка] Не удалось занять порт {PORT}: {e}")
        print(f"        Укажите другой порт в proxy_settings.json (поле \"port\") "
              f"или освободите порт: netstat -ano | findstr :{PORT}")
        raise SystemExit(1)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"Прокси-сервер запущен на {get_base_url()}")

    # Запрашиваем открытие страницы
    prompt_open_browser()

    # Основной поток ждёт прерывания
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nОстановка сервера...")
        server.shutdown()
