# Бэкенд (`promloader/*.py`)

_Обновлено: 2026-10-11 · версия 2.9.0_

## Модули

| Модуль | Строк | Назначение · ключевое |
|---|---|---|
| `main.py` | 1596 | FastAPI: маршруты, страницы (`PAGES`), фоновые задачи (`*_worker`), `lifespan` |
| `suppliers.py` | 887 | Поставщики: `run`/`_run`, `apply_items`/`_apply_one` (пропуск `ignored`), `recompute_offer` (несколько поставщиков), `recalc_prices`, `bulk_prices` (`as_cost`/`percent`/`recalc`), `link_item` (строка прайса ↔ товар, снимает `ignored`, кроме удаляемого товара), `_busy` (одно действие с поставщиком за раз: обновление, предпросмотр, удаление), `preview` (что изменит обновление), `failed` (сбойные поставщики), `_fetch_source` (скачанный прайс — во временный файл до успеха), `deleted_items`/`restore_deleted(skus)`, защита от битого прайса |
| `products.py` | 862 | Товары: `normalize`, `validate`, `create`/`update`/`delete`, `_touch` (ревизия, статус, `pending_fields`, журнал), `_lock` (🔒 ручные поля), `list_products(flt, sort, …)` / `_rows` (фильтры `FILTER_KEYS`, сортировки `SORTS`; «с ошибками» — через `validate` в Python; + закупка в грн, прошлая цена; счётчики с тем же фильтром), `search_clause` (поиск без учёта регистра через `lower_u`), `ids_for_filter` («все по фильтру»), `bulk_edit` (`BULK_FIELDS`), `export_xlsx`, `set_status`, фото |
| `sync.py` | 594 | Очередь выгрузки: `enqueue`, `run_once`, `_start`/`_start_quick`/`_poll`, ожидание чужого импорта (`BUSY_RETRY_SECONDS`), `repair_false_success`, `worker` (+ `promdelete.process`) |
| `excel.py` | 455 | Разбор файлов и XML в таблицу, `build_products`, `money_currency`, `clean_name`/`clean_names` |
| `ai.py` | 587 | Gemini/Claude, `ACTIONS`, авто-выбор модели Gemini; `run_detailed` → (изменения, расход); каждый запрос (и неудачный) пишется через `aiprice.record`, источник — `usage_source(source, job_id)`; лимиты: Gemini 429 → `_gemini_quota` (QuotaFailure/RetryInfo, `FreeTier` сам ставит `gemini_free_tier`, `limit: 0` = модель недоступна ключу), Claude — заголовки `anthropic-ratelimit-*` через `with_raw_response` (`_claude_limits`); `fallbacks` только для `FALLBACK_MODELS`, без `effort` для старых (`NO_EFFORT`); `claude_models` (Models API, бесплатно), `list_models`, `check_models` (не меняет выбор: `_gemini(switch=False)`); «Авто» при лимите конкретной модели (`_model_quota`: `PerModel` или `limit: 0`) переходит к следующей (до `AUTO_TRIES`), `_usable` ставит исчерпанные (`aiprice.limited_now`) и недоступные в конец; ключ: `clean_key` (при сохранении), `key_warning`, `_key_failed` → `AIError(key_error=True)` + `aiprice.set_key_error`; `AIError.retry_after`/`quota`; `human_wait` |
| `aiprice.py` | 220 | Цены моделей `PRICES` ($ за 1 млн, дата `PRICES_CHECKED`), `price` (точно / по семейству / неизвестна), `cost` (бесплатный Gemini = 0), `record` → `ai_usage` + статус модели, `summary`/`today`/`job_spent`/`average_cost`, `model_statuses`/`set_model_status(retry_after → until)`/`limited_now`, `key_errors`/`set_key_error`/`clear_key_error` (снимается удачным запросом, проверкой и новым ключом), `limits`/`save_limits`, `usd_rate`/`to_uah` (без ожидания интернета: свой курс или сохранённый НБУ, свежий — в фоне) |
| `db.py` | 392 | Схема, `MIGRATIONS` (ALTER TABLE ADD COLUMN), `tx()` (вложенный — часть внешнего), `in_transaction`, `query`, `get/set_setting`, SQL-функция `lower_u`, `IN_LIST` + `as_list` (список id одним параметром), `default_dir` (папка данных до открытия базы) |
| `r2.py` | 267 | Cloudflare R2 (SigV4), загрузка фото, постоянные ссылки |
| `support.py` | 263 | Обращения в поддержку: скриншоты, архив, отправка в Telegram |
| `tray.py` | 249 | Значок у часов, перезапуск, присмотр |
| `schedule.py` | 235 | Выгрузка по расписанию, пропущенные запуски |
| `updater.py` | 260 | Самообновление с GitHub по `VERSION`; `pending-check` пишется до копирования, `_put_back` — откат при сбое |
| `phototunnel.py` | 235 | Временный публичный адрес для фото, `/feed/<hex>.xml`; класс `Tunnel` (cloudflared, `probe`, `reachable`) — общий с `phone.PhoneTunnel` |
| `rates.py` | 259 | Курс НБУ (кеш на день), настройки курса, `Table.rate` (свой курс поставщика), `coverage`, `check_changed`, `mark_applied` |
| `diagnose.py` | 213 | «Проверка выгрузки» одного товара по шагам |
| `promdelete.py` | 196 | Удаление с Prom: `check`, `request`, `process`, `retry`, `cancel` |
| `promcatalog.py` | 204 | Каталог с Prom: `load` (страницы по `last_id`), `_upsert`, `reconcile`, `recover` (прерванная загрузка при запуске); товар без внешнего ID на Prom — `_own_id` (код, если не занят другим товаром Prom, иначе `PROM-номер`) и пометка `prom_no_ext`; запись ID на Prom через API — `run_ext_ids` → `write_ext_ids` (живой каталог; пропуск, если на Prom уже другой ID или ID занят; проба `EXT_TRIAL` = 2, затем остальные; `_ext_xlsx` — номер, название и код как на Prom, ID; `EXT_IMPORT_SETTINGS` с `updated_fields: ["sku"]` — без него Prom стирает цену и наличие, а «Ідентифікатор_товару» пишет всегда; `_verify` по `by_external_id` + цена, наличие, название и код не изменились; `_confirm` снимает `prom_no_ext`), ход — `ext_*` в `prom_catalog_state` |
| `prom_api.py` | 183 | `PromClient`, `PromError(retryable, busy)`, `DEFAULT_IMPORT_SETTINGS` (`mark_missing_product_as: none`), `import_state` |
| `aibulk.py` | 196 | Массовый ИИ, откат; `estimate` (цена до запуска), `get` → `spent` |
| `orders.py` | 297 | Заказы: `poll` (всё изменённое с прошлой полной проверки — `last_modified_from` с запасом `OVERLAP`, страницы по `last_id`, незаконченная загрузка продолжается по `orders_cursor`), `_where` (поиск: номер, телефон, имя, товар; даты), `TRANSITIONS` (допустимые переходы), `set_status` (проверяет `processed_ids`), уведомления о новых — `_notify` |
| `notify.py` | 241 | Telegram-уведомления по получателям: `send` кладёт в очередь `tg_outbox`, `flush` отправляет по порядку для каждого получателя, повтор с паузой до часа, через 48 ч — отказ; `outbox_state` |
| `backup.py` | 235 | zip-копии базы, фото и прайсов |
| `pricing.py` | 160 | `Pricer`: `to_uah`, `price`, `apply`; правила наценки и округление |
| `launcher.py` | 140 | Запуск сервера и браузера |
| `changes.py` | 232 | Журнал: `source()` (contextvar «кто»), `record`, `record_diff`, `search` (+ `revertable`), `revert` (`REVERTABLE`), `preview(fn)` (пробный прогон: выполнить и откатить, итог по журналу), `to_csv`, `cleanup` (180 дней) |
| `housekeeping.py` | 110 | Суточная уборка после копии (`run`): старые задачи выгрузки (кроме нужных `promdelete._ever_sent`), история поставщиков сверх 100, файлы импорта/фида, фото без товара, журнал изменений, `diagnose.RUNS` |
| `feed.py` | 107 | YML-фид для импорта Prom |
| `config.py` | 58 | Настройки: переменные окружения важнее сохранённых |
| `i18n_extract.py` | 269 | Русские строки интерфейса для украинского словаря: `collect()` (HTML, JS, сообщения Python; без логов, заданий ИИ, регулярок, SQL, проверок `in`/`==`), `dictionary()` (`static/i18n-uk.js`), `missing()`; `python -m promloader.i18n_extract` — что не переведено |
| `categories.py` | 290 | Категории каталога Prom: справочник `prom_categories` (`remember`, `info`, `label`), `for_product`, `overview` (по группам: категории на Prom, расхождения), `set_group`/`preview_group` (правило группы → товары без 🔒 через `_touch`), `from_catalog` (из `/products/list`), `refresh`, `suggest` (поиск prom.ua: `topHitsCategory`), `from_url` (`categoryId` на странице категории), `mismatch_rows`/`mismatch_xlsx` (файл для кабинета: `GET /api/categories/mismatch.xlsx`) |
| `reports.py` | 420 | Отчёты: `period_bounds`, `_load_orders` (заказы → позиции, комиссия `cpa_commission`, причина отмены), `_link_products` (по `prom_id`, затем артикулу — одним запросом), `_cost_history`/`_cost_at` (закупка на дату заказа по журналу, иначе текущая и «≈»), `sales`, `products_report` (ABC, `dead_stock`), `suppliers_report`, `store_report`, `build`, `xlsx` |
| `phone.py` | 326 | Доступ с телефона: пароль (`hash_password` pbkdf2, `check_password`), сессия (`make_session` / `valid_session`, `sign_out_everywhere`), `LoginLimiter`, `client_kind`, `from_this_computer`, `lan_addresses` / `lan_urls`, `qr_svg` (segno), `PhoneTunnel` (cloudflared к самой программе, подкласс `phototunnel.Tunnel`), `keep_tunnel` (шаг `phone_worker`, новый адрес → Telegram «phone»), `status` |
| `chat.py` | 270 | Чат с покупателями Prom: `poll` (комнаты → история только изменившихся, `_store`, уведомление «chat» о новых сообщениях покупателя, при первом опросе — без уведомлений), `rooms`, `messages`, `unseen`, `send` (`user_id` из `room_ident`), `mark_read`, `ai_draft` (`ai.chat_reply`) |
| `autostart.py`, `runtime.py` | 48, 38 | Автозапуск Windows; перезапуск из веб-сервера |

