# Инструкция по настройке RouterAI Proxy

## 1. Требования и установка

### Требования
- **Python 3.6+**
- **Только стандартная библиотека** — внешние пакеты (pip install) не нужны

### Файлы проекта

| Файл | Назначение |
|------|-----------|
| `proxy.py` | основной скрипт |
| `proxy_settings.json` | настройки (**создаётся при первом запуске**) |
| `proxy.log` | лог работы прокси и загрузки моделей |
| `catalog_cache.json` | кэш каталога моделей (создаётся при обновлении) |
| `opencode.jsonc` | конфиг OpenCode (создаётся кнопкой «Выгрузить конфиг OpenCode») |

### Первый запуск

> ⚠️ **Сначала запустите прокси, и только потом редактируйте настройки.**
> Файл `proxy_settings.json` не существует до первого запуска — скрипт создаёт его сам со значениями по умолчанию.

```bash
cd ~/bin/routerai-proxy
python3 proxy.py
```

При старте скрипт:
1. **Сам снимает предыдущие экземпляры** — ищет через `/proc/*/cmdline` процессы `python ... proxy.py` и отправляет SIGTERM (при необходимости SIGKILL), затем освобождает порт 5000
2. Создаёт `proxy_settings.json` со значениями по умолчанию (если его нет)
3. Выводит запрос:
   ```
   Открыть страницу управления в браузере? (Y/n):
   ```
   Ответ `Y` / `Enter` / `д` / `да` — откроет `http://127.0.0.1:5000`. Таймаут ответа — 5 секунд.

> Порт: **5000** (настраивается в `proxy_settings.json`, поле `port`), адрес панели: **http://127.0.0.1:5000**

### Смена порта

Если порт 5000 занят другим приложением, укажите свой в `proxy_settings.json`:
```json
{
  "port": 5555,
  ...
}
```
и перезапустите прокси. Панель будет доступна на `http://127.0.0.1:5555`.

Порт используется везде автоматически:
- адрес панели и запрос «Открыть страницу управления?»
- `baseURL` в выгружаемом `opencode.jsonc` — `http://127.0.0.1:<port>/v1`
- ссылка на панель в ошибках, которые видит OpenCode
- авто-открытие браузера при блокировке по цене
- освобождение порта при рестарте (Linux — `/proc`+сигналы, Windows — `netstat`+`taskkill`)

Допустимый диапазон: `1`–`65535`. При некорректном значении или занятом порте скрипт сообщит об этом и подскажет, что менять.

---

## 2. Настройка API-ключа

Выполняется **после первого запуска** (см. шаг 1) — иначе файла настроек не существует.

1. Получите ключ на https://routerai.ru (профиль → API)
2. Откройте созданный файл настроек:
   ```bash
   nano ~/bin/routerai-proxy/proxy_settings.json
   ```
3. Замените placeholder `"ВАШ_API_КЛЮЧ_ЗДЕСЬ"`:
   ```json
   {
     "api_key": "sk-ваш-реальный-ключ",
     ...
   }
   ```

Остановка прокси для смены ключа **не требуется** — все параметры читаются с диска при каждом запросе.

### Все параметры конфига

| Параметр | Тип | Значение |
|----------|-----|----------|
| `api_key` | строка | ключ RouterAI |
| `port` | число | порт прокси (по умолчанию `5000`) |
| `selected_model` | строка | ID выбранной модели (например `openai/gpt-5-nano`) |
| `selected_provider` | строка | тег провайдера (например `openai/flex`) |
| `frozen_prices` | объект | зафиксированные цены: `prompt`, `completion`, `input_cache_read`, `input_cache_write`, `variable_pricings` |
| `is_blocked` | bool | `true` — запросы заблокированы (выросла цена) |
| `block_reason` | строка | причина блокировки с цифрами «было → стало» |
| `check_interval_sec` | число | интервал проверки цен монитором (по умолчанию `600` = 10 мин) |
| `filter_model_id` | строка | фильтр по подстроке в ID модели |
| `filter_context` | строка | фильтр: минимум контекста (тыс. токенов) |
| `filter_input_price` | строка | фильтр: макс. цена входа (₽/1М) |
| `filter_output_price` | строка | фильтр: макс. цена выхода (₽/1М) |
| `filter_cache_price` | строка | фильтр: макс. цена кэша (₽/1М) |
| `logging_enabled` | bool | писать лог в `proxy.log` (по умолчанию `true`) |
| `auto_open_on_block` | bool | открыть панель управления при блокировке по цене (по умолчанию `false`) |
| `opencode_config_path` | строка | путь к существующему конфигу OpenCode (по умолчанию пусто) |
| `opencode_config_allow_replace` | bool | разрешить правку существующего конфига OpenCode (по умолчанию `false`) |

