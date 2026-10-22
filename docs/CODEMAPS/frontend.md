# Страницы и JS (`promloader/static/`)

_Обновлено: 2026-10-22 · версия 2.29.0_

Чистый JS без сборки. Сторонние файлы — только в `static/vendor/` (версия, лицензия и sha256 — в `vendor/README.md`). Каждая страница подключает в `<head>` `i18n-uk.js` + `i18n.js` (язык интерфейса, см. ниже) и в конце `common.js` (`api()`, `$`/`$$`, `esc`, `toast`, `formatPrice`,
`formatDate`, `statusBadge`, `loadMeta` → `META` (+ полоски: обновление, нет токена Prom, сбой поставщика, пропущенная выгрузка), `showPreview` (окно «Что изменится»), `busy(btn, fn)` (кнопка занята на время запроса — без двойных действий), `sanitizeHtml` + `safeUrl` (в описании только http/https/mailto/tel), закрытие окон `.modal[id]` по Esc и клику по фону, `initNav`, кнопка «🆘 Не получается?»). `formatDate` показывает год, если он не текущий. Стили — `app.css`
(мобильная вёрстка до 640 px: `.hide-sm`/`.show-sm`). Меню — `NAV` в `common.js` (`renderNav`), в HTML пустой `<nav></nav>`. ИИ на любой странице: значок `#ai-chip` в `.topbar .right` (`renderAiChip` из `META.ai`, вызывается в `loadMeta`), окно `#ai-window` (`openAiWindow` → `/api/ai/status` + `/api/ai/models`: сервис, модели с ценой и статусом, «Проверить модели», бесплатный Gemini, расходы, лимиты, последние запросы); значок `.bad` и полоса `.ai-key-error` — ключ не принят (`META.ai.key_error`); `refreshAiMeta` обновляет значок и шлёт событие `ai-changed`; деньги — `aiMoney`/`fmtUah`/`fmtUsd`, `aiUsageLine`, имена — `aiModelName`. Там же: меню 👤 `renderUserMenu` (кто вошёл, «Сменить пароль» — `openPasswordWindow`, «Пользователи», «Выйти»), `can(perm)` по `META.me.perms`, `applyNavPerms` прячет пункты `NAV_PERMS` и элементы `[data-perm]` (классы `body.no-<право>` — действуют и на дорисованное потом); 401 → один раз `/login?next=` или `/setup` (`goToLogin`, без петли), 403 `{forbidden}` → `refreshPerms` (`/api/me`, полоска `#perms-changed`, событие `perms-changed`); `main[data-page-perm]` → `applyPagePerm` (страница только для просмотра, `.readonly-note`, поля закрыты и у дорисованного — MutationObserver); `showOffline` (полоска «Нет связи», ставится и снимается в `api()`), страховка `unhandledrejection` → toast, `details.actions-menu` (на ≤ 640 px — кнопка «Действия ▾», `syncActionMenus`).