## API (`main.py`)

| Группа | Маршруты |
|---|---|
| Товары | `GET /api/products` (`status, q, supplier, group, presence, on_prom, no_photo, gone, errors, sort`), `GET /api/products.xlsx` (те же фильтры), `POST /api/products/bulk-edit` (`fields` + `ids`/`filter`), `POST /api/products`, `GET/PATCH /api/products/{id}`, `…/{id}/ai`, `…/{id}/unlock`, `…/{id}/duplicate`, `…/{id}/changes`, `POST …/{id}/prom-link` (номер на Prom по артикулу, ссылка `products.prom_url` на кабинет), `POST /api/products/status` |
| Фото | `POST …/{id}/images`, `…/images/url`, `…/images/order`, `DELETE …/images/{image_id}`, `GET /media/{name}` |
| Удаление | `POST /api/products/delete` (`ids`, `prom`), `…/delete/check`, `…/delete/retry`, `…/delete/cancel` |
| Выбор товаров | массовые действия (`/api/sync`, `/api/products/status`, `…/prices`, `…/bulk-edit`, `…/currencies`, `…/delete`, `…/delete/check`, `/api/ai/bulk`) принимают `ids` **или** `filter` `{status, q, supplier}` — `main._selected` |
| Цены и курс | `GET/PUT /api/pricing`, `POST /api/pricing/test`, `/api/pricing/apply`, `/api/products/prices`, `/api/products/currencies`, `GET/PUT /api/rates`, `GET /api/rates/{code}` |
| Журнал | `GET /api/changes`, `GET /api/changes.csv`, `POST /api/changes/{id}/revert` |
| Выгрузка | `POST /api/sync`, `GET /api/sync/jobs`, `…/jobs/{id}/file`, `…/jobs/{id}/retry`, `GET /api/sync/photos`, `POST /api/diagnose`, `GET /api/diagnose/{run_id}`, `GET /feed/prom.yml`, `GET /feed/{name}.xml` |
| Импорт файла | `POST /api/import/upload`, `GET /api/import/{token}/sheet`, `…/image`, `POST …/preview`, `…/commit` |
| Поставщики | `GET/POST /api/suppliers`, `GET/PATCH/DELETE /api/suppliers/{id}`, `…/source`, `…/open`, `…/run`, `GET …/deleted`, `POST …/restore-deleted` (`skus`, `run`), `POST …/preview` |
| Prom | `GET/POST /api/prom/catalog`, `GET /api/prom/external-ids` (сколько и пример), `POST /api/prom/external-ids/write` (запись в фоне), `GET /api/orders` (`status, q, date_from, date_to, limit, offset` → `items, total, counts`), `POST /api/orders/refresh`, `…/seen` (`ids`; без них — все), `…/{id}/status` |
| ИИ | `POST /api/ai/check`, `GET/POST /api/ai/bulk`, `POST /api/ai/bulk/estimate` (`action` + `ids`/`filter`), `…/{job_id}/status`, `…/{job_id}/revert`; окно «✨ ИИ»: `GET /api/ai/status` (выбор, расходы, лимиты, `usd_rate`), `GET /api/ai/models` (цены, статусы), `POST /api/ai/models/check` `{models}`, `POST /api/ai/model` `{provider?, model?, free_tier?}` (ключ — только в «Настройках»); `…/products/{id}/ai` → `{changes, usage}`; `/api/meta.ai` — модель, `auto`, `free`, `today_*` |
| Доступ с телефона | `GET /login`, `POST /api/login` (`password`, `next`; 10 неудач за 15 мин → 429), `POST /api/logout`, `GET/POST /api/phone` (`password?`, `lan?`, `remote?` → состояние, `restarting`), `POST /api/phone/sign-out-all`, `GET /api/phone/qr.svg?text=` |
| Настройки и сервис | `GET/POST /api/settings`, `…/check`, `GET /api/meta` (`access`: local / phone), `/api/update/check`, `/api/update/install`, `/api/backups*`, `/api/shutdown`, `/api/restart`, `/api/autostart`, `/api/schedules*`, `/api/r2/check`, `/api/r2/stats` |
| Telegram и поддержка | `/api/telegram/recipients*` (+ `outbox`), `POST /api/telegram/retry`, `/api/telegram/candidates`, `/api/support*`, `/api/support/channel/chats` |

