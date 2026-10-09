# Данные (`promloader/db.py`)

_Обновлено: 2026-10-09 · версия 2.3.0_

SQLite `promloader.sqlite3` в папке данных (`PROMLOADER_DATA`), рядом `uploads/` (фото) и файлы прайсов.
Новые колонки добавляются через `MIGRATIONS` (только `ADD COLUMN`); при первом запуске новой версии миграция идёт сама.

| Таблица | Что хранит · важные колонки |
|---|---|
| `products` | Товар. `external_id` (артикул = id оффера на Prom), тексты RU/UA, `price`/`currency` (розница), `cost_price`/`rrp` + `cost_currency` (закупка в своей валюте), `presence`, `quantity`, `params` (JSON), `status`, `last_error`, `revision`, `synced_at`, `prom_id`, `supplier_id`, `locked_fields` (🔒), `pending_fields` (что не отправлено), `vendor_code`, `barcode`, `delete_next_at`/`delete_attempts`, `prom_no_ext` (в кабинете Prom у товара пустой «Ідентифікатор_товару») |
| `images` | Фото товара: `file` (в `uploads/`) или `url`, `position` |
| `sync_jobs` | Задачи выгрузки: `kind` (`import`/`quick`), `status` (`pending`/`waiting`/`done`/`failed`), `products` (JSON {id: revision}), `import_id`, `attempts`, `next_run_at`, `result`, `started_at`, `sent` |
| `suppliers` | Поставщик: источник, `mapping`, `header_row`, `prefix`, `interval_hours`, `new_status`, `auto_sync`, `missing_action`, `merge_by_barcode`, `rate_mode`/`rate_currency`/`rate_value`/`rate_add` (свой курс — только для `rate_currency`), `clean_names` |
| `supplier_items` | Строка прайса ↔ товар: `(supplier_id, sku)` уникально, `product_id` (SET NULL при удалении товара), `data`, `images_hash`, `missing`, `seen_run`, `ignored` (удалён вами — не создавать заново; снимается возвратом или когда товар с тем же артикулом снова есть, но не пока товар удаляется с Prom) |
| `supplier_runs` | История обновлений поставщика: `status`, `stats` (JSON), `message` |
| `price_rules` | Наценка: поставщик, группа, диапазон закупки, `markup_percent`, округление |
| `product_changes` | Журнал: `product_id`, `at`, `source` (кто), `field`, `old`, `new`; хранится 180 дней |
| `schedules` | Выгрузка по расписанию |
| `ai_jobs`, `ai_items` | Массовый ИИ и откат |
| `ai_usage` | Каждый запрос к ИИ: `at`, `provider`, `model` (ответившая), `action`, `source` (`card`/`bulk`/`check`), `job_id`, `tokens_in`/`tokens_out` (мышление — в выходе), `cost_usd` (NULL — цена неизвестна), `ok`, `error` |
| `orders` | Заказы Prom |
| `tg_recipients` | Получатели Telegram и их события |
| `tg_outbox` | Очередь уведомлений Telegram: `chat_id`, `text`, `next_at`, `attempts`, `last_error`, `sent_at`, `failed` (через 48 ч без доставки); отправленные хранятся 30 дней |
| `support_tickets` | Обращения в поддержку |
| `r2_objects` | Фото, загруженные в Cloudflare R2 |
| `settings` | Ключ → значение (см. ниже) |

## Ключи `settings` (основные)

- Prom и фото: `prom_token`, `prom_api_base`, `public_base_url`, настройки R2, `import_method`, `import_plain_v2`, `import_settings`, `quick_updates`
- ИИ: `ai_provider`, `gemini_key`, `anthropic_key`, `gemini_model` (пусто — авто), `gemini_model_used`, `claude_model`, `ai_rate`, `gemini_free_tier` (`1`/`0`/пусто — не знаем), `ai_model_status` (JSON `provider:model` → статус), `ai_limits` (JSON: Claude — остаток из заголовков, Gemini — `last_limit`)
- Курс: `rate_mode` (`nbu`/`manual`), `rate_add`, `rate_manual` (JSON), `rate_auto_send`, `rate_applied` (курс последнего пересчёта), `nbu_rate_<КОД>` (кеш на день)
- Состояния: `prom_catalog_state` (ход и итог загрузки каталога, в т. ч. `missing_on_prom`); заказы — `orders_synced_at` (начало последней полной проверки), `orders_cursor` (незаконченная загрузка), `orders_initialized`, `orders_last_poll`, `orders_error`
- Переменные окружения важнее сохранённых значений (`config.py`).