| Адрес | Файлы | Что там |
|---|---|---|
| `/` | `index.html`, `list.js`, `scanner.js` | 📷 `#scan-product` — штрихкод товара в поиск; «Первые шаги» (`renderFirstSteps` по `META.first_steps`, «Скрыть» — `localStorage`). Список товаров: статусы, поиск, поставщик, фильтры (`FILTERS` — группа, наличие, «Ещё фильтры»), сортировка, «⬇ Excel»; вид списка в адресе (`syncUrl`, `sessionStorage promloader-list-url` для «← К списку»); «Ничего не найдено»; «✏ Изменить» (`openEditModal`); отметки + «выбрать все N по фильтру» (`selection()` → `{ids}` или `{filter}`); колонки «Закупка» и «Цена» (наценка, 🔒 вручную, «было …»); массовые действия: отправить, статусы, «Вернуть «На Prom»», ✨ ИИ (цена до запуска — `/api/ai/bulk/estimate`, у задания «потрачено»), 💲 Цены (опт → закупка с выбором валюты), удалить (окно «и на Prom / только из программы», в строке «Повторить» / «Отменить удаление»); очередь отправки; «⬇ Каталог с Prom» и итог сверки; «Как исправить» (`openExtIds`) → «Записать ID на Prom», ход записи в `showCatalogState` (`ext_*`, опрос раз в 5 с); полоска готовности черновиков `#readiness` (`/api/products/readiness`, `promote-ready`), «Подготовить для Prom» первым в массовом ИИ, «🗂 Подобрать группу и категорию Prom» (`match_category`, подпись из `META.ai.bulk_only`); «✨ Заполнить через ИИ» в полоске (`openAiModal(sel, {count, action})`), галочка `#ai-auto-promote`; фильтр «Выгода» `#f-profit`, сортировки `profit_desc/asc`, пункт полоски `needs_manual`; полоска `#list-alerts` (конфликты, дубли — `/api/products/alerts`; в режиме дублей — «★ выгодный» и кнопка `#dup-keep-best` → `/api/products/duplicates/keep-best`), пометки `.dup-note`/`.conflict-note`, «📷 Заполнить по фото» в полоске черновиков, `#ai-limit` |
| `/product?id=` | `product.html`, `product.js`, `preview.js`, `richtext.js` | Карточка: автосохранение и резерв в браузере, фото (перетаскивание, порядок), закупка и валюта, 🔒 закреплённые поля, поставщики товара, блок «Видео» (`renderVideos`/`addVideo`, `videoList` уходит в `formValues().videos`), ИИ-помощник (над ответом — строка расхода `aiUsageLine`), проверка перед отправкой, превью как на Prom, «🕘 История изменений» с «↩ Вернуть», «Открыть на Prom ↗», «🔍 Проверка выгрузки»; метки происхождения полей `renderOrigins` (`.origin-*`, `META.origin_labels`), `state.aiFields` → `_ai_fields`; без права «товары» — `applyReadOnly`/`lockForm` (`.card-readonly`, `#readonly-note`, автосохранение не запускается, клики по изменяющим кнопкам перехватываются); `#conflict-panel` — `renderConflict` (сравнение и выбор, `/api/products/{id}/prom-conflict`) |
| `/import` | `import.html`, `import.js`, `mapping.js` | Импорт Excel/CSV/XML: лист, строки, назначение колонок (по 200 строк, «Показать ещё»), проверка карточек |
| `/suppliers`, `/supplier?id=` | `suppliers.html`, `supplier.html`, `supplier.js`, `suppliers-common.js`, `mapping.js` | Поставщики: источник, колонки, расписание, свой курс, очистка названий, история обновлений (`runSummary`), блок «Удалённые вами товары» (вернуть выбранные/все + сразу обновить), «👁 Что изменится»; галочка «Автоконвейер» `#auto-pipeline`, итоги `pipeline_*` в `runSummary` |
| `/pricing` | `pricing.html` | «💱 Курс валют» (НБУ / свой, надбавка, автоотправка, на какие товары действует курс), правила наценки, калькулятор; перед пересчётом — окно «Что изменится»; `margin-watch.js` — «Сторож наценки» (`/api/pricing/low-margin`, `min-margin`) |
| `/changes` | `changes.html` | Журнал изменений: период, кто, поле, поиск, CSV, «↩ Вернуть»; на телефоне — карточки |
| `/orders` | `orders.html` | Заказы Prom: поиск (номер, телефон, имя, товар; `?q=` в адресе — ссылка из Telegram), даты, статусы, «Показать ещё» и общее число; только допустимые переходы (`actions` заказа), «Выполнен» с подтверждением, отмена с причиной; «не просмотрен» снимается по действию; автообновление только при открытой вкладке. `scanner.js` — `openScanner({title, hint, placeholder, onCode})`: камера + `makeDetector()` — свой `BarcodeDetector` браузера, иначе ZXing (`vendor/zxing-0.21.3.min.js`, `loadZxing` — только при открытии сканера; кадр ≤ 720 px → `MultiFormatReader`); не https — поле ввода; `onCode` может вернуть `{text, bad, warn}` для окна. `orders-scan.js`: «📷 Сканировать» `#scan-btn`, «📦 Режим упаковки» `#pack-mode` (localStorage), итог `#scan-result` (`showScanResult`), `packBlock` у заказа (`[data-pack]`, `[data-unpack]`); `?q=` и поиск по ТТН. `orders-np.js`: `prepayBlock` (⚠️ рисковый покупатель, «💬 Запросить предоплату в чат Prom», «Предоплата получена / снять пометку»), подтверждение ТТН с наложкой на 409 `risky` (`err.body` из `api()`); `buyerBadge` (⚠️ отказы/отмены покупателя — у заказа и `#ttn-buyer` в окне ТТН), «ждёт на отделении: N дн.» и `[data-remind]` «🔔 Напомнить в чат»; `orders-stats.js`: полоска выручки (`/api/orders/summary`) и вкладка «Статистика товаров» (`/api/orders/stats`, `stats.xlsx`, `#stats` в адресе); ТТН: галочка «номер в чат Prom» (`notify_buyer`, localStorage); ТТН: `#ttn-status` (не менять / Принят / Выполнен, localStorage). «Настройки» → «Новая Почта»: `#np-risky` (`auto_flag_risky_cod`) |
| `/login`, `/setup` | `login.html`, `setup.html` | Вход (логин + пароль, «Забыли пароль?» → код из файла) и создание первого администратора; без `common.js` |
| `/settings` | `settings.html`, `phone.js`, `users.js`, `settings-tabs.js` | Вкладки `#settings-tabs` (панели `main > [data-tab]`, якорь `#id` открывает вкладку панели); «👥 Пользователи» `#users` (`users.js`: роли `#roles-table` — права галочками, своя роль `#role-add`; пользователи: роль, имя, «Пароль», блокировка, удаление, добавление); Токен Prom, фото (R2 / туннель / адрес), ИИ, Telegram (+ недоставленные уведомления и «Повторить сейчас»), «📱 Доступ с телефона» (Wi‑Fi, Cloudflare, адреса с QR; с телефона — только надпись), расписание, обновления, копии, автозапуск; Telegram: дни недели сводки `#tg-summary-days`; «🔄 Синхронизация с кабинетом Prom» `#prom-sync` (`/api/prom/autosync`) |
| `/manifest.webmanifest`, `/sw.js` | `manifest.webmanifest`, `sw.js`, `app-install.js`, `icon-192/512.png` | Приложение на телефоне: манифест и service worker с корня сайта, открыты без входа. В `sw.js` — только офлайн-страница для переходов и push; ничего не кешируется. Регистрация и `beforeinstallprompt` — в `common.js`; блок «Приложение на телефоне» в «Настройках» — `app-install.js`. Каждая страница несёт `<link rel="manifest">` и мета-теги iOS (проверяет `tests/test_pwa.py`) |
| `/login` | `login.html` | Вход с телефона по паролю (своя маленькая страница без `common.js`, не в `PAGES`/`NAV`); `api()` при 401 `{login}` сам ведёт сюда, в меню телефона — «Выйти» (`showLogout`) |
| `/support` | `support.html` | Обращения в поддержку со скриншотами |
| `/chat` | `chat.html`, `chat.js` | Чат с покупателями: диалоги (поиск, непрочитанные), переписка с карточкой товара и заказом, вложения, шаблоны ответа, «✨ Черновик ИИ», «Отправить»; на телефоне — список ↔ диалог; `#quick` — готовые ответы из данных (`quick` в ответе messages), «✨ Предложить ответ» |
| `/reports` | `reports.html`, `reports.js` | Отчёты: период, вкладки «Продажи / Товары / Поставщики и группы / Магазин», карточки итогов, график SVG без библиотек, таблицы с сортировкой (`table`), «≈», «⬇ Excel» (`/api/reports/{tab}.xlsx`) |
| `/categories` | `categories.html`, `categories.js` | Категории Prom по группам: что стоит на Prom, «разнёс по N категориям», «не та категория» (ссылка на фильтр списка), выбор категории (поиск prom.ua, ссылка, уже известные) с окном «Что изменится» |
| `/diagnose` | `diagnose.html` | Пошаговая проверка выгрузки одного товара |

