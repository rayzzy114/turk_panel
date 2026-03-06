# SMM Panel

Панель для автоматизации Facebook-задач с веб-интерфейсом на FastAPI, браузерным воркером на Camoufox/Playwright и SQLite по умолчанию.

Проект умеет:

- управлять аккаунтами, прокси и задачами из одной панели;
- проверять логин аккаунта через браузерную сессию;
- выполнять warmup-сессии и сохранять подробную историю warmup;
- различать типы Facebook checkpoint и сохранять их в состоянии аккаунта;
- импортировать аккаунты из обычного colon-формата и турецкого shop-формата;
- работать с mobile proxy и вызывать принудительную ротацию IP;
- парсить комментарии Facebook через Apify;
- отправлять provider-заказы через MoreThanPanel.

## Стек

- Python 3.11+
- FastAPI
- Uvicorn
- SQLAlchemy async
- SQLite по умолчанию
- Camoufox + Playwright
- Jinja2 + Tailwind CDN + Font Awesome
- httpx

## Структура проекта

- `api.py` — основной FastAPI app, маршруты, bootstrap БД, миграции SQLite
- `worker.py` — браузерная автоматизация Facebook, логин, warmup, checkpoint/captcha handling
- `models.py` — модели БД и enum-ы
- `crud.py` — вспомогательные операции с аккаунтами и прокси
- `import_data.py` — парсинг и импорт аккаунтов/прокси
- `iproxy_utils.py` — rotation endpoint и проверка внешнего IP через прокси
- `apify_api.py` — клиент для Apify comments scraper
- `mtp_api.py` — клиент для MoreThanPanel
- `templates/index.html` — основной UI
- `tests/` — pytest-набор

## Быстрый старт

### 1. Установка зависимостей

```bash
python -m venv .venv
source .venv/bin/activate
pip install uv
uv sync
playwright install chromium
playwright install-deps chromium
```

Для Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install uv
uv sync
playwright install chromium
playwright install-deps chromium
```

Если `uv sync` недоступен в вашей среде, можно использовать:

```bash
uv pip install -r pyproject.toml
```

### 2. Настройка `.env`

Минимальный пример:

```env
DATABASE_URL=sqlite+aiosqlite:///./smm_panel_demo.db

ADMIN_LOGIN=admin
ADMIN_PASS=admin

FB_HEADLESS=true
FB_USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36

MORETHAN_API_URL=https://morethanpanel.com/api/v2
MORETHAN_API_KEY=
MORETHAN_LIKE_ID=0
MORETHAN_FOLLOW_ID=0
MORETHAN_LIKE_COMMENT_ID=0

APIFY_API_TOKEN=
APIFY_ACTOR_ID=apify/facebook-comments-scraper
APIFY_RESULTS_LIMIT=50
APIFY_INCLUDE_REPLIES=false
APIFY_VIEW_OPTION=RANKED_UNFILTERED
APIFY_TIMEOUT_SECONDS=90

MAX_CONCURRENT_TASKS=3

FB_KEEP_BROWSER_ON_CHECKPOINT=true
FB_CHECKPOINT_WAIT_SECONDS=600
FB_CHECKPOINT_POLL_SECONDS=5
```

## Запуск

Локально:

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

С autoreload для разработки:

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Панель будет доступна по адресу `http://127.0.0.1:8000/` или `http://<server-ip>:8000/`.

## Аутентификация

- Панель использует HTTP Basic Auth.
- Логин и пароль берутся из `ADMIN_LOGIN` и `ADMIN_PASS`.

## База данных и миграции

- По умолчанию используется SQLite: `sqlite+aiosqlite:///./smm_panel_demo.db`
- Таблицы создаются автоматически при старте.
- Легкие миграции SQLite выполняются прямо из `api.py` через `_migrate_schema_if_needed(...)`.
- Для новых колонок используется текущий паттерн: `PRAGMA table_info` + `ALTER TABLE ... ADD COLUMN`, без Alembic.

## Что есть в панели

### Аккаунты