---

## 3. Настройка OpenCode Desktop

### Способ А — кнопка в панели (рекомендуется)

1. Откройте http://127.0.0.1:5000
2. Выберите модель и провайдера
3. В блоке **«Настройки»** нажмите **«Выгрузить конфиг OpenCode»**
4. Рядом появится путь созданного файла: `~/bin/routerai-proxy/opencode.jsonc`
5. Скопируйте его в конфиг OpenCode:
   ```bash
   cp ~/bin/routerai-proxy/opencode.jsonc ~/.config/opencode/opencode.jsonc
   ```

Кнопка подставляет `limit.context` из контекста **выбранного провайдера** — при смене провайдера нажмите её повторно.

### Способ Б — вручную

```json
{
  "$schema": "https://opencode.ai",
  "disabled_providers": [],
  "provider": {
    "custom": {
      "name": "RouterAI",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:5000/v1",
        "apiKey": "local-auth-not-needed"
      },
      "models": {
        "routerai/proxy-model": {
          "name": "RouterAI Контролируемая Модель",
          "limit": { "context": 1048576, "output": 131000 }
        }
      }
    }
  },
  "compaction": { "auto": true, "reserved": 15000 }
}
```

### Ключевые параметры

- **`baseURL`**: `http://127.0.0.1:5000/v1` — именно с `/v1`. OpenCode сам дописывает `/chat/completions`, в итоге запрос уходит на `/v1/chat/completions`.
- **`apiKey`**: значение не важно (`local-auth-not-needed`) — реальный ключ RouterAI берётся из `proxy_settings.json`.
- **Модель**: ID `routerai/proxy-model` — именно это имя прокси перехватывает и подменяет на реальную модель+провайдера.

### Зачем нужен `limit`

```json
"limit": { "context": 1048576, "output": 131000 }
```

Это **не** ограничение провайдера, а declaration для OpenCode: на сколько токенов он может рассчитывать.

| Поле | Смысл | Откуда брать |
|------|-------|--------------|
| `context` | сколько токенов истории диалога вмещается | `context_length` выбранного провайдера |
| `output` | сколько токенов reserved под ответ модели | `max_completion_tokens` провайдера |

**Почему это наше значение, а не модели:** за прокси стоит **одна** виртуальная модель `routerai/proxy-model`, но реальная модель и провайдер меняются в любой момент. У разных провайдеров одной модели контекст отличается — например, `deepseek/deepseek-v4-flash-0731` имеет провайдеров на 1048K, 1000K и 262K. OpenCode обязан получить конкретное число, поэтому мы задаём его сами.

**На что влияет:**
- `context` **завышен** → OpenCode не сожмёт историю вовремя, провайдер вернёт ошибку переполнения контекста
- `context` **занижен** → OpenCode будет сжимать историю раньше, чем нужно, теряя контекст диалога
- `output` **завышен** → возможен отказ провайдера (превышен его лимит completion)

Поэтому кнопка выгрузки берёт значения из каталога RouterAI для конкретного провайдера.

### Нужен ли `compaction`

```json
"compaction": { "auto": true, "reserved": 15000 }
```

**Да, в нашем случае нужен.** Разбор:

| Параметр | Что делает |
|----------|-----------|
| `auto: true` | OpenCode автоматически сжимает историю диалога при приближении к лимиту `context` |
| `reserved: 15000` | всегда оставляет 15 000 токенов свободными под ответ модели |

**Почему особенно в нашем случае:**
- Провайдеры меняются на лету прямо из панели, и у каждого свой реальный лимит контекста
- Длинный диалог неизбежно упрётся в лимит — без `compaction` провайдер просто вернёт ошибку
- С `auto: true` OpenCode сам сожмёт историю, и чат продолжит работать без ручного вмешательства
- `reserved` гарантирует, что после сжатия останется место для generation ответа

**Что учитывать:** `compaction` работает от того `context`, который вы указали в `limit`. Если он не совпадает с реальным лимитом провайдера — сжатие сработает не в тот момент. Поэтому актуальный `limit.context` из кнопки выгрузки + `compaction: auto: true` дают предсказуемое поведение.

### Обновление существующего конфига

Если конфиг OpenCode уже настроен и содержит другие ваши провайдеры и настройки — не пересоздавайте его. Прокси умеет **точечно** обновлять только свои значения.

**В блоке «Настройки»:**
1. ☑ **Разрешить изменение существующего конфига** — без этой галочки правка невозможна (и на странице, и через API)
2. **Путь к конфигу OpenCode** — например `~/.config/opencode/opencode.jsonc`
3. **Обновить конфиг OpenCode** — кнопка активна только при включённом чекбоксе