## Правила, которые легко нарушить

- Доступ: middleware `same_site_only` (`main.py`) → `phone.client_kind`. Изменяющие запросы принимаются только с
  Origin своей страницы. `/media/` и `/feed/` открыты для Prom. В тестах `conftest.py` разрешает адрес `testserver`.
  - **«Свой» компьютер** — `127.0.0.1` без `Cf-Connecting-Ip` и с адресом из `_allowed_hosts`. Решает настоящий
    адрес клиента, а не `Host`: когда программа слушает сеть (`runtime.lan_bound`), `Host` подделывается.
  - **«Телефон»** — нужна кука `pl_phone` (HMAC на `phone_secret`, 30 дней), иначе страница → `/login?next=`, API →
    401 `{login: true}`. Телефон — это:
    - клиент из сети при `lan_bound` и включённом Wi‑Fi;
    - запрос через cloudflared (`127.0.0.1` + `Cf-Connecting-Ip`) на адрес туннеля `phone.tunnel` при включённом
      доступе из интернета.
  - Всё остальное → 403.
  - `APP_PASSWORD` (Docker) — Basic, как раньше.
  - Пароль, доступ, QR — `/api/phone*`, только `access == "local"`. `/api/shutdown` — только
    `phone.from_this_computer`.
- Никаких запросов в интернет внутри `db.tx()`: курс заранее — `rates.prefetch()`.
- Эндпоинты — обычные `def` (FastAPI выполняет их в пуле потоков). `async def` — только если нужен цикл событий
  (запрос к Prom через `PromClient`, чтение загружаемого файла, фоновая задача); тогда работа с базой — через
  `asyncio.to_thread`. Список разрешённых `async` — `ASYNC_OK` в `tests/test_regressions.py`.
