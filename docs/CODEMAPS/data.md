# Данные (`promloader/db.py`)

_Обновлено: 2026-10-08 · версия 2.0.0_

SQLite `promloader.sqlite3` в папке данных (`PROMLOADER_DATA`), рядом `uploads/` (фото) и файлы прайсов.
Новые колонки добавляются через `MIGRATIONS` (только `ADD COLUMN`); при первом запуске новой версии миграция идёт сама.

| Таблица | Что хранит · важные колонки |
|---|---|
| `products` | Товар. `external_id` (артикул = id оффера на Prom), тексты RU/UA, `price`/`currency` (розница), `cost_price`/`rrp` + `cost_currency` (закупка в своей валюте), `presence`, `quantity`, `params` (JSON), `status`, `last_error`, `revision`, `synced_at`, `prom_id`, `supplier_id`, `locked_fields` (🔒), `pending_fields` (что не отправлено), `vendor_code`, `barcode`, `delete_next_at`/`delete_attempts` |
| `images` | Фото товара: `file` (в `uploads/`) или `url`, `position` |
| `sync_jobs` | Задачи выгрузки: `kind` (`import`/`quick`), `status` (`pending`/`waiting`/`done`/`failed`), `products` (JSON {id: revision}), `import_id`, `attempts`, `next_run_at`, `result`, `started_at`, `sent` |
| `suppliers` | Поставщик: источник, `mapping`, `header_row`, `prefix`, `interval_hours`, `new_status`, `auto_sync`, `missing_action`, `merge_by_barcode`, `rate_mode`/`rate_currency`/`rate_value`/`rate_add` (свой курс — только для `rate_currency`), `clean_names` |
| `supplier_items` | Строка прайса ↔ товар: `(supplier_id, sku)` уникально, `product_id` (SET NULL при удалении товара), `data`, `images_hash`, `missing`, `seen_run`, `ignored` (удалён вами — не создавать заново; снимается возвратом или когда товар с тем же артикулом снова есть, но не пока товар удаляется с Prom) |
| `supplier_runs` | История обновлений поставщика: `status`, `stats` (JSON), `message` |
| `price_rules` | Наценка: поставщик, группа, диапазон закупки, `markup_percent`, округление |
| `product_changes` | Журнал: `product_id`, `at`, `source` (кто), `field`, `old`, `new`; хранится 180 дней |
| `schedules` | Выгрузка по расписанию |
| `ai_jobs`, `ai_items` | Массовый ИИ и откат |
| `orders` | Заказы Prom |
| `tg_recipients` | Получатели Telegram и их события |
| `support_tickets` | Обращения в поддержку |
| `r2_objects` | Фото, загруженные в Cloudflare R2 |
| `settings` | Ключ → значение (см. ниже) |

## Ключи `settings` (основные)

- Prom и фото: `prom_token`, `prom_api_base`, `public_base_url`, настройки R2, `import_method`, `import_plain_v2`, `import_settings`, `quick_updates`
- Курс: `rate_mode` (`nbu`/`manual`), `rate_add`, `rate_manual` (JSON), `rate_auto_send`, `rate_applied` (курс последнего пересчёта), `nbu_rate_<КОД>` (кеш на день)
- Состояния: `prom_catalog_state` (ход и итог загрузки каталога, в т. ч. `missing_on_prom`)
- Переменные окружения важнее сохранённых значений (`config.py`).