**Что меняется (и только это):**

| Что | Как ищется |
|-----|-----------|
| Порт в `baseURL` | по подписи `http://127.0.0.1:<порт>/v1` внутри вашего провайдера |
| `limit.context` / `limit.output` | внутри блока модели `routerai/proxy-model` |

Значения берутся из панели: порт из настройки `port`, лимиты — от выбранного провайдера.

**Что гарантированно не затрагивается:**
- Комментарии, отступы и оформление файла (правка текстовая, без JSON-парсинга)
- Другие провайдеры в `provider.*`
- Любые другие ветки и поля
- Если ни один ключ не найден — файл не изменяется вовсе

**Бэкап:** перед первой правкой создаётся `opencode.jsonc.bak` с исходным содержимым.

> Файл должен **существовать** — прокси его не создаёт. Если путь не указан или файла нет, кнопка сообщит об этом и ничего не запишет.


### Что делает прокси с запросом
```
OpenCode → POST /v1/chat/completions {model: "routerai/proxy-model"}
                 ↓
    подмена на "{selected_model}@provider={selected_provider}&allow_fallbacks=false"
                 ↓
    POST https://routerai.ru/api/v1/chat/completions (с реальным ключом)
                 ↓
    ответ (включая streaming) транслируется обратно в OpenCode
```

Маршрутизация устойчива к вариациям пути: `/v1/chat/completions/`, `/v1/chat/completions?stream=true` — всё попадает в обработчик (`rstrip('/')` + `urlparse`).

---

## 4. HTTP-эндпоинты

| Метод | Путь | Назначение |
|-------|------|-----------|
| GET | `/` | главная страница управления (каталог рендерится из кэша) |
| GET | `/v1/models` | список моделей для OpenCode (`routerai/proxy-model`) |
| POST | `/v1/chat/completions` | проксирование запросов |
| GET | `/api/catalog` | каталог из файла кэша |
| GET | `/api/catalog?refresh=1` | принудительная загрузка из API + перезапись кэша |
| GET | `/api/cache_date` | дата последнего обновления каталога |
| GET | `/api/price_changes` | статус блокировки и причина |
| POST | `/select` или `/api/select` | выбор модели/провайдера |
| POST | `/save_filters` или `/api/save_filters` | сохранение фильтров |
| POST | `/api/toggle_logging` | вкл/выкл логирование |
| POST | `/api/toggle_auto_open` | вкл/выкл авто-открытие панели при блокировке |
| POST | `/api/export_opencode_config` | создать `opencode.jsonc` в папке скрипта |
| POST | `/api/update_opencode_config` | обновить порт/лимиты в существующем конфиге OpenCode |
| POST | `/api/toggle_opencode_replace` | разрешить/запретить правку конфига OpenCode |
| POST | `/api/save_opencode_path` | сохранить путь к конфигу OpenCode |
| POST | `/unblock` | снятие блокировки с обновлением цен |

---

## 5. Остановка

- **Вручную**: `Ctrl+C` в терминале
- **Linux**: `pkill -f "python3.*proxy.py"`
- **Windows**: `taskkill /F /IM python.exe` (снимет все Python-процессы)

Автоснятие предыдущего экземпляра при старте избавляет от ошибки `OSError: [Errno 98] Address already in use`.

---

## 6. Запуск под Linux

### Проверка Python

В большинстве дистрибутивов Python 3 уже есть. Проверьте:
```bash
python3 --version
```
Если написано `Python 3.x.y` — всё готово, переходите к запуску.

### Установка Python 3 (если отсутствует)

| Дистрибутив | Команда |
|-------------|---------|
| Debian / Ubuntu / Mint | `sudo apt update && sudo apt install -y python3` |
| Fedora / RHEL / CentOS | `sudo dnf install -y python3` |
| Arch / Manjaro | `sudo pacman -S python` |
| openSUSE | `sudo zypper install -y python3` |

После установки повторите проверку `python3 --version`.

### Запуск

```bash
cd ~/bin/routerai-proxy
python3 proxy.py
```

При первом старте в этой же папке появятся `proxy_settings.json` и (после обновления каталога) `catalog_cache.json`, а запросы начнут писаться в `proxy.log`.

> Останавливать — `Ctrl+C`. Если процесс нужно снять принудительно: `pkill -f "python3.*proxy.py"`.

### Автозапуск (опционально)

```bash
sudo nano /etc/systemd/system/routerai-proxy.service
```

```ini
[Unit]
Description=RouterAI Proxy Server
After=network.target

[Service]
Type=simple
User=user
WorkingDirectory=/home/user/bin/routerai-proxy
ExecStart=/usr/bin/python3 /home/user/bin/routerai-proxy/proxy.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable routerai-proxy
sudo systemctl start routerai-proxy
sudo systemctl status routerai-proxy
```