- Публичный адрес (`public_base_url`, туннель) открывает только `/media/` и `/feed/`; в `_allowed_hosts` его нет.
  Origin сверяется с `Host` и `X-Forwarded-Host` (`_same_origin`).
- Список id в SQL — `f"id {db.IN_LIST}"` + `db.as_list(ids)`, а не `IN (?, ?, …)` (в SQLite на Windows не больше 32 766 параметров).
- Настройки (`POST /api/settings`, `rates.save_settings`) — одной транзакцией: ошибка в поле не оставляет половину записанной.
- Ошибки ввода `ValueError`/`TypeError`/`OverflowError` → 400 (`bad_input`), `KeyError` → 404.
- `lifespan`: `backup.apply_pending(db.default_dir())` до `db.init()` (Docker без START.bat), `promcatalog.recover()`, остановка воркеров через `gather(return_exceptions=True)` и закрытие туннеля.
- Восстановление копии: `backup._allowed` (только база, `uploads/`, `suppliers/`), `integrity_check`, настройки обновлений (`DEVICE_SETTINGS`) берутся с этого компьютера.

- Все изменения товара — через `products.update`/`_touch`: иначе нет журнала, `pending_fields` и смены статуса.
- «Кто поменял» задаётся `with changes.source("…")` вокруг операции (поставщик, курс, ИИ, импорт, Prom).
- Prom запускает импорты по одному: `sync` ждёт и не шлёт второй. Импорт закончен, только когда счётчики покрыли все товары файла (`prom_api.import_counted`).
- Товары `sending` не удаляются, `deleting` не отправляются; кнопки статусов их не трогают.
- Статус пишется только с условием на текущий (`AND status = 'sending'`, `NOT IN ('sending', 'deleting')`); `products._touch` сам читает статус внутри транзакции — переданный аргумент устарел.
- Цена без правила наценки: закупка в гривнах — цена не трогается; закупка в валюте — РРЦ (или закупка) по курсу. Розница в $/€ из прайса — это РРЦ (`excel.money_currency`). Свой курс поставщика — только для его `rate_currency`.
- Строка прайса с непонятной ячейкой не выбрасывается: `build_products` отдаёт `field_errors`, `apply_items` пропускает только строки с ошибками проверки (`validate`).
- Удалённые вами товары поставщика помечаются `supplier_items.ignored = 1` и при обновлении прайса не создаются заново; если товар с тем же артикулом снова есть — поставщик привязывает его и метку снимает (`_apply_one`).
- «Что изменится» — только через `changes.preview(fn)`: внутри fn никаких запросов в интернет и записи файлов (курс — `rates.prefetch()` заранее, картинки — `embedded={}`).
- Поиск — только через `products.search_clause` (`lower_u(...) LIKE`): обычный `LIKE` в SQLite не понимает регистр кириллицы.
- `sync.enqueue` без токена Prom ничего не ставит в очередь (в тестах токен задаёт `conftest.py`).
- Уведомления — только через `notify.send` (очередь): прямой `send_to` — лишь для «проверить получателя».
- Журнал программы — `main.setup_log_file` (ротация 5 МБ × 3, `data/logs/promloader.log`); вывод процесса и строки значка у часов — `logs/console.log` (`tray.log_path`). Один файл не пишут два процесса.
- `/api/meta.first_steps` — для «Первых шагов»: `token`, `photos` (`r2`/`site`/`tunnel`/`off`), `products`, `suppliers`, `sent`.
- Страницы (`main._page`) отдаются с `?v=<версия>` в ссылках на `/static/*.js|css`, сами файлы — с `Cache-Control: no-cache` (`StaticNoCache`). Иначе после обновления браузер брал старые скрипты к новому HTML.
- Внешний ID существующего товара на Prom через API не поменять (`/products/edit` его не принимает) — только импортом Excel в кабинете. Если у магазина пакет без API, Prom отвечает 403 «Api is not available…» — `PromError.no_api`.
- Импорт Prom с ошибками строк (`prom_api.error_positions`: `errors[].errors[].positions`, `with_errors_count` при этом 0)
  остаётся «в процесі» и блокирует новые импорты, пока его не отменят в кабинете — `sync.busy_text`/`CABINET_HINT`.
  Файл импорта без колонки поля = «очистить поле», если поле есть в `updated_fields` (проверено 2026-10-10).
