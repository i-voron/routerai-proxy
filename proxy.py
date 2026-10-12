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
import html as html_lib
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
# Блокировка для чтения-записи настроек: монитор цен и HTTP-обработчики
# работают в разных потоках и оба трогают proxy_settings.json
settings_lock = threading.RLock()

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


def esc(value) -> str:
    """Экранирует строку из API RouterAI перед вставкой в HTML.

    Названия моделей и провайдеров приходят извне и могут содержать разметку.
    Без экранирования она выполнилась бы в панели, где открыт конфиг с ключом.
    """
    return html_lib.escape(str(value), quote=True)


def atomic_write(path: str, data: str) -> None:
    """Пишет файл так, чтобы он не мог остаться обрезанным.

    Данные уходят во временный файл, сбрасываются на диск и только потом
    подменяют целевой. os.replace на одной файловой системе атомарен, поэтому
    читатель видит либо старую версию, либо новую — но не половину записи.
    """
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def update_settings(mutator):
    """Читает настройки, применяет изменение и сохраняет под одной блокировкой.

    Отдельные load + save терпят гонку: два потока прочитают одно состояние,
    каждый поменяет своё поле, и последний save перезапишет чужую правку.
    """
    with settings_lock:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)
        else:
            settings = dict(default_settings)
        mutator(settings)
        atomic_write(SETTINGS_FILE, json.dumps(settings, indent=2, ensure_ascii=False))
        return settings


def load_settings():
    with settings_lock:
        if not os.path.exists(SETTINGS_FILE):
            atomic_write(SETTINGS_FILE, json.dumps(default_settings, indent=2, ensure_ascii=False))
            return default_settings
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


# Ссылка на переменную окружения в настройках: {env:ИМЯ_ПЕРЕМЕННОЙ}
ENV_REF_RE = re.compile(r"\s*\{env:\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\s*")


def resolve_env_refs(value):
    """Разворачивает в настройках ссылки вида {env:ИМЯ_ПЕРЕМЕННОЙ}.

    Позволяет хранить секреты (например api_key) в переменных окружения,
    а не в файле настроек. Обычные значения возвращаются без изменений.
    Если переменная не найдена — возвращается пустая строка.
    """
    if not isinstance(value, str):
        return value
    m = ENV_REF_RE.fullmatch(value)
    if m:
        return os.environ.get(m.group(1), "")
    return value


def resolve_api_key(settings):
    """Возвращает кортеж (ключ, сообщение_об_ошибке).

    Ключ разворачивается из {env:...} при необходимости. Сообщение об ошибке
    заполнено только когда ключ использовать нельзя — и если причина в
    ненайденной переменной окружения, в тексте будет её имя.
    """
    raw = settings.get("api_key", "") or ""
    key = resolve_env_refs(raw)
    if key and key != "ВАШ_API_КЛЮЧ_ЗДЕСЬ":
        # Похоже на ссылку, но не разобралось — иначе уйдёт в RouterAI буквально
        if key.strip().startswith("{env:"):
            return key, (f"Некорректная ссылка на переменную окружения: {key.strip()} "
                         f"Ожидается формат {{env:ИМЯ_ПЕРЕМЕННОЙ}} — латиницей, "
                         f"цифрами и знаком подчёркивания.")
        return key, None
    m = ENV_REF_RE.fullmatch(raw)
    if m:
        return key, (f"Переменная окружения {m.group(1)} не найдена. "
                     f"Задайте её в окружении, из которого запущен прокси.")
    return key, "API ключ не настроен! Укажите его в настройках."


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

                            reason = "ЦЕНА ВЫРОСЛА! " + "; ".join(changes)
                            update_settings(lambda s: (
                                s.__setitem__("is_blocked", True),
                                s.__setitem__("block_reason", reason),
                                s.__setitem__("price_changes", changes),
                            ))
                            write_log("Монитор", reason)
        except Exception as e:
            write_log("Монитор", f"Ошибка мониторинга цен: {e}")

        try:
            settings = load_settings()
            time.sleep(settings.get("check_interval_sec", 600))
        except Exception as e:
            write_log("Монитор", f"Ошибка в цикле мониторинга при ожидании: {e}")
            time.sleep(600)