Замените `user` на своё имя пользователя (узнать: `whoami`).

---

## 7. Запуск под Windows

### Что установить

1. **Python 3.6+** — https://www.python.org/downloads/
   - При установке **обязательно** отметьте галочку **«Add python.exe to PATH»**
   - Проверка: откройте Командную строку (Win+R → `cmd`) и выполните
     ```cmd
     python --version
     ```
     Должна вывестись версия, например `Python 3.12.1`. Если `не найдено` — Python не в PATH, переустановите с галочкой.

2. **Сам прокси** — скопируйте папку `routerai-proxy` целиком (вместе с `proxy.py`) в любое место, например `C:\routerai-proxy`. Внешних пакетов (**pip install**) не требуется.

### Как запускать

Откройте **Командную строку** в папке скрипта:

```cmd
cd C:\routerai-proxy
python proxy.py
```

Или проще: в Проводнике откройте папку, в адресной строке наберите `cmd` и нажмите Enter — консоль откроется уже в нужной папке.

При старте появится запрос:
```
Открыть страницу управления в браузере? (Y/n):
```
Нажмите `Y` или `Enter` — откроется `http://127.0.0.1:5000`.

### Удобный запуск (ярлык)

Создайте файл `start.bat` в папке скрипта:
```bat
@echo off
cd /d %~dp0
python proxy.py
pause
```
Дальше запускайте двойным щелчком по `start.bat`. Строка `pause` не даст окну закрыться сразу при ошибке.

### Отличия от Linux

| Аспект | Linux | Windows |
|--------|-------|---------|
| Освобождение порта при старте | через `/proc` + `SIGTERM/SIGKILL` | через `netstat -ano` + `taskkill /F /PID` |
| Ручная остановка | `Ctrl+C` или `pkill -f "python3.*proxy.py"` | `Ctrl+C` в консоли, либо Диспетчер задач |
| Ручное освобождение порта | `lsof -i :5000` + `kill <PID>` | `netstat -ano \| findstr :5000` → `taskkill /F /PID <PID>` |

Скрипт сам определяет ОС (`os.name == "nt"`) и выбирает нужный механизм — вручную ничего настраивать не нужно.

### Автозапуск

1. Win+R → `shell:startup` → Enter (откроется папка автозагрузки)
2. Создайте там ярлык для `start.bat`
3. Прокси будет стартовать вместе с системой

> Порт берётся из `proxy_settings.json` (по умолчанию `5000`). Если он занят — укажите другой в поле `port` и перезапустите.

Пути к файлам те же: `proxy_settings.json`, `proxy.log`, `catalog_cache.json` и `opencode.jsonc` создаются рядом со скриптом — в `C:\routerai-proxy\`. Конфиг OpenCode копируется в `%USERPROFILE%\.config\opencode\opencode.jsonc`.

---

## 8. Проверка работы

1. Запустите прокси: `python3 proxy.py`
2. Откройте http://127.0.0.1:5000
3. Укажите API-ключ в `proxy_settings.json`
4. Нажмите **«Обновить модели»** (первый раз — загрузка из API)
5. Выберите модель и провайдера
6. Нажмите **«Выгрузить конфиг OpenCode»** и скопируйте `opencode.jsonc` в `~/.config/opencode/`
7. Отправьте сообщение в OpenCode Desktop

### Быстрая диагностика

```bash
# Список моделей (должен вернуть routerai/proxy-model)
curl -s http://127.0.0.1:5000/v1/models

# Дата кэша каталога
curl -s http://127.0.0.1:5000/api/cache_date

# Статус блокировки
curl -s http://127.0.0.1:5000/api/price_changes

# Выгрузить конфиг OpenCode в папку скрипта
curl -s -X POST http://127.0.0.1:5000/api/export_opencode_config

# Тестовый запрос через прокси
curl -s -X POST http://127.0.0.1:5000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"routerai/proxy-model","messages":[{"role":"user","content":"привет"}]}'
```

### Что смотреть в `proxy.log`

```
[Запрос] POST /v1/chat/completions от 127.0.0.1 (Content-Length: 2383)
[Прокси] Запрос от клиента: model='routerai/proxy-model', сообщений=2
         -> подмена на 'openai/gpt-5-nano@provider=openai/flex&allow_fallbacks=false', stream=True
[Прокси] Ответ RouterAI: статус 200, content-type=text/event-stream
[Прокси] Стриминг завершён: отправлено 19271 байт клиенту
```

Тела запросов и ответов в лог **не пишутся** — только метаданные.