- Видео: `products.videos` (JSON, ссылки `https://www.youtube.com/watch?v=ID`) — `youtube_url`/`youtube_id`/`split_videos`/`parse_videos`
  (`MAX_VIDEOS` = 5), в `EDITABLE`/`LOCKABLE` и `suppliers.SYNC_FIELDS`; прайсы — `excel.TARGETS["videos"]`, `VIDEO_HINT`
  (раньше подсказки названия; `param:Відео` → видео; `<video url>`). На Prom — только iframe YouTube в описании
  (`feed.with_videos`, `VIDEO_IFRAME`): теги `<video>`, `<videos>`, `<video_url>`, `<video_link>` Prom пропускает (2026-10-11).
  Видео в галерею товара (как в кабинете) импортом не передать (проверено 2026-10-11): Excel-колонки «Посилання_відео»,
  «Посилання_на_відео», «Відео», «Відео_товару», «Ссылка_видео», «Видео», «Відеоогляд» Prom пропускает; YouTube в
  `<picture>`/«Посилання_зображення» — берёт только картинку; `/products/edit` на `videos`/`video` — «Unknown field».
- ИИ «Перевести на русский» — `ai.ACTIONS["translate_ru"]`; без украинского текста запрос не делается
  (`ai.nothing_to_translate`; массовый ИИ такие товары пропускает).