# --- ВЕБ-ИНТЕРФЕЙС УПРАВЛЕНИЯ С ТОЧЕЧНЫМИ ФИЛЬТРАМИ ПРОВАЙДЕРОВ ---
# Шаблон панели вынесен в отдельный файл, чтобы логика и представление
# не мешались друг другу. Плейсхолдеры <!-- XXX --> подставляются в render_html.
TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "panel.html")


def load_html_template() -> str:
    """Читает шаблон панели. Ошибку не глотаем — без шаблона панель не работает."""
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        return f.read()


HTML_TEMPLATE = load_html_template()


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
                    <tr class="variable-pricing-row" style="background: #fff3cd;" data-provider-assoc="{esc(provider_tag)}">
                        <td colspan="3" style="font-size: 13px; color: #856404;">{esc(provider_name)}</td>
                        <td style="text-align: center; color: #666; font-size: 14px;">{threshold_text}</td>
                        <td style="text-align: right; font-size: 14px;">{prompt * 1000000:.2f}</td>
                        <td style="text-align: right; font-size: 14px;">{completion * 1000000:.2f}</td>
                        <td style="text-align: right; font-size: 14px;">{cache_read * 1000000:.2f}</td>
                        <td style="text-align: right; font-size: 14px;">{cache_write * 1000000:.2f}</td>
                        <td style="text-align: right; font-size: 14px;"></td>
                        <td style="font-size: 14px;"></td>
                    </tr>''')
    
    return "".join(rows)


def format_tokens(value):
    """Форматирует количество токенов компактно: 1048576 → «1048K», 8192 → «8K».

    Значения меньше 1000 показываются как есть — иначе при делении на 1000
    они превращались бы в «0K». Пусто/ноль → «—».
    """
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    return f"{v // 1000}K" if v >= 1000 else str(v)


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


def render_catalog_sections(catalog_data, settings) -> str:
    """Отрисовывает все модели каталога: заголовок, описание и таблица провайдеров.

    Раньше эта разметка была продублирована в render_html и handle_api_catalog,
    и любую правку таблицы приходилось вносить в два места.
    """
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
            <h3 class="model-title">{esc(item["name"])} <span class="model-id">ID: <code>{esc(item["id"])}</code></span>
            <span style="margin-left: 15px; font-size: 13px; color: #666;">{date_str}</span>
            <a href="https://routerai.ru/models/{esc(item["id"])}" target="_blank" style="margin-left: 15px; font-size: 13px; color: #007bff; text-decoration: none;">[Страница модели ↗]</a>
            <a href="https://routerai.ru/api/v1/models/{esc(item["id"])}/endpoints" target="_blank" style="margin-left: 10px; font-size: 13px; color: #28a745; text-decoration: none;">[Endpoints ↗]</a></h3>
            {f'<details style="margin-bottom: 10px;"><summary style="cursor: pointer; color: #666; font-size: 13px;">Описание</summary><p style="margin: 5px 0 0 0; font-size: 13px; color: #555;">{esc(description)}</p></details>' if description else ''}
            <table>
                <thead>
                    <tr>
                        <th>Провайдер (Хост)</th>
                        <th>Тег провайдера</th>
                        <th>Контекст / Выход</th>
                        <th>Тариф</th>
                        <th style="text-align: right;">Вход (₽/1М)</th>
                        <th style="text-align: right;">Выход (₽/1М)</th>
                        <th style="text-align: right;">Кэш: чтение (₽/1М)</th>
                        <th style="text-align: right;">Кэш: запись (₽/1М)</th>
                        <th style="text-align: right; min-width: 180px;">Дополнительно</th>
                        <th>Действие<br>
                            <label style="font-size: 11px; font-weight: normal; cursor: pointer;" title="Выбрать к сравнению всех провайдеров этой модели">
                                <input type="checkbox" class="cmp-check-model" data-model="{esc(item["id"])}" onchange="toggleCompareModel(this, '{esc(item["id"])}')"> все
                            </label>
                        </th>
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
            tokens_display = " / ".join(
                t for t in (format_tokens(ep.get("context_length")),
                            format_tokens(ep.get("max_completion_tokens"))) if t != "—"
            ) or "—"
            catalog_html += f'''
                    <tr class="provider-row"{selected_style}
                        data-context-raw="{ep.get("context_length") or 0}"
                        data-input="{(ep.get("pricing") or {}).get("prompt", 0) * 1000000}"
                        data-output="{(ep.get("pricing") or {}).get("completion", 0) * 1000000}"
                        data-cache="{(ep.get("pricing") or {}).get("input_cache_read", 0) * 1000000}"
                        data-reasoning="{(ep.get("pricing") or {}).get("internal_reasoning", 0) * 1000000}"
                        data-request="{(ep.get("pricing") or {}).get("request", 0)}"
                        data-max-tokens="{ep.get("max_completion_tokens") or 0}"
                        data-model="{esc(item["id"])}"
                        data-model-name="{esc(get_model_display_name(item["id"], catalog_data))}"
                        data-tag="{esc(ep.get("tag", ""))}"
                        data-provider="{esc(ep.get("provider_name", ""))}">
                        <td><strong>{esc(ep.get("provider_name", ""))}</strong> <small style="color:gray;">{esc(ep.get("quantization", ""))}</small></td>
                        <td><code>{esc(ep.get("tag", ""))}</code></td>
                        <td><strong>{tokens_display}</strong> токенов</td>
                        <td style="text-align: center; font-size: 14px;">Базовый</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "prompt")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "completion")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "input_cache_read")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_price_cell(ep, "input_cache_write")}</td>
                        <td style="text-align: right; font-size: 14px;">{format_extra_prices(ep)}</td>
                        <td style="white-space: nowrap; line-height: 2.1;">
                            <button class="btn" onclick="selectProviderFromRow(this)">Выбрать</button><br>
                            <label style="font-size: 12px; cursor: pointer;" title="Добавить в сравнение затрат">
                                <input type="checkbox" class="cmp-check" onchange="toggleCompare(this)"> сравнить
                            </label>
                        </td>
                    </tr>{vp_rows}'''
        catalog_html += '''
                </tbody>
            </table>
        </div>'''

    return catalog_html


def render_html(settings, catalog_data):
    html = HTML_TEMPLATE

    # Значения фильтров из настроек
    html = html.replace("<!-- FILTER_MODEL_ID -->", esc(settings.get("filter_model_id", "")))
    html = html.replace("<!-- FILTER_CONTEXT -->", esc(settings.get("filter_context", "")))
    html = html.replace("<!-- FILTER_INPUT_PRICE -->", esc(settings.get("filter_input_price", "")))
    html = html.replace("<!-- FILTER_OUTPUT_PRICE -->", esc(settings.get("filter_output_price", "")))
    html = html.replace("<!-- FILTER_CACHE_PRICE -->", esc(settings.get("filter_cache_price", "")))

    # Состояние чекбоксов
    html = html.replace("<!-- LOGGING_CHECKED -->", "checked" if settings.get("logging_enabled", True) else "")
    html = html.replace("<!-- AUTO_OPEN_CHECKED -->", "checked" if settings.get("auto_open_on_block", False) else "")
    html = html.replace("<!-- OPENCODE_REPLACE_CHECKED -->", "checked" if settings.get("opencode_config_allow_replace", False) else "")
    html = html.replace("<!-- OPENCODE_CONFIG_PATH -->", esc(settings.get("opencode_config_path", "")))

    # Дата обновления кэша моделей
    cache_date = get_cache_info()
    html = html.replace("<!-- CACHE_DATE -->", cache_date)

    # Профиль
    if settings.get("selected_model") and settings.get("selected_provider"):
        frozen = settings.get("frozen_prices", {})
        model_name = get_model_display_name(settings["selected_model"], catalog_data)
        selected_ep = get_selected_endpoint(settings["selected_model"], settings["selected_provider"], catalog_data)
        context_len = selected_ep.get("context_length") or 0
        output_len = selected_ep.get("max_completion_tokens") or 0
        provider_name = selected_ep.get("provider_name") or ""
        quantization = selected_ep.get("quantization") or ""
        prov_display = f'<span class="profile-val">{esc(provider_name)}</span>' if provider_name else ""
        if quantization:
            prov_display += f' <small style="color:gray;">{esc(quantization)}</small>'
        tag_display = f'<span class="profile-val">{esc(settings["selected_provider"])}</span>'
        # Обе величины в одну строку; пропускаем те, которых нет в каталоге
        tokens_display = " / ".join(
            t for t in (format_tokens(context_len), format_tokens(output_len)) if t != "—"
        )
        extra_prices = format_extra_prices(selected_ep, inline=True)
        extra_html = f'<p><strong>Дополнительно:</strong><br>{extra_prices}</p>' if extra_prices else ''
        context_html = (f'<p><strong>Контекст / Выход:</strong> '
                        f'<strong>{tokens_display}</strong> токенов</p>'
                        if tokens_display else '')
        links_html = f'''
            <p style="margin-top: 8px;">
                <a href="https://routerai.ru/models/{esc(settings["selected_model"])}" target="_blank" style="font-size: 13px; color: #007bff; text-decoration: none;">[Страница модели ↗]</a>
                <a href="https://routerai.ru/api/v1/models/{esc(settings["selected_model"])}/endpoints" target="_blank" style="font-size: 13px; color: #28a745; text-decoration: none; margin-left: 10px;">[Endpoints ↗]</a>
            </p>'''
        profile_html = f'''
            <p style="margin-bottom: 10px;">
                <span style="font-size: 16px;"><strong>{esc(model_name)}</strong></span>
                <span style="color: #666; font-size: 13px; margin-left: 10px;">ID: <code>{esc(settings["selected_model"])}</code></span>
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
    catalog_html = render_catalog_sections(catalog_data, settings)
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
            atomic_write(CACHE_FILE, json.dumps(payload, ensure_ascii=False))
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