## Оформление (2.29.0)

- Тема: `i18n.js` (в `<head>`, до отрисовки) — `setupTheme()` ставит `html[data-theme]` из `localStorage
  promloader-theme` (auto/light/dark, «auto» следит за системой), `html[data-brand]` + `--brand` из
  `promloader-brand`, `html[data-density]` из `promloader-density`; `window.THEME.set/get/brand/density`. Ошибка
  внутри не мешает переводу (try). Цвета — только переменные `:root` в `app.css` (`--panel`, `--panel-2`, `--chip-bg`,
  `--line-strong`, `--info*`, `--warn-*`, `--err-*`, `--ok-line`, `--input-bg`, `--overlay`); тёмные значения —
  `:root[data-theme="dark"]`. Превью Prom (`.pp`, `.tile`) — всегда светлое.
- Значки: `vendor/lucide-1.49.0.svg` (спрайт `<symbol>`), `ICONS` (эмодзи → имя) в `common.js`; `iconize` заменяет
  эмодзи на `<svg class="i"><use>` в тексте страницы — **после** перевода (`startIcons` по `DOMContentLoaded`, его
  наблюдатель создаётся после переводчика), кроме `translate="no"`, полей, `option`, превью Prom. Тексты и словарь
  с эмодзи не меняются. `iconSvg(name)` — значок для кода (нижняя панель).