- Украинский интерфейс переводит готовый текст на экране, поэтому сообщение для пользователя — одна фраза-шаблон
  (`f"Не удалось …: {err}"`), а не склейка из кусков: так её найдёт словарь (`ai.human_wait` — пример). Новая строка
  без перевода валит `tests/test_i18n.py` — добавьте её в `static/i18n-uk.js` (`python -m promloader.i18n_extract`
  покажет какие). Telegram, меню значка у часов, заголовки Excel/CSV остаются русскими.
- Категория Prom — две колонки: `prom_category_id` (что стоит на Prom, пишет только каталог: `promcatalog._upsert`,
  `categories.refresh`) и `target_category_id` (что нужно: правило `group_categories` или своё с 🔒 — `ALWAYS_LOCK`,
  закрепляется у любого товара). Смена группы в `_touch` берёт категорию новой группы. В фид — только если
  `feed.CATEGORY_TAG` задан (способ передачи проверяется на живом Prom; до этого `None`).
- Отчёты: отменённые заказы не входят в выручку и прибыль. Прибыль = позиции − закупка (грн, на дату заказа) −
  комиссия Prom. Товар заказа ищется по `products.prom_id`: внешний ID в заказах Prom обычно пустой.
- Чат Prom: история — только по `room_ident` (`room_id` Prom не принимает), ответ — `/chat/send_message` с `user_id`
  (первая часть `room_ident`), `body`, `project`; «прочитано» — `room_id` + `message_id`. Prom отвечает 200 со
  `status: error` — это ошибка (`PromClient._chat`). Порядок сообщений — по номеру (время Prom без пояса).
  `chat_worker` — раз в минуту.