def patch_opencode_config(text: str, port: int, new_context: int, new_output: int):
    """Правит порт и лимиты в тексте конфига OpenCode, ничего больше не трогая.

    Возвращает (новый_текст, список_изменений, найдены_ли_ключи).
    Правка текстовая: комментарии, отступы и остальные настройки сохраняются.
    """
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

    changes = []
    if port_changed:
        changes.append(f"порт {old_port} → {port}")
    if limits_changed:
        if old_ctx is not None and old_ctx != new_context:
            changes.append(f"context {old_ctx} → {new_context}")
        if old_out is not None and old_out != new_output:
            changes.append(f"output {old_out} → {new_output}")

    found_any = (m is not None) or (key_idx != -1)
    return text, changes, found_any


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
            write_log("Прокси", f"Ошибка при отображении главной страницы: {e}")

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

            update_settings(lambda s: s.__setitem__("auto_open_on_block", enabled))
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

            update_settings(lambda s: s.__setitem__("logging_enabled", enabled))

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

            update_settings(lambda s: s.__setitem__("opencode_config_allow_replace", enabled))
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

            update_settings(lambda s: s.__setitem__("opencode_config_path", path))
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

            new_text, changes, found_any = patch_opencode_config(text, port, new_context, new_output)

            if not found_any:
                self._send_json({
                    "status": "ok",
                    "message": "Ключи не найдены — используйте «Выгрузить конфиг OpenCode»"
                })
                return

            if new_text == original:
                self._send_json({"status": "ok", "message": "Уже актуально, изменений не требуется"})
                return

            # --- Бэкап перед первой записью ---
            backup_path = cfg_path + ".bak"
            if not os.path.exists(backup_path):
                atomic_write(backup_path, original)

            atomic_write(cfg_path, new_text)

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
            atomic_write(out_path, json.dumps(config, ensure_ascii=False, indent=2))

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
            catalog_html = render_catalog_sections(catalog_data, settings)

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
            cache_write = float((selected_ep or {}).get("pricing", {}).get("input_cache_write", 0))

            chosen = {
                "filter_model_id": params.get("filter_model_id", [""])[0],
                "filter_context": params.get("filter_context", [""])[0],
                "filter_input_price": params.get("filter_input_price", [""])[0],
                "filter_output_price": params.get("filter_output_price", [""])[0],
                "filter_cache_price": params.get("filter_cache_price", [""])[0],
                "selected_model": model_id,
                "selected_provider": provider_tag,
                "frozen_prices": {
                    "prompt": prompt,
                    "completion": completion,
                    "input_cache_read": cache,
                    "input_cache_write": cache_write,
                    "variable_pricings": variable_pricings
                },
                # Выбор провайдера — это явное принятие текущих цен
                "is_blocked": False,
                "block_reason": "",
            }

            def apply_choice(s):
                s.update(chosen)

            update_settings(apply_choice)

            # Возвращаем обновленные данные профиля
            cached = load_catalog_cache()
            model_name = get_model_display_name(model_id, cached)
            selected_ep = get_selected_endpoint(model_id, provider_tag, cached)
            context_len = selected_ep.get("context_length") or 0
            output_len = selected_ep.get("max_completion_tokens") or 0
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
                    "context": format_tokens(context_len),
                    "output": format_tokens(output_len),
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
                    write_log("Прокси", f"Ошибка снятия блокировки: {e}")
            def lift_block(s):
                s["is_blocked"] = False
                s["block_reason"] = ""
                # Цены перечитываем из каталога: снятие блокировки — это
                # согласие с текущими ценами, поэтому они становятся эталоном
                try:
                    cached = load_catalog_cache()
                    selected_ep = get_selected_endpoint(s.get("selected_model"), s.get("selected_provider"), cached)
                    if selected_ep:
                        prices = selected_ep.get("pricing") or {}
                        s["frozen_prices"] = {
                            "prompt": prices.get("prompt", 0),
                            "completion": prices.get("completion", 0),
                            "input_cache_read": prices.get("input_cache_read", 0),
                            "input_cache_write": prices.get("input_cache_write", 0),
                            "variable_pricings": selected_ep.get("variable_pricings") or []
                        }
                except Exception as e:
                    write_log("Прокси", f"Не удалось перечитать цены при снятии блокировки: {e}")

            update_settings(lift_block)
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

            filters = {
                "filter_model_id": params.get("filter_model_id", [""])[0],
                "filter_context": params.get("filter_context", [""])[0],
                "filter_input_price": params.get("filter_input_price", [""])[0],
                "filter_output_price": params.get("filter_output_price", [""])[0],
                "filter_cache_price": params.get("filter_cache_price", [""])[0],
            }
            update_settings(lambda s: s.update(filters))
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

            filters = {
                "filter_model_id": params.get("filter_model_id", [""])[0],
                "filter_context": params.get("filter_context", [""])[0],
                "filter_input_price": params.get("filter_input_price", [""])[0],
                "filter_output_price": params.get("filter_output_price", [""])[0],
                "filter_cache_price": params.get("filter_cache_price", [""])[0],
            }
            update_settings(lambda s: s.update(filters))

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

    def _reject_chat(self, status, message, err_type="invalid_request_error", code=None):
        """Отклоняет запрос к чату с логированием причины."""
        write_log("Прокси", f"Отклонено: {message}")
        self.send_openai_error(status, message, err_type=err_type, code=code)

    def _check_chat_allowed(self, settings):
        """Проверяет, можно ли обрабатывать запрос. Возвращает ключ или None."""
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
            return None

        if not settings.get("selected_model") or not settings.get("selected_provider"):
            self._reject_chat(400, "Модель или провайдер не выбраны в веб-панели управления!")
            return None

        api_key, key_error = resolve_api_key(settings)
        if key_error:
            # Не логируем сам ключ — только причину (и имя переменной, если ссылка)
            write_log("Прокси", f"Отклонено: {key_error}")
            self.send_openai_error(401, key_error, err_type="authentication_error", code="invalid_api_key")
            return None

        return api_key

    def handle_chat_completions(self):
        settings = load_settings()
        api_key = self._check_chat_allowed(settings)
        if api_key is None:
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
            # stdin закрыт (запуск без терминала, systemd, nohup) — это отказ,
            # а не согласие: пустая строка здесь не должна открывать браузер
            result["answer"] = None

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

    # Фоновый монитор цен. Живёт здесь, а не на уровне модуля: иначе импорт
    # proxy.py (например, из тестов) поднимал бы лишний поток.
    monitor_thread = threading.Thread(target=price_monitor_loop, daemon=True)
    monitor_thread.start()

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

    # Предупреждаем о нерабочем ключе сразу, а не при первом запросе из OpenCode
    _key, _key_error = resolve_api_key(load_settings())
    if _key_error and "Переменная окружения" in _key_error:
        write_log("Прокси", f"Внимание: {_key_error}")
        # write_log молчит при выключенном логировании — дублируем в консоль
        print(f"[Внимание] {_key_error}")

    # Запрашиваем открытие страницы
    prompt_open_browser()

    # Основной поток ждёт прерывания
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nОстановка сервера...")
        server.shutdown()