- `/today` (`today.html`, `today.js`) — первый пункт `NAV`, логотип ведёт сюда, вход без `next` — сюда.
- Телефон: `renderBottomNav` (`BOTTOM_NAV`, «Ещё» → `body.nav-open` раскрывает меню сверху), счётчики заказов и чата —
  в `loadMeta`.
- Права: `[data-perm]` у кнопок/ссылок без права — видны, бледные, с замком (CSS, `--lock-icon`), нажатие перехватывает
  обработчик в фазе захвата → toast «Нет права…»; блоки (не кнопки) по-прежнему скрываются. `showRoleBanner` —
  строка «Ваша роль … — нельзя: …» под шапкой.
- «Настройки» → `#look` (вкладка «Оформление»): тема, `.look-logo[data-slot]`, компактный список, цвет из логотипа.
  `login.html` берёт `/api/brand`: `.login-logo` над формой, `body.login-hero` с `--login-bg` — большой логотип на фоне.

## Упрощение (2.28.0)

- Меню: `NAV` — четвёртый элемент `true` = пункт в «Ещё ▾» (`details.nav-more`); на ≤ 640 px `renderNav` рисует всё
  в ряд. Высота липкой шапки — `--topbar-h` (`measureTopbar`). Список «Ещё ▾» при открытии ставится `position: fixed`
  под кнопкой (`placeNavMore`): на ≤ 1440 px `.topbar nav` с `overflow-x: auto` обрезал бы `absolute`-список;
  прокрутка и смена размера окна его закрывают.
- «Товары»: `#pipeline` (`renderPipeline` по `list.counts` и `list.ready` = `/api/products/readiness`; этапы
  черновиков `DRAFT_STAGES` → `status=draft&readiness=…`), `#stage-action` (`renderStageAction`: ИИ, по фото,
  «В Готов», `#send-stage` → `/api/sync` с фильтром). `#bulk .bulk-actions` видны только при `.has-selection`
  (липкая панель). `#jobs-panel` и `#quick-drop` скрыты, пока не нужны; `#filters-toggle` — фильтры на телефоне;
  плашка «📉 Заканчиваются» и порог `#low-threshold` — в `#list-alerts`.
- Карточка: редкие действия — `details.actions-menu.menu-always` (свёрнуто и на компьютере), «Видео» —
  `details#video-panel` в конце формы. Заказы: `ttnCreateButton` + `packBlock` + смена статуса — одна строка
  `.order-actions`; `prepayBlock(o) || buyerBadge(o.buyer)` — одна плашка риска.
- Поставщик: `details#purchase-panel` → `loadPurchase` (первые `PURCHASE_SHOWN` = 100 строк, остальное — в Excel).

## Плавность и удобство (2.27.0) — общие помощники `common.js`

- `skeletonRows(cols)` / `skeletonCards(n)` — серая заготовка, пока идёт первая загрузка (товары, заказы, отчёты);
  при ошибке запроса заготовка убирается. `.is-stale` — старые цифры отчёта полупрозрачные, пока считаются новые.
- `patchChildren(container, items, keyOf, htmlOf)` — фоновое обновление меняет только строки с новой разметкой
  (товары, очередь отправки, задания ИИ, заказы, диалоги и сообщения чата). Обработчики кликов — делегированные, на
  контейнере (не на строках). Чат прокручивает вниз, только если и так были внизу.
