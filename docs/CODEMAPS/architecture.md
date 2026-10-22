# Архитектура

_Обновлено: 2026-10-11 · версия 2.9.0 · сверено с кодом `promloader/`_

Локальное приложение для Windows: FastAPI-сервер + страницы на чистом JS + SQLite. Пользователь работает в браузере
на `localhost`; Prom.ua получает копию данных через публичное API. Всё введённое сначала сохраняется в базе,
отправка на Prom — отдельный шаг через очередь.

```
START.bat → launcher.py (свободный порт, браузер) → tray.py (значок у часов, присмотр)
                     │
                     ▼
              main.py (FastAPI: ~100 маршрутов /api/*, страницы, /media, /feed)
                     │ фоновые задачи (lifespan, если PROMLOADER_WORKER != 0)
   ┌─────────────────┼──────────────────────────────────────────────────────────┐
   sync.worker (2 с)  supplier_worker (30 с)  schedule_worker (20 с)  ai_worker
   ├ очередь выгрузки  └ suppliers.run         └ schedule              └ aibulk
   └ promdelete.process
   rates_worker (час)  orders_worker (5 мин; при догоне — 20 с)  notify_worker (мин: очередь Telegram)  support_worker  r2_worker
   maintenance_worker (час: суточная копия → housekeeping.run, обновления)
                     │
                     ▼
               db.py (SQLite, db.tx() — вложенный вызов входит во внешнюю транзакцию)
                     │
                     ▼
     prom_api.PromClient (httpx) ──► Prom.ua: import_file / import_url / import/status /
                                     edit_by_external_id / edit (удаление) / by_external_id / list / orders
```

## Сторонний код в интерфейсе

Только `static/vendor/zxing-0.21.3.min.js` (ZXing, Apache-2.0) — распознавание штрихкодов там, где у браузера нет
`BarcodeDetector`. Файл — как в npm, без изменений: sha256 записан в `vendor/README.md` и проверяется тестом
`tests/test_scanner.py`, который также читает им настоящий штрихкод ТТН (`tests/fixtures/ttn_code128.png`).

## Вход и права (2.23.0)

`main.same_site_only`: адрес клиента (`phone.client_kind`) → проверка Origin → `_sign_in_gate`: кука `pl_session`
(`users.session_user`) → нет пользователей — `/setup` (только с этого компьютера), нет входа — `/login` (API — 401
`{login, setup}`), нет права (`users.required`) — 403 `{forbidden}`. Открыто без входа: `PUBLIC_PATHS`, `/static/`,
`/media/`, `/feed/`. Имя вошедшего — в `changes._user` на время запроса (журнал пишет его в `product_changes.user`).

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
- Язык интерфейса (RU / UK): `static/i18n.js` + словарь `static/i18n-uk.js`, полнота — `promloader/i18n_extract.py` и `tests/test_i18n.py` ([frontend.md](frontend.md#язык-интерфейса-260))
- Тесты: `tests/` (pytest, Prom и НБУ подменяются `httpx.MockTransport`; `conftest.py` — курс 40 по умолчанию); `test_regressions.py` — по тесту на каждую ошибку из `ОТЧЁТ-ПРОВЕРКИ.md`, в т. ч. запуск программы в чистом процессе; «сторожа» (2.26.2): `test_contracts.py` — страница ↔ сервер (каждый адрес `/api/…` из JS/HTML есть на сервере, каждый `$("#id")` есть на странице), каждый модуль импортируется сам по себе, перевод сохраняет `{}` и значки, правила прав указывают на настоящие адреса; `test_invariants.py` — права каждой роли на каждом адресе, база без поздних колонок, параллельные правки, откат журнала по каждому полю, символы SQL в поиске, копия «туда и обратно», постраничный вывод без пропусков и повторов, «странные» заказы Prom (null, строки вместо объектов) не ломают страницы, ТТН, отчёты и уведомления, граница суток в отчётах; `test_v227.py` (2.27.0) — «↩ Отменить», цена от закупки в валюте через наценку и курс, все ручные переходы статусов, повторный / «рваный» / пустой прайс, логотип; `test_browser.py` — настоящий Chromium (Playwright): заготовка, отмена, горячие клавиши, «Пред./След.», список помнит место, строки не пересоздаются, телефон 390/360 без прокрутки вбок, украинский, логотип; без Playwright пропускается, в CI ставится отдельным шагом; `test_v229.py` (2.29.0) — списание остатка по выполненному заказу (один раз, не для старых и первой загрузки, странные заказы), «Сегодня», логотипы и цвет; `test_browser.py` дополнен темой, значками, «Сегодня», запертыми кнопками, нижней панелью; `test_browser_matrix.py` (2.29.3) — все страницы × роли admin / operator / viewer × компьютер 1366 и телефон 390 (+ тёмная тема), проверка изнутри страницы `tests/browser_audit.js`: ошибки JS и отказы сервера, вылезание вбок, обрезанное родителем и закрытое другим, кнопки мельче пальца (каждая страница в новой вкладке — эмуляция теряет `pointer: coarse`), контраст WCAG, битые картинки; у «просмотра» действия заказов и чата заперты; ссылка на закрытый раздел — «Нет доступа» с выходом; `test_v2293.py` — читаемый цвет логотипа, страница отказа, права «просмотра»; полный обход 4 ролей × 7 экранов и «нажать всё» — описаны в `docs/КАРТА-ПРОГРАММЫ.md`; `test_v228.py` (2.28.0) — `ERRORS_SQL` ≡ `validate` на всех сочетаниях, постраничный «с ошибками», остатки, заказ поставщику, уведомление «заканчивается»
- План развития: `ПЛАН-РАЗВИТИЯ.md`
- Документы для пользователя: `КАК-ЗАПУСТИТЬ.md`, `ПРОВЕРКА-ПОСЛЕ-УСТАНОВКИ.md`, `ЧТО-НОВОГО.md`; версия — `VERSION` (по ней работает самообновление из `main`)
