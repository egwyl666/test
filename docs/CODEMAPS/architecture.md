# Архитектура

_Обновлено: 2026-10-05 · версия 1.6.1 · сверено с кодом `promloader/`_

Локальное приложение для Windows: FastAPI-сервер + страницы на чистом JS + SQLite. Пользователь работает в браузере
на `localhost`; Prom.ua получает копию данных через публичное API. Всё введённое сначала сохраняется в базе,
отправка на Prom — отдельный шаг через очередь.

```
START.bat → launcher.py (свободный порт, браузер) → tray.py (значок у часов, присмотр)
                     │
                     ▼
              main.py (FastAPI: ~95 маршрутов /api/*, страницы, /media, /feed)
                     │ фоновые задачи (lifespan, если PROMLOADER_WORKER != 0)
   ┌─────────────────┼──────────────────────────────────────────────────────────┐
   sync.worker (2 с)  supplier_worker (30 с)  schedule_worker (20 с)  ai_worker
   ├ очередь выгрузки  └ suppliers.run         └ schedule              └ aibulk
   └ promdelete.process
   rates_worker (час)  orders_worker  support_worker  r2_worker  maintenance_worker (час: копия, чистка журнала, обновления)
                     │
                     ▼
               db.py (SQLite, db.tx() — вложенный вызов входит во внешнюю транзакцию)
                     │
                     ▼
     prom_api.PromClient (httpx) ──► Prom.ua: import_file / import_url / import/status /
                                     edit_by_external_id / edit (удаление) / by_external_id / list / orders
```

## Главные потоки

| Поток | Путь |
|---|---|
| Прайс поставщика → товары | `suppliers.run` → `excel` (разбор XML/Excel/CSV, очистка названий) → `pricing.Pricer` (курс `rates.Table` + наценка) → `products._touch` (+ журнал `changes`) → при `auto_sync` `sync.enqueue` |
| Импорт файла | `/api/import/*` → `excel.build_products` → `products.create/update` (источник «Импорт файла») |
| Выгрузка на Prom | `sync.enqueue` → задача `sync_jobs` (`quick` — только цена/наличие через `edit_by_external_id`; `import` — YML-фид `feed.build` через `import_file`/`import_url`) → `_poll` пока Prom не отчитается по всем товарам → `_finish_products` |
| Фото для Prom | Cloudflare R2 (`r2.py`), временный туннель (`phototunnel.py`) или `PUBLIC_BASE_URL` |
| Удаление с Prom | `promdelete.request` → статус `deleting` → `promdelete.process` (по `prom_id` или артикулу, `/products/edit status=deleted`) → удаление из базы; повторы с паузой 1 мин…1 ч |
| Сверка с Prom | `promcatalog.run`: каталог Prom → товары программы; затем `reconcile` — «На Prom», но на Prom нет → `error` |
| Курс | `rates_worker` раз в час `rates.check_changed` → `suppliers.recalc_prices` → новые цены на Prom (если `rate_auto_send`) |

## Статусы товара

`draft` → `ready` → `sending` → `synced` / `error`; `deleting` — ждёт подтверждения удаления на Prom.
Любое изменение выгруженного товара (`products._touch`) возвращает его в `ready` и копит `pending_fields`
(только цена/наличие → быстрое обновление).

## Где что

- Бэкенд: [backend.md](backend.md)
- Страницы и JS: [frontend.md](frontend.md)
- Таблицы и настройки: [data.md](data.md)
- Тесты: `tests/` (pytest, Prom и НБУ подменяются `httpx.MockTransport`; `conftest.py` — курс 40 по умолчанию)
- План развития: `ПЛАН-РАЗВИТИЯ.md`
- Документы для пользователя: `КАК-ЗАПУСТИТЬ.md`, `ПРОВЕРКА-ПОСЛЕ-УСТАНОВКИ.md`, `ЧТО-НОВОГО.md`; версия — `VERSION` (по ней работает самообновление из `main`)