- `busy(btn)` + CSS `.is-busy::after` — крутящийся значок на кнопке во время запроса.
- `undoToast(text, token, onUndone)` — уведомление с «↩ Отменить» (8 с) → `POST /api/products/undo/{token}`.
- Горячие клавиши: `/` — поиск (`#search` или `#q`), `?` — окно `#keys-help` (`showKeysHelp`), страница добавляет свои
  в `PAGE_KEYS`; в полях ввода не перехватываются (`typingIn`). Карточка: Ctrl+S, ←/→.
- Список помнит место: `list.js` на `pagehide` пишет в `sessionStorage["promloader-list-place"]` адрес, прокрутку,
  число строк, отметки и порядок id; при возврате на тот же адрес восстанавливает. Из того же порядка карточка рисует
  «← Пред. / N из M / След. →» (`#card-nav`, перед переходом — `flush()`).
- Телефон: `#bulk.has-selection` — липкая панель массовых действий; `.help-fab-text` скрыт (только «🆘»);
  `@media (pointer: coarse)` — кнопки и галочки под палец.
- Логотип магазина: `renderBrand()` (из `loadMeta`, по `META.shop_logo`) — картинка `/shop-logo.png?v=` в `.topbar .logo`
  и в `link[rel=icon]`; на `login.html` — своя загрузка картинки (без `common.js`).

Страницы регистрируются в `main.PAGES`; новая страница = HTML в `static/` (с `<nav></nav>` и `common.js`) + строка в `PAGES` + пункт в `NAV` (`common.js`). Тест `tests/test_stage6.py` проверяет, что всё сходится.

## Правила страниц

- Ответ сервера не перезаписывает то, что пользователь набирает: карточка — `fillUntouched` и `applyServer` пропускают
  активное поле, поля из `state.pending` и поле с ошибкой (`.invalid`); поставщик — `fillForm` только при открытии и
  после сохранения, опрос обновляет `showState`; настройки — каждый раздел заполняет свои поля (`fillMain`, `fillAdv`, `fillAi`); новый ключ ИИ проверяется сразу после «Сохранить» (`runAiCheck`), предупреждение о формате — `#ai-key-warning`.
- Перед «Что изменится» и «Обновить» поставщика несохранённая форма сохраняется (`saveIfDirty`).
- Список: номер запроса `list.seq` (устаревший ответ отбрасывается), фоновое обновление — один таймер `refreshList`.
- Действие с запросом — через `busy(кнопка, …)`.

## Язык интерфейса (2.6.0)

- Исходный язык — русский, прямо в коде. `i18n.js` при `localStorage promloader-lang = "uk"` переводит готовую
  страницу словарём `window.I18N_UK` (`i18n-uk.js`, «русская строка → украинская», `{}` — подстановка): текстовые
  узлы, `title`/`placeholder`/`aria-label`/`alt`, кнопки `input`, `confirm`/`prompt`/`alert`, заголовок вкладки; всё
  дорисованное позже — через `MutationObserver`. Страница скрыта до перевода (не дольше 1,5 с). `I18N.set(lang)` —
  смена языка с перезагрузкой (переключатель «RU | UK» — `renderLangSwitch` в `common.js`), `uiLocale()` — локаль для
  дат и чисел.
- Поиск в словаре: точная строка (пробелы схлопываются) → шаблоны (длинные раньше; подстановка сама переводится,
  перечисление через «, » — по частям; короткий шаблон вроде «{} в {}» срабатывает, только если подстановки — числа
  или известные фразы) → склейки страницы: латинское имя перед фразой («Prom: …»), разделители «·».
- Данные пользователя — внутри `translate="no"` (названия товаров, описания, группы, поставщики, заказы, значения
  журнала, превью витрины): их не трогают, даже если совпали со строкой словаря.
- Фраза, которую страница собирает из кусков, переводится по кускам: `I18N.tr("…")` (в русском режиме возвращает
  строку как есть). Строка в шаблоне — целой фразой с `${…}`, тогда её найдёт и сборщик строк, и словарь.
- `I18N.missing` — что на странице не нашлось в словаре (для проверки в браузере).