- список аккаунтов с proxy/status/checkpoint metadata;
- ручное обновление email/proxy/proxy type;
- проверка входа;
- warmup аккаунта;
- bulk-операции по аккаунтам;
- удаление, бан и shadow-ban.

### Warmup

- warmup-сессия сохраняет отдельный `WarmupLog`;
- в логе хранятся:
  - время старта и завершения;
  - длительность;
  - сколько действий было запланировано, успешно и с ошибкой;
  - подробный `action_log` по шагам;
  - `result` и `error_message`.
- история warmup доступна через API и в UI.

### Checkpoint handling

Поддерживается различение checkpoint-типов:

- `code_verification`
- `face_verification`
- `suspicious_login`
- `account_disabled`
- `unknown_checkpoint`

Что происходит:

- code/suspicious обычно идут в flow получения кода через email/IMAP;
- face verification не решается автоматически, для нее делается screenshot в `./screenshots/`;
- тип последнего checkpoint сохраняется в `Account.last_checkpoint_type`.

### Прокси

Поддерживаются типы:

- `datacenter`
- `residential`
- `mobile`

Для mobile proxy:

- можно хранить `proxy_rotation_url`;
- перед стартом сессии воркер может вызывать ротацию IP;
- есть отдельный API endpoint для ручной ротации;
- внешний IP можно проверить через `ipify`.

### Импорт аккаунтов

Поддерживаются два формата:

1. Colon format

```text
login:password:email:email_password
```

2. Turkish shop format

```text
facebook giriş: 61581112340247   şifre: l51dxqwk033e11   mail: tillielarriva36@kh-mail.com   mail şifre: 75f7797d8073
```

Что делает импорт:

- автоматически определяет формат строки;
- пытается определить `imap_server` из email;
- возвращает построчный результат импорта;
- может пометить строку как `mobile` proxy import, если в ней найден `rotation_url`.

### Импорт прокси

- есть API и UI для загрузки прокси;
- прокси назначаются аккаунтам с учетом занятости и статуса аккаунтов.

### Задачи

Поддерживаются типы действий:

- `like_post`
- `follow`
- `comment_post`
- `like_comment`
- `like_comment_bot`
- `reply_comment`
- `check_login`

Часть действий уходит во внешний provider через MoreThanPanel, часть выполняется браузерным ботом.

### Парсер комментариев

- используется Apify actor `facebook-comments-scraper`;
- UI умеет парсить комментарии по URL поста;
- ответы и debug-информация возвращаются через API.

## Основные API endpoints

Ниже не полный список, а основные рабочие точки:

- `GET /` — UI панели
- `GET /api/accounts`
- `PUT /api/accounts/{account_id}`
- `POST /api/accounts/{account_id}/check_login`
- `POST /api/accounts/{account_id}/warmup`
- `GET /api/accounts/{account_id}/warmup/logs`
- `POST /api/accounts/{account_id}/rotate-ip`
- `POST /api/accounts/import`
- `POST /api/accounts/upload`
- `GET /api/proxies`
- `POST /api/proxies/import`
- `POST /api/tasks`
- `GET /api/tasks`
- `POST /api/parse_comments`
- `GET /api/balance`

## UI

- серверный HTML-шаблон находится в `templates/index.html`;
- стили: Tailwind CDN;
- иконки: Font Awesome 5;
- новые UI-статусы должны использовать иконки, а не emoji.

## Тесты и качество

Проверка стиля и типизации:

```bash
uvx ruff check .
uvx ty check
```

Полный запуск тестов:

```bash
uv run pytest -q
```

Точечные тесты по областям:

```bash
uv run pytest tests/test_worker_actions.py -q
uv run pytest tests/test_import_data.py -q
uv run pytest tests/test_api_hybrid.py -q
uv run pytest tests/test_logic_comprehensive.py -q
```

## Полезные замечания

- Если меняются модели или API, обновляйте `SESSION_CONTEXT.md` вместе с кодом.
- Для новых изменений в схеме SQLite придерживайтесь существующего idempotent-паттерна миграций.
- Для отладки face-checkpoint screenshots сохраняются в `./screenshots`.
- `main.py` не является entrypoint сервера. Актуальный запуск идет через `uvicorn api:app`.
