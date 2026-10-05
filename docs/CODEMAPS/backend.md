# Бэкенд (`promloader/*.py`)

_Обновлено: 2026-10-05 · версия 1.9.0_

## Модули

| Модуль | Строк | Назначение · ключевое |
|---|---|---|
| `main.py` | 1289 | FastAPI: маршруты, страницы (`PAGES`), фоновые задачи (`*_worker`), `lifespan` |
| `suppliers.py` | 738 | Поставщики: `run`/`_run`, `apply_items`/`_apply_one` (пропуск `ignored`), `recompute_offer` (несколько поставщиков), `recalc_prices`, `bulk_prices` (`as_cost`/`percent`/`recalc`), `link_item` (строка прайса ↔ товар, снимает `ignored`), `preview` (что изменит обновление), `failed` (сбойные поставщики), `_fetch_source` (скачанный прайс — во временный файл до успеха), `deleted_items`/`restore_deleted(skus)`, защита от битого прайса |
| `products.py` | 631 | Товары: `normalize`, `validate`, `create`/`update`/`delete`, `_touch` (ревизия, статус, `pending_fields`, журнал), `_lock` (🔒 ручные поля), `list_products(flt, sort, …)` / `_rows` (фильтры `FILTER_KEYS`, сортировки `SORTS`; «с ошибками» — через `validate` в Python; + закупка в грн, прошлая цена; счётчики с тем же фильтром), `search_clause` (поиск без учёта регистра через `lower_u`), `ids_for_filter` («все по фильтру»), `bulk_edit` (`BULK_FIELDS`), `export_xlsx`, `set_status`, фото |
| `sync.py` | 544 | Очередь выгрузки: `enqueue`, `run_once`, `_start`/`_start_quick`/`_poll`, ожидание чужого импорта (`BUSY_RETRY_SECONDS`), `repair_false_success`, `worker` (+ `promdelete.process`) |
| `excel.py` | 442 | Разбор файлов и XML в таблицу, `build_products`, `money_currency`, `clean_name`/`clean_names` |
| `ai.py` | 373 | Gemini/Claude, `ACTIONS`, авто-выбор модели Gemini |
| `db.py` | 357 | Схема, `MIGRATIONS` (ALTER TABLE ADD COLUMN), `tx()`, `query`, `get/set_setting`, SQL-функция `lower_u` |
| `r2.py` | 267 | Cloudflare R2 (SigV4), загрузка фото, постоянные ссылки |
| `support.py` | 260 | Обращения в поддержку: скриншоты, архив, отправка в Telegram |
| `tray.py` | 247 | Значок у часов, перезапуск, присмотр |
| `schedule.py` | 235 | Выгрузка по расписанию, пропущенные запуски |
| `updater.py` | 233 | Самообновление с GitHub по `VERSION` |
| `phototunnel.py` | 229 | Временный публичный адрес для фото, `/feed/<hex>.xml` |
| `rates.py` | 224 | Курс НБУ (кеш на день), настройки курса, `Table.rate` (свой курс поставщика), `coverage`, `check_changed`, `mark_applied` |
| `diagnose.py` | 213 | «Проверка выгрузки» одного товара по шагам |
| `promdelete.py` | 194 | Удаление с Prom: `check`, `request`, `process`, `retry`, `cancel` |
| `promcatalog.py` | 193 | Каталог с Prom: `load` (страницы по `last_id`), `_upsert`, `reconcile` |
| `prom_api.py` | 183 | `PromClient`, `PromError(retryable, busy)`, `DEFAULT_IMPORT_SETTINGS` (`mark_missing_product_as: none`), `import_state` |
| `aibulk.py` | 177 | Массовый ИИ, откат |
| `orders.py` | 165 | Заказы: опрос, статусы |
| `notify.py` | 164 | Telegram-уведомления по получателям |
| `backup.py` | 161 | zip-копии базы, фото и прайсов |
| `pricing.py` | 151 | `Pricer`: `to_uah`, `price`, `apply`; правила наценки и округление |
| `launcher.py` | 140 | Запуск сервера и браузера |
| `changes.py` | 171 | Журнал: `source()` (contextvar «кто»), `record`, `record_diff`, `search` (+ `revertable`), `revert` (`REVERTABLE`), `preview(fn)` (пробный прогон: выполнить и откатить, итог по журналу), `to_csv`, `cleanup` (180 дней) |
| `feed.py` | 105 | YML-фид для импорта Prom |
| `config.py` | 58 | Настройки: переменные окружения важнее сохранённых |
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
| Prom | `GET/POST /api/prom/catalog`, `GET /api/orders`, `POST /api/orders/refresh`, `…/seen`, `…/{id}/status` |
| ИИ | `POST /api/ai/check`, `GET/POST /api/ai/bulk`, `…/{job_id}/status`, `…/{job_id}/revert` |
| Настройки и сервис | `GET/POST /api/settings`, `…/check`, `GET /api/meta`, `/api/update/check`, `/api/update/install`, `/api/backups*`, `/api/shutdown`, `/api/restart`, `/api/autostart`, `/api/schedules*`, `/api/r2/check`, `/api/r2/stats` |
| Telegram и поддержка | `/api/telegram/recipients*`, `/api/telegram/candidates`, `/api/support*`, `/api/support/channel/chats` |

## Правила, которые легко нарушить

- Доступ: middleware `same_site_only` (`main.py`) — только `localhost` (или `APP_PASSWORD` / `PROMLOADER_ALLOWED_HOSTS`), изменяющие запросы только с Origin своей страницы; `/media/` и `/feed/` открыты для Prom. В тестах `conftest.py` разрешает адрес `testserver`.
- Никаких запросов в интернет внутри `db.tx()`: курс заранее — `rates.prefetch()`; тяжёлые эндпоинты — `def` (пул потоков) или `asyncio.to_thread`.
- Восстановление копии: `backup._allowed` (только база, `uploads/`, `suppliers/`), `integrity_check`, настройки обновлений (`DEVICE_SETTINGS`) берутся с этого компьютера.

- Все изменения товара — через `products.update`/`_touch`: иначе нет журнала, `pending_fields` и смены статуса.
- «Кто поменял» задаётся `with changes.source("…")` вокруг операции (поставщик, курс, ИИ, импорт, Prom).
- Prom запускает импорты по одному: `sync` ждёт и не шлёт второй. Импорт закончен, только когда счётчики покрыли все товары файла (`prom_api.import_counted`).
- Товары `sending` не удаляются, `deleting` не отправляются; кнопки статусов их не трогают.
- Удалённые вами товары поставщика помечаются `supplier_items.ignored = 1` и при обновлении прайса не создаются заново; если товар с тем же артикулом снова есть — поставщик привязывает его и метку снимает (`_apply_one`).
- «Что изменится» — только через `changes.preview(fn)`: внутри fn никаких запросов в интернет и записи файлов (курс — `rates.prefetch()` заранее, картинки — `embedded={}`).
- Поиск — только через `products.search_clause` (`lower_u(...) LIKE`): обычный `LIKE` в SQLite не понимает регистр кириллицы.
- `sync.enqueue` без токена Prom ничего не ставит в очередь (в тестах токен задаёт `conftest.py`).
