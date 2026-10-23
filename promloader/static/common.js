// Общие помощники для всех страниц.

if (!window.uiLocale) window.uiLocale = () => "ru-RU";  // язык интерфейса задаёт i18n.js

const META = { presence: {}, statuses: {}, targets: {}, max_images: 10, shop_name: "" };

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    delete opts.json;
  }
  let response;
  try {
    response = await fetch(path, opts);
  } catch (e) {
    showOffline(true);
    const err = new Error("Нет связи с программой");
    err.network = true;
    throw err;
  }
  showOffline(false);
  let body = null;
  const text = await response.text();
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!response.ok) {
    // вход истёк или ещё не выполнен — на страницу входа (или создания администратора), потом обратно сюда.
    // Один раз, и не со страницы входа: десяток фоновых запросов не должен устраивать цепочку переходов
    if (response.status === 401 && body && body.login) goToLogin(body.setup);
    // права отняли, пока страница была открыта: перечитать их и убрать кнопки, которые больше не работают
    if (response.status === 403 && body && body.forbidden) refreshPerms();
    let detail = body && body.detail !== undefined ? body.detail : body;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    const err = new Error(typeof detail === "string" && detail ? detail : `Ошибка ${response.status}`);
    err.status = response.status;
    err.field = body && body.field;
    err.body = body && typeof body === "object" ? body : {};
    throw err;
  }
  return body;
}

let leaving = false;
function goToLogin(setup) {
  if (leaving || ["/login", "/setup"].includes(location.pathname)) return;
  leaving = true;
  location.href = `${setup ? "/setup" : "/login"}?next=${encodeURIComponent(location.pathname + location.search + location.hash)}`;
}

let permsChecked = 0;
async function refreshPerms() {
  if (Date.now() - permsChecked < 5000) return;  // пачка отказов подряд — одна проверка
  permsChecked = Date.now();
  let me;
  try { me = await api("/api/me"); } catch { return; }
  const before = (META.me && META.me.perms || []).join(",");
  META.me = me;
  if (before === (me.perms || []).join(",")) return;
  applyNavPerms();
  document.dispatchEvent(new Event("perms-changed"));  // страница сама закроет то, что стало нельзя
  if (document.getElementById("perms-changed")) return;
  const bar = document.createElement("div");
  bar.id = "perms-changed";
  bar.className = "update-banner";
  bar.innerHTML = `<span>🔐 Администратор изменил ваши права — обновите страницу, чтобы увидеть, что теперь доступно.</span>
    <button class="btn small" type="button">Обновить страницу</button>`;
  bar.querySelector("button").onclick = () => location.reload();
  document.querySelector(".topbar")?.insertAdjacentElement("afterend", bar);
}

async function loadMeta() {
  Object.assign(META, await api("/api/meta"));
  renderAiChip();
  showUpdateBanner();
  showTokenBanner();
  showFailedSuppliers();
  showPaused();
  showMissedSchedules();
  const link = document.querySelector('.topbar nav a[href="/orders"]');
  if (link) link.innerHTML = `Заказы${META.orders_unseen ? ` <span class="nav-badge">${META.orders_unseen}</span>` : ""}`;
  const chatLink = document.querySelector('.topbar nav a[href="/chat"]');
  if (chatLink) chatLink.innerHTML = `Чат${META.chat_unseen ? ` <span class="nav-badge">${META.chat_unseen}</span>` : ""}`;
  const reviewsLink = document.querySelector('.topbar nav a[href="/reviews"]');
  if (reviewsLink) reviewsLink.innerHTML = `Отзывы${META.reviews_unseen ? ` <span class="nav-badge">${META.reviews_unseen}</span>` : ""}`;
  for (const [href, n] of [["/orders", META.orders_unseen], ["/chat", META.chat_unseen]]) {  // счётчики и в нижней панели
    const icon = document.querySelector(`.bottom-nav a[href="${href}"] .bn-icon`);
    if (!icon) continue;
    icon.querySelector(".nav-badge")?.remove();
    if (n) icon.insertAdjacentHTML("beforeend", `<span class="nav-badge">${n}</span>`);
  }
  renderUserMenu();
  renderBrand();
  measureTopbar();
  return META;
}

// Высота «липкой» шапки — чтобы закреплённые панели (массовые действия) вставали под неё, а не за неё
function measureTopbar() {
  const bar = document.querySelector(".topbar");
  const h = bar && getComputedStyle(bar).position === "sticky" ? bar.offsetHeight : 0;
  document.documentElement.style.setProperty("--topbar-h", `${h}px`);
}
window.addEventListener("resize", measureTopbar);
window.addEventListener("load", measureTopbar);

// Логотип магазина (Настройки → Prom): в шапке вместо «Prom Loader» и значком вкладки браузера
let brandDefault = null;
function renderBrand(version = META.shop_logo) {
  window.THEME?.brand(META.brand_accent || "");  // цвет из логотипа (или стандартный) — и запомнить для <head>
  const logo = $(".topbar .logo");
  if (logo && brandDefault === null) brandDefault = logo.innerHTML;
  const icon = document.querySelector('link[rel="icon"]');
  if (icon && !icon.dataset.default) icon.dataset.default = icon.getAttribute("href");
  if (!version) {
    if (logo) { logo.innerHTML = brandDefault; logo.classList.remove("with-logo"); }
    if (icon) icon.href = icon.dataset.default;
    return;
  }
  const src = `/shop-logo.png?v=${encodeURIComponent(version)}`;
  if (logo) {
    logo.classList.add("with-logo");
    logo.innerHTML = `<img src="${src}" alt="">${META.shop_name ? `<span class="logo-name" translate="no">${esc(META.shop_name)}</span>` : ""}`;
  }
  if (icon) icon.href = src;
}

// Пропущенная выгрузка (компьютер был выключен): спрашиваем, что делать.
function showMissedSchedules() {
  $$(".missed-banner").forEach((el) => el.remove());
  for (const m of META.missed_schedules || []) {
    const bar = document.createElement("div");
    bar.className = "missed-banner";
    bar.innerHTML = `⏰ Выгрузка на Prom <b>${esc(formatDate(m.slot))}</b> (${esc(m.label)}) не выполнена —
      программа в это время не работала. Что сделать?
      <span class="actions">
        <button class="btn small primary" data-d="run">Выгрузить сейчас</button>
        <button class="btn small" data-d="snooze">Отложить на час</button>
        <button class="btn small" data-d="skip">Пропустить</button>
      </span>`;
    bar.querySelectorAll("[data-d]").forEach((b) => b.addEventListener("click", async () => {
      try {
        await api(`/api/schedules/${m.id}/resolve`, { method: "POST", json: { decision: b.dataset.d } });
        toast({ run: "Товары поставлены в отправку", snooze: "Отложено на час", skip: "Эта выгрузка пропущена" }[b.dataset.d], "ok");
        bar.remove();
      } catch (err) { toast(err.message, "error"); }
    }));
    document.querySelector(".topbar")?.insertAdjacentElement("afterend", bar);
  }
}

// Сбой обновления поставщика — видно на всех страницах, а не только на странице поставщика.
function showFailedSuppliers() {
  const failed = META.failed_suppliers || [];
  const link = document.querySelector('.topbar nav a[href="/suppliers"]');
  if (link) link.innerHTML = `Поставщики${failed.length ? ` <span class="nav-badge" title="Не обновились: ${esc(failed.map((f) => f.name).join(", "))}">!</span>` : ""}`;
  $$(".supplier-failed-banner").forEach((el) => el.remove());
  const here = Number(new URLSearchParams(location.search).get("id"));
  const show = failed.filter((f) => !(location.pathname === "/supplier" && f.id === here));
  if (!show.length) return;
  const bar = document.createElement("div");
  bar.className = "update-banner supplier-failed-banner";
  const first = show[0];
  bar.innerHTML = `⚠️ Поставщик <b translate="no">«${esc(first.name)}»</b> не обновился: ${esc((first.message || "").slice(0, 160))}
    <a href="/supplier?id=${first.id}">Открыть</a>${show.length > 1 ? ` · и ещё ${show.length - 1} — <a href="/suppliers">все поставщики</a>` : ""}`;
  document.querySelector(".topbar")?.insertAdjacentElement("afterend", bar);
}

// Пауза после переезда: этот компьютер не забирает заказы и не отправляет — работает новый.
function showPaused() {
  $$(".paused-banner").forEach((el) => el.remove());
  if (!META.paused) return;
  const bar = document.createElement("div");
  bar.className = "update-banner paused-banner";
  bar.innerHTML = `⏸ Программа на паузе: данные перенесены на другое устройство, здесь заказы, чат и отправка на Prom
    не работают. <button class="btn small" id="unpause">Включить обратно</button>`;
  document.querySelector(".topbar")?.insertAdjacentElement("afterend", bar);
  bar.querySelector("#unpause").onclick = async () => {
    if (!confirm("Включить фоновую работу на этом компьютере? Если программа работает и на новом устройстве, заказы и уведомления будут приходить дважды.")) return;
    try { await api("/api/pause", { method: "POST", json: { paused: false } }); await loadMeta(); }
    catch (err) { toast(err.message, "error"); }
  };
}

// «Что изменится»: окно с итогом пробного прогона. Возвращает true, если нажали «Применить».
function showPreview(title, p, { applyLabel = "Применить", note = "" } = {}) {
  return new Promise((resolve) => {
    const money = (v, cur) => formatPrice(v, cur || "UAH");
    const pct = (o, n) => {
      const a = parseFloat(o), b = parseFloat(n);
      if (!(a > 0) || isNaN(b)) return "";
      const d = Math.round((b - a) / a * 1000) / 10;
      return ` <span class="${d >= 0 ? "price-up" : "price-down"}">${d >= 0 ? "+" : ""}${d}%</span>`;
    };
    const table = (rows, head, cell) => rows.length
      ? `<div class="table-scroll preview-table"><table class="list"><thead><tr>${head}</tr></thead><tbody>${rows.map(cell).join("")}</tbody></table></div>` : "";
    const nothing = !p.created && !p.changed;
    const box = document.createElement("div");
    box.className = "modal";
    box.innerHTML = `<div class="modal-box preview-box">
      <h2>${esc(title)}</h2>
      ${nothing ? `<p>Ничего не изменится — цены и товары уже актуальны.</p>` : `
      <div class="preview-sum">
        ${p.created ? `<div><b>${p.created}</b> новых товаров</div>` : ""}
        ${p.price_changed ? `<div><b>${p.price_changed}</b> цен изменится</div>` : ""}
        ${p.gone ? `<div><b>${p.gone}</b> станут «нет в наличии»</div>` : ""}
        ${p.changed - p.price_changed - p.gone > 0 ? `<div><b>${p.changed}</b> товаров изменится всего</div>` : ""}
        ${p.to_prom ? `<div>🚀 <b>${p.to_prom}</b> сразу уйдут на Prom</div>` : ""}
      </div>
      ${Object.keys(p.fields || {}).length ? `<p class="small muted">Что меняется: ${Object.entries(p.fields).map(([k, n]) => `${esc(I18N.tr(k))} — ${n}`).join(", ")}</p>` : ""}
      ${p.samples.prices.length ? `<h3>Цены${p.price_changed > p.samples.prices.length ? ` (первые ${p.samples.prices.length})` : ""}</h3>` : ""}
      ${table(p.samples.prices, "<th>Товар</th><th>Было</th><th>Станет</th>", (r) =>
        `<tr><td translate="no">${esc(r.name)}<div class="small muted">${esc(r.code)}</div></td><td class="nowrap">${money(r.old, r.currency)}</td>
         <td class="nowrap"><b>${money(r.new, r.currency)}</b>${pct(r.old, r.new)}</td></tr>`)}
      ${p.samples.created.length ? `<h3>Новые товары${p.created > p.samples.created.length ? ` (первые ${p.samples.created.length})` : ""}</h3>` : ""}
      ${table(p.samples.created, "<th>Товар</th><th>Артикул</th>", (r) => `<tr><td translate="no">${esc(r.name)}</td><td class="small" translate="no">${esc(r.code)}</td></tr>`)}
      ${p.samples.gone.length ? `<h3>Станут «нет в наличии»</h3>` : ""}
      ${table(p.samples.gone, "<th>Товар</th><th>Артикул</th>", (r) => `<tr><td translate="no">${esc(r.name)}</td><td class="small" translate="no">${esc(r.code)}</td></tr>`)}`}
      ${note ? `<p class="small muted">${note}</p>` : ""}
      <div class="toolbar" style="margin:12px 0 0;justify-content:flex-end">
        <button class="btn" data-a="cancel">Отмена</button>
        <button class="btn primary" data-a="apply">${esc(nothing ? "Всё равно применить" : applyLabel)}</button>
      </div></div>`;
    const done = (ok) => { box.remove(); document.removeEventListener("keydown", onKey); resolve(ok); };
    const onKey = (e) => { if (e.key === "Escape") done(false); };
    box.addEventListener("click", (e) => {
      const a = e.target.closest("[data-a]");
      if (a) done(a.dataset.a === "apply");
      else if (e.target === box) done(false);
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(box);
  });
}

// Нет токена Prom — отправка на Prom невозможна: говорим об этом на всех страницах, кроме «Настроек».
function showTokenBanner() {
  if (META.prom_token_set || $("#token-banner") || location.pathname === "/settings") return;
  const bar = document.createElement("div");
  bar.id = "token-banner";
  bar.className = "update-banner";
  bar.innerHTML = `Программа ещё не подключена к Prom — товары нельзя отправить.
    <a href="/settings">Вставьте API-токен в «Настройках»</a>`;
  document.querySelector(".topbar")?.insertAdjacentElement("afterend", bar);
}

function showUpdateBanner() {
  if (!META.update_available || $("#update-banner") || location.pathname === "/settings") return;
  const bar = document.createElement("div");
  bar.id = "update-banner";
  bar.className = "update-banner";
  bar.innerHTML = `Доступна новая версия программы <b>${esc(META.update_available)}</b>.
    <a href="/settings#updates">Посмотреть, что нового, и обновить</a>`;
  document.querySelector(".topbar")?.insertAdjacentElement("afterend", bar);
}

// Программа выключена или перезапускается: полоска сверху вместо молчащих страниц. Убирается при первом удачном
// запросе — фоновые обновления страниц (очередь, заказы) сами «увидят», что связь вернулась.
function showOffline(on) {
  const bar = document.getElementById("offline-banner");
  if (!on) { if (bar) bar.remove(); return; }
  if (bar) return;
  const el = document.createElement("div");
  el.id = "offline-banner";
  el.className = "offline-banner";
  el.innerHTML = `⚠️ Нет связи с программой — она выключена или перезапускается. Если не пройдёт само за минуту,
    запустите её ярлыком «Prom Loader». <button class="btn small" type="button">Проверить</button>`;
  el.querySelector("button").onclick = () => fetch("/api/meta", { cache: "no-store" })
    .then((r) => { if (r.ok) showOffline(false); }).catch(() => toast("Программа всё ещё не отвечает", "error"));
  document.body.prepend(el);
}

// Страховка: ошибка, которую страница забыла обработать, всё равно видна (а не пропадает молча).
// Нет связи — уже сказано полоской; одинаковые сообщения подряд не повторяем.
let lastUnhandled = { text: "", at: 0 };
window.addEventListener("unhandledrejection", (e) => {
  const err = e.reason;
  if (!err || err.network) return;
  const text = err.message || String(err);
  if (text === lastUnhandled.text && Date.now() - lastUnhandled.at < 5000) return;
  lastUnhandled = { text, at: Date.now() };
  toast(text, "error");
});

// Ждём, пока программа перезапустится (после обновления/восстановления), и перезагружаем страницу.
function waitForRestart(message, restarting) {
  const overlay = document.createElement("div");
  overlay.className = "restart-overlay";
  overlay.innerHTML = `<div><div class="ai-loading">${esc(message)}</div>
    <p class="small muted">${restarting ? "Программа перезапускается — страница обновится сама."
      : "Закройте чёрное окно программы и запустите её снова (ярлык «Prom Loader»)."}</p></div>`;
  document.body.appendChild(overlay);
  if (!restarting) return;
  let sawDown = false;
  const tick = async () => {
    try {
      await fetch("/api/meta", { cache: "no-store" }).then((r) => { if (!r.ok) throw new Error(); });
      if (sawDown) { location.reload(); return; }
    } catch { sawDown = true; }
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 1500);
}

// Минимальный markdown для заметок «Что нового»: заголовки ## и пункты «- ».
function markdownLite(text) {
  return text.split("\n").map((line) => {
    if (line.startsWith("## ")) return `<h4>Версия ${esc(line.slice(3))}</h4>`;
    if (line.startsWith("- ")) return `<li>${esc(line.slice(2))}</li>`;
    return line.trim() ? `<p>${esc(line)}</p>` : "";
  }).join("");
}

function $(sel, root = document) { return root.querySelector(sel); }
function $$(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(message, kind = "") {
  let box = $("#toasts");
  if (!box) {
    box = document.createElement("div");
    box.id = "toasts";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
  return el;
}

// Действие уже выполнено, а несколько секунд его можно отменить — вместо вопроса «Вы уверены?» перед ним.
// token — номер отмены из ответа сервера (нет номера — обычное уведомление); onUndone — перерисовать страницу.
function undoToast(message, token, onUndone = () => {}) {
  const el = toast(message, "ok");
  if (!token) return el;
  el.classList.add("with-undo");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "toast-undo";
  btn.textContent = I18N.tr("↩ Отменить");
  el.appendChild(btn);
  const timer = setTimeout(() => el.remove(), 8000);  // дольше обычного: надо успеть понять и нажать
  btn.addEventListener("click", () => busy(btn, async () => {
    try {
      const r = await api(`/api/products/undo/${encodeURIComponent(token)}`, { method: "POST" });
      clearTimeout(timer);
      el.remove();
      toast(I18N.tr(`Отменено: ${r.restored}`), "ok");
      onUndone();
    } catch (err) { toast(err.message, "error"); }
  }));
  return el;
}

// Заготовка на месте таблицы, пока идёт первая загрузка: страница не «прыгает», когда приходят данные
function skeletonRows(cols, rows = 6) {
  const cell = (n) => `<td><span class="sk" style="width:${40 + ((n * 37) % 50)}%"></span></td>`;
  return Array.from({ length: rows }, (_, r) =>
    `<tr class="skeleton" aria-hidden="true">${Array.from({ length: cols }, (_, c) => cell(r + c)).join("")}</tr>`).join("");
}

// То же для списков карточками (заказы, чат, поставщики)
function skeletonCards(count = 3) {
  return Array.from({ length: count }, () => `<div class="panel skeleton" aria-hidden="true">
    <span class="sk" style="width:28%"></span><br><span class="sk" style="width:62%;margin-top:12px"></span><br>
    <span class="sk" style="width:45%;margin-top:10px"></span></div>`).join("");
}

// Перерисовать список, не трогая строки, которые не изменились: при фоновом обновлении не мигают картинки, не
// сбрасываются наведение, фокус и выделение текста. keyOf — номер строки, htmlOf — её разметка (один элемент).
function patchChildren(container, items, keyOf, htmlOf) {
  const old = new Map();
  for (const el of container.children) if (el.dataset.key) old.set(el.dataset.key, el);
  const tpl = document.createElement("template");  // <template> правильно разбирает и <tr>
  const keep = new Set();
  let prev = null;
  for (const item of items) {
    const key = String(keyOf(item));
    const html = htmlOf(item).trim();
    let el = old.get(key);
    if (!el || el._html !== html) {
      tpl.innerHTML = html;
      const fresh = tpl.content.firstElementChild;
      fresh.dataset.key = key;
      fresh._html = html;
      if (el) el.replaceWith(fresh);
      el = fresh;
    }
    keep.add(el);
    const want = prev ? prev.nextElementSibling : container.firstElementChild;
    if (want !== el) container.insertBefore(el, want);
    prev = el;
  }
  for (const el of [...container.children]) if (!keep.has(el)) el.remove();
}

const CURRENCY_SIGN = { UAH: "₴", USD: "$", EUR: "€" };

function formatPrice(value, currency = "UAH") {
  if (value === null || value === undefined || value === "") return "";
  const n = Number(value);
  if (Number.isNaN(n)) return "";
  const text = n.toLocaleString("uk-UA", { minimumFractionDigits: n % 1 ? 2 : 0, maximumFractionDigits: 2 });
  return `${text} ${CURRENCY_SIGN[currency] || currency || "₴"}`;
}

function statusBadge(status) {
  return `<span class="badge ${esc(status)}">${esc(META.statuses[status] || status)}</span>`;
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  // год — если не текущий: иначе «05.10» прошлого года не отличить от этого
  const year = d.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined;
  return d.toLocaleString(uiLocale(), { day: "2-digit", month: "2-digit", year, hour: "2-digit", minute: "2-digit" });
}

// Кнопка «занята», пока идёт её запрос: двойной клик не отправит товары, не удалит и не запустит ИИ дважды.
// disabled не трогаем — его выставляет сама страница по состоянию товара.
async function busy(btn, fn) {
  if (!btn || btn.dataset.busy) return;
  btn.dataset.busy = "1";
  btn.classList.add("is-busy");
  try {
    return await fn();
  } finally {
    delete btn.dataset.busy;
    btn.classList.remove("is-busy");
  }
}

// Горячие клавиши на всех страницах: «/» — поиск, «?» — список клавиш. Страница добавляет свои в PAGE_KEYS
// ([клавиша, что делает]) — они попадают в тот же список. В полях ввода клавиши не перехватываются.
const PAGE_KEYS = [];
function typingIn(el) {
  return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
}
function showKeysHelp() {
  let box = $("#keys-help");
  if (!box) {
    box = document.createElement("div");
    box.id = "keys-help";
    box.className = "modal hidden";
    document.body.appendChild(box);
  }
  const rows = [["/", "Поиск на странице"], ...PAGE_KEYS, ["Esc", "Закрыть окно"], ["?", "Эта подсказка"]];
  box.innerHTML = `<div class="modal-box" style="max-width:420px"><h2>${esc(I18N.tr("Горячие клавиши"))}</h2>
    <table class="keys">${rows.map(([k, t]) => `<tr><td><kbd>${esc(k)}</kbd></td><td>${esc(I18N.tr(t))}</td></tr>`).join("")}</table>
    <div class="toolbar" style="margin:12px 0 0"><span class="spacer"></span><button class="btn" type="button" data-close>${esc(I18N.tr("Закрыть"))}</button></div></div>`;
  box.querySelector("[data-close]").onclick = () => box.classList.add("hidden");
  box.classList.remove("hidden");
}
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || typingIn(e.target)) return;
  if (e.key === "/") {
    const search = $("#search") || $("#q");
    if (search && search.offsetParent !== null) { e.preventDefault(); search.focus(); search.select(); }
  } else if (e.key === "?") {
    e.preventDefault();
    showKeysHelp();
  }
});

// Окна (.modal с id) закрываются клавишей Esc и кликом по затемнённому фону — как везде
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const open = [...document.querySelectorAll(".modal[id]:not(.hidden)")].pop();
  if (open) open.classList.add("hidden");
});
document.addEventListener("click", (e) => {
  if (e.target.matches && e.target.matches(".modal[id]")) e.target.classList.add("hidden");
});

// Описание: обычный текст -> абзацы; HTML -> только безопасные теги.
const ALLOWED_TAGS = new Set(["P", "BR", "B", "STRONG", "I", "EM", "U", "UL", "OL", "LI", "H2", "H3", "H4", "TABLE", "TBODY", "THEAD", "TR", "TD", "TH", "SPAN", "DIV", "A", "IMG"]);
const ALLOWED_ATTRS = { A: ["href"], IMG: ["src", "alt"] };

// Эти теги удаляются вместе с содержимым; остальные неразрешённые — «разворачиваются» (остаётся текст).
const DROP_TAGS = new Set(["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "XML", "META", "TITLE", "HEAD", "LINK",
  "NOSCRIPT", "TEMPLATE", "SVG", "MATH", "FORM", "INPUT", "BUTTON", "SELECT", "TEXTAREA"]);

// Ссылки и картинки в описании — только обычные адреса. Проверка «не javascript:» обходилась: браузер выкидывает
// из адреса табы и переводы строк, и «java&#9;script:» всё равно выполнялся. Поэтому — белый список схем.
const SAFE_SCHEMES = new Set(["http:", "https:", "mailto:", "tel:"]);

function safeUrl(value) {
  try {
    return SAFE_SCHEMES.has(new URL(value, location.href).protocol);
  } catch (e) {
    return false;
  }
}

function sanitizeHtml(html) {
  const doc = new DOMParser().parseFromString(`<div>${html}</div>`, "text/html");
  const clean = (node) => {
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.COMMENT_NODE) { child.remove(); continue; }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;
      const tag = child.tagName.toUpperCase();
      if (DROP_TAGS.has(tag)) { child.remove(); continue; }
      clean(child);  // сначала вглубь: иначе вложенное в развёрнутый тег не проверится
      if (!ALLOWED_TAGS.has(tag)) { child.replaceWith(...child.childNodes); continue; }
      for (const attr of Array.from(child.attributes)) {
        const isUrl = attr.name === "href" || attr.name === "src";
        const ok = (ALLOWED_ATTRS[tag] || []).includes(attr.name) && (!isUrl || safeUrl(attr.value));
        if (!ok) child.removeAttribute(attr.name);
      }
    }
  };
  const root = doc.body.firstChild;
  clean(root);
  return root.innerHTML;
}

function descriptionHtml(text) {
  text = (text || "").trim();
  if (!text) return "";
  if (/<\s*\/?\s*[a-z][^>]*>/i.test(text)) return sanitizeHtml(text);
  return text.split(/\n\s*\n/).map((p) => `<p>${esc(p.trim()).replace(/\n/g, "<br>")}</p>`).join("");
}

// Меню — одно на все страницы (раньше его копия была в каждом HTML). Новая страница: HTML в static/, строка в
// main.PAGES и пункт здесь. Третий элемент — адреса, при которых пункт тоже подсвечен (карточка товара — «Товары»).
// [адрес, название, другие адреса раздела, true — в «Ещё ▾»]. Каждый день нужны первые пять и «Настройки»,
// остальное — реже: на компьютере оно в выпадающем «Ещё», на телефоне меню и так листается вбок — там всё в ряд.
const NAV = [
  ["/today", "Сегодня"],
  ["/", "Товары", ["/product"]],
  ["/orders", "Заказы"],
  ["/chat", "Чат"],
  ["/reviews", "Отзывы"],
  ["/suppliers", "Поставщики", ["/supplier"]],
  ["/reports", "Отчёты"],
  ["/pricing", "Наценка", [], true],
  ["/categories", "Категории", [], true],
  ["/changes", "Журнал", [], true],
  ["/import", "Импорт файла", [], true],
  ["/diagnose", "Проверка выгрузки", [], true],
  ["/support", "Поддержка", [], true],
  ["/settings", "Настройки"],
];

function renderNav() {
  const nav = document.querySelector(".topbar nav");
  if (!nav || nav.children.length) return;
  const path = location.pathname;
  const link = ([href, label, also = []]) =>
    `<a href="${href}"${href === path || also.includes(path) ? ' class="active"' : ""}>${esc(label)}</a>`;
  const flat = window.matchMedia("(max-width: 640px)").matches;
  const main = NAV.filter((n) => flat || !n[3]);
  const more = flat ? [] : NAV.filter((n) => n[3]);
  const inMore = more.find(([href, , also = []]) => href === path || also.includes(path));
  const logo = document.querySelector(".topbar .logo");
  if (logo) logo.href = "/today";  // логотип ведёт на главную «Сегодня»
  nav.innerHTML = main.filter((n) => n[0] !== "/settings").map(link).join("")
    + (more.length ? `<details class="nav-more"><summary class="${inMore ? "active" : ""}"><span>${esc(inMore ? inMore[1] : "Ещё")}</span> ▾</summary>
        <div class="nav-more-box">${more.map(link).join("")}</div></details>` : "")
    + main.filter((n) => n[0] === "/settings").map(link).join("");
}
// Телефон: нижняя панель как в приложении (Сегодня · Товары · Заказы · Чат · Ещё) — под большим пальцем.
// «Ещё» раскрывает полное меню сверху.
const BOTTOM_NAV = [["/today", "Сегодня", "house"], ["/", "Товары", "boxes", ["/product"]], ["/orders", "Заказы", "shopping-cart"],
                    ["/chat", "Чат", "message-circle"]];
function renderBottomNav() {
  if (!window.matchMedia("(max-width: 640px)").matches || document.querySelector(".bottom-nav")
      || !document.querySelector(".topbar nav")) return;
  const path = location.pathname;
  const bar = document.createElement("nav");
  bar.className = "bottom-nav";
  bar.innerHTML = BOTTOM_NAV.map(([href, label, , also = []]) =>
    `<a href="${href}" class="${href === path || also.includes(path) ? "active" : ""}"><span class="bn-icon"></span><span>${esc(label)}</span></a>`).join("")
    + `<button type="button" class="bn-more"><span class="bn-icon"></span><span>${esc(I18N.tr("Ещё"))}</span></button>`;
  [...bar.querySelectorAll(".bn-icon")].forEach((el, i) => el.appendChild(iconSvg(i < BOTTOM_NAV.length ? BOTTOM_NAV[i][2] : "menu")));
  bar.querySelector(".bn-more").onclick = () => document.body.classList.toggle("nav-open");
  document.body.appendChild(bar);
  document.body.classList.add("has-bottom-nav");
}

document.addEventListener("click", (e) => {  // «Ещё ▾» закрывается кликом мимо и после выбора раздела
  const open = document.querySelector(".nav-more[open]");
  if (open && (!open.contains(e.target) || e.target.closest("a"))) open.open = false;
});
// В окне уже 1440 px меню прокручивается вбок (overflow-x), и выпадающий список внутри него обрезался бы —
// «Ещё» нажимается, а ничего не видно. Поэтому список ставим поверх страницы (fixed) под самой кнопкой.
function placeNavMore(d) {
  const box = d.querySelector(".nav-more-box");
  if (!box) return;
  if (!d.open) { box.style.cssText = ""; return; }
  const r = d.querySelector("summary").getBoundingClientRect();
  box.style.position = "fixed";
  box.style.top = `${Math.round(r.bottom + 6)}px`;
  box.style.left = `${Math.round(Math.max(8, Math.min(r.left, innerWidth - box.offsetWidth - 8)))}px`;
}
// открываем сами, а не ждём «toggle»: он приходит позже, и первый кадр список был бы обрезан
document.addEventListener("click", (e) => {
  const summary = e.target.closest && e.target.closest(".nav-more > summary");
  if (!summary) return;
  e.preventDefault();
  const d = summary.parentElement;
  d.open = !d.open;
  placeNavMore(d);
});
for (const ev of ["scroll", "resize"]) {  // список fixed — при прокрутке страницы или меню закрываем, а не оставляем висеть
  window.addEventListener(ev, (e) => {
    const open = document.querySelector(".nav-more[open]");
    if (open && !(e.target instanceof Node && open.contains(e.target))) open.open = false;
  }, true);
}

function initNav() { renderNav(); }

// Разделы меню, которые роли не открыть (сервер всё равно не пустит), — не показываем.
const NAV_PERMS = { "/settings": "admin", "/support": "admin", "/import": "catalog" };

function can(perm) {
  return !!(META.me && (META.me.perms || []).includes(perm));
}

function applyNavPerms() {
  if (!META.me) return;
  document.querySelectorAll(".topbar nav a[href]").forEach((a) => {
    const perm = NAV_PERMS[a.getAttribute("href")];
    if (perm) a.classList.toggle("hidden", !can(perm));
  });
  document.body.dataset.role = META.me.role;
  showRoleBanner();
  // кнопки, которые роли всё равно не разрешены (сервер ответит «Недостаточно прав»), — прячем: класс на body
  // действует и на то, что страница дорисует потом (полоска готовности, окна)
  for (const p of ["catalog", "ai", "orders", "chat", "admin"]) document.body.classList.toggle(`no-${p}`, !can(p));
  applyPagePerm();
}

// Чего нельзя этой роли — одной строкой под шапкой (можно скрыть до конца сеанса), чтобы «бледные» кнопки с замком
// не казались поломкой. У администратора и у ролей со всеми рабочими правами плашки нет.
const PERM_NAMES = { catalog: "менять товары и цены", ai: "ИИ", orders: "заказы и ТТН", chat: "отвечать в чате" };
function showRoleBanner() {
  const missing = Object.keys(PERM_NAMES).filter((p) => !can(p));
  if (!missing.length || document.querySelector(".role-banner")) return;
  try { if (sessionStorage.getItem("promloader-role-banner") === "hidden") return; } catch {}
  const bar = document.createElement("div");
  bar.className = "role-banner";
  bar.innerHTML = `<span>🔒 ${esc(I18N.tr("Ваша роль"))} «<b translate="no">${esc(META.me.role_label)}</b>» — ${esc(I18N.tr("нельзя"))}:
    ${missing.map((p) => esc(I18N.tr(PERM_NAMES[p]))).join(", ")}. ${esc(I18N.tr("Такие кнопки — бледные, с замком. Нужен доступ — попросите администратора."))}</span>
    <span class="spacer"></span><button type="button" title="Скрыть">✕</button>`;
  bar.querySelector("button").onclick = () => {
    bar.remove();
    try { sessionStorage.setItem("promloader-role-banner", "hidden"); } catch {}
  };
  document.querySelector(".topbar")?.after(bar);
}
// Нажатие на «запертую» кнопку — объяснение вместо действия (сервер всё равно ответил бы «Недостаточно прав»)
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-perm]");
  if (!el || !META.me || can(el.dataset.perm) || !el.matches("button, a, .btn, summary, label")) return;
  e.preventDefault();
  e.stopPropagation();
  toast(I18N.tr(`Нет права: ${PERM_NAMES[el.dataset.perm] || el.dataset.perm}. Попросите администратора дать доступ.`), "error");
}, true);

// Страница целиком для изменений (поставщики, наценка, категории), а у роли нет права: всё видно, но поля и кнопки
// закрыты — и те, что страница дорисует потом (таблицы правил, окна)
function applyPagePerm() {
  const main = document.querySelector("main[data-page-perm]");
  if (!main || can(main.dataset.pagePerm) || main.dataset.locked) return;
  main.dataset.locked = "1";
  document.body.classList.add("page-readonly");
  const note = document.createElement("div");
  note.className = "readonly-note";
  note.textContent = "👁 Только просмотр: у вашей роли нет права это менять.";
  main.prepend(note);
  const lock = () => main.querySelectorAll("input, select, textarea, button").forEach((el) => {
    if (!el.disabled && !el.closest("[data-view-ok]")) el.disabled = true;
  });
  lock();
  new MutationObserver(lock).observe(main, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
}

// 👤 в шапке: кто вошёл, смена своего пароля, «Пользователи» (администратору) и «Выйти».
function renderUserMenu() {
  applyNavPerms();
  const bar = document.querySelector(".topbar");
  const me = META.me;
  if (!bar || !me || document.getElementById("user-menu")) return;
  let right = bar.querySelector(".right");
  if (!right) {
    right = document.createElement("div");
    right.className = "right";
    bar.appendChild(right);
  }
  const box = document.createElement("details");
  box.id = "user-menu";
  box.className = "user-menu";
  box.innerHTML = `<summary title="${esc(me.role_label)}">👤 <span translate="no">${esc(me.name || me.login)}</span> ▾</summary>
    <div class="user-menu-list">
      <div class="small muted"><span translate="no">${esc(me.login)}</span> · ${esc(me.role_label)}</div>
      <div class="theme-pick" role="group" aria-label="Оформление">
        <button type="button" data-theme-pick="light" title="Светлая тема">☀</button>
        <button type="button" data-theme-pick="dark" title="Тёмная тема">☾</button>
        <button type="button" data-theme-pick="auto" title="Как в системе">Авто</button>
      </div>
      <button type="button" class="btn small" data-act="password">Сменить пароль</button>
      ${can("admin") ? `<a class="btn small" href="/settings#users">Пользователи</a>` : ""}
      <button type="button" class="btn small" data-act="logout">Выйти</button>
    </div>`;
  const markTheme = () => $$("[data-theme-pick]", box).forEach((b) =>
    b.classList.toggle("active", b.dataset.themePick === window.THEME.get()));
  markTheme();
  box.addEventListener("click", async (e) => {
    const pick = e.target.closest("[data-theme-pick]");
    if (pick) { window.THEME.set(pick.dataset.themePick); markTheme(); return; }
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "logout") {
      await api("/api/logout", { method: "POST" }).catch(() => {});
      location.href = "/login";
    } else if (act === "password") {
      box.open = false;
      openPasswordWindow();
    }
  });
  document.addEventListener("click", (e) => { if (!box.contains(e.target)) box.open = false; });
  right.appendChild(box);
}

function openPasswordWindow() {
  const box = document.createElement("div");
  box.className = "modal";
  box.innerHTML = `<form class="modal-box" style="max-width:420px">
    <h2>Сменить пароль</h2>
    <label class="field"><span>Текущий пароль</span><input name="old" type="password" autocomplete="current-password" required></label>
    <label class="field"><span>Новый пароль</span><input name="new" type="password" autocomplete="new-password" placeholder="не меньше 8 символов" required></label>
    <label class="field"><span>Новый пароль ещё раз</span><input name="new2" type="password" autocomplete="new-password" required></label>
    <p class="small muted">На других устройствах нужно будет войти заново.</p>
    <div class="toolbar"><button class="btn primary" type="submit">Сохранить</button><button class="btn" type="button" data-close>Отмена</button></div>
  </form>`;
  const close = () => box.remove();
  box.querySelector("[data-close]").onclick = close;
  box.addEventListener("click", (e) => { if (e.target === box) close(); });
  const form = box.querySelector("form");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(form.querySelector("[type=submit]"), async () => {
      try {
        await api("/api/me/password", { method: "POST", json: { old: form.old.value, new: form.new.value, new2: form.new2.value } });
        toast("Пароль изменён", "ok");
        close();
      } catch (err) { toast(err.message, "error"); }
    });
  });
  document.body.appendChild(box);
  form.old.focus();
}

// Перетаскивание файлов на всю страницу.
function onPageFileDrop(handler, { accept = (f) => true, text = "Отпустите, чтобы добавить",
                                   rejectText = "Этот файл сюда не подходит" } = {}) {
  let depth = 0;
  let overlay = null;
  const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
  window.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    depth++;
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.className = "drop-overlay";
      overlay.textContent = text;
      document.body.appendChild(overlay);
    }
  });
  window.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth && overlay) { overlay.remove(); overlay = null; }
  });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    if (overlay) { overlay.remove(); overlay = null; }
    const all = Array.from(e.dataTransfer.files);
    const files = all.filter(accept);
    const rejected = all.filter((f) => !accept(f));
    if (rejected.length) toast(`${rejected.map((f) => `«${f.name}»`).join(", ")}: ${rejectText}`, "error");
    if (files.length) handler(files);
  });
}

function pickFiles({ accept = "", multiple = true } = {}) {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = accept;
    input.multiple = multiple;
    input.onchange = () => resolve(Array.from(input.files));
    input.click();
  });
}

const isImage = (f) => f.type.startsWith("image/") || /\.(jpe?g|png|gif|webp|bmp|heic)$/i.test(f.name);

// ---------- «Сообщить о проблеме» ----------
// Ошибки страницы запоминаем — они уйдут вместе с обращением и помогут найти причину.
const JS_ERRORS_KEY = "promloader-js-errors";
function rememberJsError(text) {
  try {
    const list = JSON.parse(sessionStorage.getItem(JS_ERRORS_KEY) || "[]");
    list.push(`${new Date().toLocaleTimeString()} ${location.pathname}: ${String(text).slice(0, 500)}`);
    sessionStorage.setItem(JS_ERRORS_KEY, JSON.stringify(list.slice(-20)));
  } catch { /* хранилище недоступно — не страшно */ }
  const fab = document.querySelector(".help-fab");
  if (fab) fab.classList.add("has-errors");
}
window.addEventListener("error", (e) => rememberJsError(e.message + (e.filename ? ` (${e.filename.split("/").pop()}:${e.lineno})` : "")));
window.addEventListener("unhandledrejection", (e) => {
  const reason = e.reason;
  if (reason && (reason.network || reason.status)) return;  // ошибки сервера пользователь уже видел в сообщении
  rememberJsError("Promise: " + (reason && reason.message ? reason.message : reason));
});

function jsErrors() {
  try { return JSON.parse(sessionStorage.getItem(JS_ERRORS_KEY) || "[]"); } catch { return []; }
}

function addHelpButton() {
  if (location.pathname === "/support" || document.querySelector(".help-fab")) return;
  const a = document.createElement("a");
  a.className = "help-fab" + (jsErrors().length ? " has-errors" : "");
  a.href = "/support?from=" + encodeURIComponent(location.pathname + location.search);
  // на телефоне видно только «🆘»: кнопка поверх страницы не закрывает собой кнопки заказа и строки списка
  a.innerHTML = `🆘 <span class="help-fab-text">${esc(I18N.tr("Не получается?"))}</span>`;
  a.setAttribute("aria-label", I18N.tr("Не получается?"));
  a.title = "Сообщить о проблеме: описание и скриншот — разработчик разберётся";
  document.body.appendChild(a);
}

// Второстепенные кнопки (details.actions-menu): на компьютере — в ряд, как обычно; на телефоне — одной кнопкой
// «Действия ▾», чтобы не занимать три-четыре ряда экрана. Нажатие на действие или мимо меню его закрывает.
const NARROW = window.matchMedia("(max-width: 640px)");
function syncActionMenus() {
  $$("details.actions-menu:not(.menu-always)").forEach((d) => { d.open = !NARROW.matches; });
}
NARROW.addEventListener("change", syncActionMenus);
document.addEventListener("click", (e) => {
  $$(NARROW.matches ? "details.actions-menu[open]" : "details.actions-menu.menu-always[open]").forEach((d) => {
    if (!d.contains(e.target) || e.target.closest(".actions-box .btn")) d.open = false;
  });
});

// ---------- ИИ: значок в шапке и окно «✨ ИИ» на любой странице ----------
// Здесь — выбор провайдера и модели, цена запроса, расходы и лимиты. Ключи (секрет) вводятся только в «Настройках».

const AI_STATUS = {
  ok: ["✓", "работает"], unavailable: ["✗", "недоступна"], limit: ["⏳", "лимит"], busy: ["⚠", "перегружена"],
  error: ["⚠", "ошибка"],
};
const AI_SOURCE = { card: "карточка", bulk: "массовый", check: "проверка" };

// claude-opus-5-5 → Claude Opus 5.5; gemini-3.8-flash-lite → Gemini 3.8 Flash-Lite
function aiModelName(id) {
  if (!id) return "";
  const m = String(id).replace(/^models\//, "").replace(/-\d{8}$/, "");
  const c = m.match(/^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?$/);
  const cap = (w) => w.charAt(0).toUpperCase() + w.slice(1);
  if (c) return `Claude ${cap(c[1])} ${c[2]}${c[3] ? "." + c[3] : ""}`;
  return m.split("-").map((w) => (/^\d/.test(w) ? w : cap(w))).join(" ").replace("Flash Lite", "Flash-Lite");
}

function fmtUsd(v) {
  if (v === null || v === undefined) return "";
  if (v === 0) return "$0";
  if (v < 0.0001) return "< $0.0001";
  return "$" + v.toFixed(v < 0.01 ? 4 : v < 1 ? 3 : 2);
}

function fmtUah(v) {
  if (v === null || v === undefined) return "";
  if (v === 0) return "0 грн";
  if (v < 0.01) return "< 0,01 грн";
  return v.toLocaleString("uk-UA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " грн";
}

// цена за 1 млн токенов: $0.75, $4, $0.1
function fmtRate(v) { return v === null || v === undefined ? "" : "$" + parseFloat(v.toFixed(3)); }

// «≈ 0,13 грн ($0.0032)»; без курса — только доллары
function aiMoney(usd, uah, { free = false, withUsd = true } = {}) {
  if (free) return "бесплатно";
  if (usd === null || usd === undefined) return "цена неизвестна";
  const about = (text) => (text.startsWith("<") ? text : "≈ " + text);
  if (uah === null || uah === undefined) return about(fmtUsd(usd));
  return `${about(fmtUah(uah))}${withUsd && usd ? ` (${fmtUsd(usd)})` : ""}`;
}

function fmtTokens(n) { return Number(n || 0).toLocaleString(uiLocale()); }

// Строка под ответом ИИ: «Gemini 3.8 Flash · 1 240 + 610 токенов · ≈ 0,13 грн ($0.0032)»
function aiUsageLine(u) {
  if (!u) return "";
  return `${aiModelName(u.model)} · ${fmtTokens(u.tokens_in)} + ${fmtTokens(u.tokens_out)} токенов · ${aiMoney(u.cost_usd, u.cost_uah, { free: u.free })}`;
}

function renderAiChip() {
  const ai = META.ai;
  const bar = document.querySelector(".topbar");
  if (!bar || !ai) return;
  let chip = document.getElementById("ai-chip");
  if (!chip) {
    let right = bar.querySelector(".right");
    if (!right) {
      right = document.createElement("div");
      right.className = "right";
      bar.appendChild(right);
    }
    chip = document.createElement("button");
    chip.type = "button";
    chip.id = "ai-chip";
    chip.className = "ai-chip";
    chip.addEventListener("click", openAiWindow);
    right.prepend(chip);
  }
  chip.classList.toggle("off", !ai.enabled);
  chip.classList.toggle("bad", !!(ai.enabled && ai.key_error));
  if (ai.enabled && ai.key_error) {
    chip.innerHTML = `✨ ИИ <span class="ai-chip-spent">ключ не принят</span>`;
    chip.title = "Сервис ИИ не принял ключ — нажмите, чтобы узнать, что сделать";
    return;
  }
  if (!ai.enabled) {
    chip.innerHTML = `✨ ИИ <span class="ai-chip-spent">не подключён</span>`;
    chip.title = "ИИ-помощник не подключён — нажмите, чтобы узнать, как подключить";
    return;
  }
  const model = aiModelName(ai.model) || "авто";
  const spent = ai.free ? "бесплатно" : ai.today_requests ? aiMoney(ai.today_usd, ai.today_uah, { withUsd: false }) : "";
  chip.innerHTML = `✨ <span class="ai-chip-model">${esc(model)}</span>${spent ? `<span class="ai-chip-spent"> · сегодня ${esc(spent)}</span>` : ""}`;
  // «(программа выбирает сама)» переводится отдельно: в общей фразе это подстановка
  const which = ai.auto ? `${model} ${I18N.tr("(программа выбирает сама)")}` : model;
  chip.title = `ИИ: ${which}. Сегодня запросов: ${ai.today_requests}${spent ? `, ${spent}` : ""}.\nНажмите — сменить модель, посмотреть цены, расходы и лимиты.`;
}

// После смены модели или запроса к ИИ — обновить значок (и карточку товара, если ИИ включили/выключили)
async function refreshAiMeta() {
  try {
    const m = await api("/api/meta");
    const was = META.ai && META.ai.enabled;
    META.ai = m.ai;
    renderAiChip();
    if (was !== m.ai.enabled) document.dispatchEvent(new CustomEvent("ai-changed"));
  } catch { /* не страшно: значок обновится при следующем открытии страницы */ }
}

const aiWin = { el: null, status: null, models: null, modelsError: "", checking: false, lastCheck: null };

function openAiWindow() {
  if (!aiWin.el) {
    const el = document.createElement("div");
    el.className = "modal hidden";
    el.id = "ai-window";
    el.innerHTML = `<div class="modal-box ai-window">
      <div class="ai-win-head"><h2>✨ ИИ-помощник</h2><button type="button" class="btn small" data-close title="Закрыть (Esc)">✕</button></div>
      <div class="ai-win-body"></div></div>`;
    el.querySelector("[data-close]").addEventListener("click", () => el.classList.add("hidden"));
    document.body.appendChild(el);
    aiWin.el = el;
  }
  aiWin.el.classList.remove("hidden");
  aiWin.models = null;
  aiWin.lastCheck = null;
  loadAiWindow(true);
}

async function loadAiWindow(withModels) {
  const body = aiWin.el.querySelector(".ai-win-body");
  if (!aiWin.status) body.innerHTML = `<div class="ai-loading">Загружаю…</div>`;
  try {
    aiWin.status = await api("/api/ai/status");
  } catch (err) {
    body.innerHTML = `<p class="err-text">${esc(err.message)}</p>`;
    return;
  }
  renderAiWindow();
  if (withModels && aiWin.status.enabled) {
    aiWin.modelsError = "";
    try {
      aiWin.models = (await api("/api/ai/models")).items;
    } catch (err) {
      aiWin.models = [];
      aiWin.modelsError = err.message;
      try { aiWin.status = await api("/api/ai/status"); } catch { /* покажем то, что есть */ }  // ключ не принят — полоса сверху
    }
    renderAiWindow();
  }
}

function aiPriceUah(usd) {
  const rate = aiWin.status && aiWin.status.usd_rate;
  return usd === null || usd === undefined || !rate ? null : usd * rate;
}

function renderAiWindow() {
  const st = aiWin.status;
  const body = aiWin.el.querySelector(".ai-win-body");
  const free = st.provider === "gemini" && st.free_tier;
  const anyKey = st.keys.gemini || st.keys.claude;
  const providerSelect = `<label class="field ai-provider"><span>Сервис ИИ</span><select id="aiw-provider">
      ${st.provider ? "" : `<option value="">— выберите —</option>`}
      ${Object.entries(st.providers).map(([k, name]) => `<option value="${k}" ${k === st.provider ? "selected" : ""}
        ${st.keys[k] ? "" : "disabled"}>${esc(name)}${st.keys[k] ? "" : " — нет ключа"}</option>`).join("")}
    </select></label>`;
  if (!anyKey) {
    body.innerHTML = `<p>ИИ ещё не подключён. Он помогает в карточке и для многих товаров сразу: улучшить описание,
      перевести на украинский, придумать название и ключевые слова — и всегда показывает, сколько стоил запрос.</p>
      <p>Вставьте ключ <b>Google Gemini</b> (есть бесплатный) или <b>Anthropic Claude</b> в
      <a href="/settings#ai">Настройках</a> — потом модель, цены и расходы будут здесь, на любой странице.</p>`;
    return;
  }
  const keyError = st.key_error ? `<div class="ai-key-error">⚠️ ${esc(st.key_error.message)}
    <a href="/settings#ai">Открыть «Настройки»</a></div>` : "";
  body.innerHTML = `
    ${keyError}
    ${providerSelect}
    ${st.enabled ? `
    <section><h3>Модель</h3>
      <div class="ai-model-list" id="aiw-models">${renderAiModels()}</div>
      <p class="small muted">Цена — примерно за один запрос по карточке (около 1 500 токенов текста на входе и 900 на выходе),
        у длинных описаний дороже. Точная цена каждого запроса — ниже, в «Последних запросах».</p>
      ${st.provider === "gemini" ? `<label class="small ai-free"><input type="checkbox" id="aiw-free" ${free ? "checked" : ""}>
        Мой ключ Gemini бесплатный — запросы ничего не стоят, но их число в минуту и в сутки ограничено.
        <span class="muted">Google не сообщает тариф ключа заранее; программа отметит сама, когда Google ответит
        «лимит бесплатного уровня».</span></label>` : ""}
      <div class="toolbar" style="margin:8px 0 0">
        <button type="button" class="btn small" id="aiw-check" ${aiWin.models && aiWin.models.length ? "" : "disabled"}>Проверить модели</button>
        <span class="small muted" id="aiw-check-note">${aiCheckNote()}</span>
      </div>
    </section>` : `<p class="small muted">Выберите сервис — ключ для него уже есть.</p>`}
    <section><h3>Расходы</h3>${renderAiSpend()}</section>
    ${st.enabled ? `<section><h3>Массовый ИИ</h3>${parallelSwitch(st)}</section>` : ""}
    <section><h3>Лимиты</h3>${renderAiLimits()}</section>
    <section><h3>Последние запросы</h3>${renderAiRecent()}</section>
    <p class="small muted">Цены моделей — по прайсам Anthropic и Google на ${esc(formatDay(st.prices_checked))}; в гривнах —
      по курсу доллара программы${st.usd_rate ? ` (${st.usd_rate.toFixed(2)} грн)` : " (пока неизвестен — показываем в долларах)"}.
      Ключи и темп массового ИИ — в <a href="/settings#ai">Настройках</a>.</p>`;
  bindAiWindow();
}

function formatDay(isoDate) {
  const [y, m, d] = String(isoDate || "").split("-");
  return d ? `${d}.${m}.${y}` : isoDate;
}

function renderAiModels() {
  const st = aiWin.status;
  if (aiWin.models === null) return `<div class="ai-loading small">Узнаю, какие модели доступны вашему ключу…</div>`;
  const free = st.provider === "gemini" && st.free_tier;
  const priceOf = (m) => {
    if (free) return `<span class="ai-price">бесплатно</span>`;
    if (m.typical_usd === null) return `<span class="ai-price muted">цена неизвестна</span>`;
    const uah = aiPriceUah(m.typical_usd);
    return `<span class="ai-price">${aiMoney(m.typical_usd, uah, { withUsd: false })}</span>`;
  };
  const statusOf = (m) => {
    if (!m.status) return `<span class="ai-st muted">не проверялась</span>`;
    let [icon, label] = AI_STATUS[m.status] || ["", m.status];
    if (m.status === "limit" && m.limit_until) {
      const until = new Date(m.limit_until);
      label = until > new Date() ? `лимит до ${until.toLocaleTimeString(uiLocale(), { hour: "2-digit", minute: "2-digit" })}` : "лимит сброшен";
      if (until <= new Date()) icon = "↻";
    }
    return `<span class="ai-st st-${esc(m.status)}" title="${esc(m.status_message || "")}${m.checked_at ? ` · ${esc(formatDate(m.checked_at))}` : ""}">${icon} ${esc(label)}</span>`;
  };
  const row = (value, title, sub, price, status, missing = false) => `
    <label class="ai-model${value === (st.model || "") ? " selected" : ""}${missing ? " missing" : ""}">
      <input type="radio" name="aiw-model" value="${esc(value)}" ${value === (st.model || "") ? "checked" : ""}>
      <span class="ai-model-name">${title}<span class="small muted">${sub}</span></span>
      ${price}${status}
    </label>`;
  let html = "";
  if (st.provider === "gemini") {
    const used = st.model_used ? aiModelName(st.model_used) : "";
    html += row("", "Авто — программа выбирает сама", used ? `сейчас: ${esc(used)} · сама перейдёт на другую, если Google уберёт модель`
      : "лучшая доступная Flash; сама перейдёт на другую, если Google уберёт модель", "", "");
  }
  html += aiWin.models.map((m) => row(m.id, esc(m.name && m.name !== m.id ? m.name : aiModelName(m.id)),
    `${esc(m.id)}${m.price_in !== null ? ` · вход ${fmtRate(m.price_in)} · выход ${fmtRate(m.price_out)} за 1 млн токенов${m.price_exact ? "" : " (оценка по семейству)"}` : ""}${m.missing ? " · <b>ключу эта модель не видна</b>" : ""}`,
    priceOf(m), statusOf(m), m.missing)).join("");
  if (aiWin.modelsError && !st.key_error) html += `<p class="err-text small">${esc(aiWin.modelsError)}</p>`;
  return html;
}

function aiCheckModels() {
  const st = aiWin.status;
  const list = (aiWin.models || []).filter((m) => !m.missing);
  const current = st.model || st.model_used;
  const ordered = [...list.filter((m) => m.id === current), ...list.filter((m) => m.id !== current)];
  return ordered.slice(0, 8);
}

function aiCheckNote() {
  if (aiWin.checking) return "Проверяю — по короткому запросу к каждой модели…";
  if (aiWin.lastCheck) return aiWin.lastCheck;
  const models = aiCheckModels();
  if (!models.length) return "";
  const free = aiWin.status.provider === "gemini" && aiWin.status.free_tier;
  const usd = models.reduce((sum, m) => sum + (m.check_usd || 0), 0);
  const cost = free ? "бесплатно" : aiMoney(usd, aiPriceUah(usd));
  return `Короткий запрос к ${models.length} моделям (первые в списке) — ${cost}.`;
}

function renderAiSpend() {
  const s = aiWin.status.summary;
  const free = aiWin.status.provider === "gemini" && aiWin.status.free_tier;
  const line = (label, p) => `<div><span class="muted">${label}</span> <b>${p.requests ? aiMoney(p.cost_usd, p.cost_uah) : "0 грн"}</b>
    <span class="small muted">· ${p.requests} запр. · ${fmtTokens(p.tokens_in)} + ${fmtTokens(p.tokens_out)} токенов${p.unpriced ? ` · у ${p.unpriced} цена неизвестна` : ""}</span></div>`;
  const byModel = s.by_model.length ? `<div class="table-scroll"><table class="list compact"><thead><tr><th>Модель (за месяц)</th><th>Запросов</th><th>Сумма</th></tr></thead><tbody>
    ${s.by_model.map((m) => `<tr><td>${esc(aiModelName(m.model))}</td><td>${m.requests}</td><td class="nowrap">${aiMoney(m.cost_usd, aiPriceUah(m.cost_usd))}</td></tr>`).join("")}
    </tbody></table></div>` : "";
  return `<div class="ai-spend">${line("Сегодня:", s.today)}${line("За месяц:", s.month)}</div>
    ${free ? `<p class="small muted">Ключ Gemini отмечен как бесплатный — запросы к Gemini считаются по 0.</p>` : ""}${byModel}`;
}

// ---------- «Один поток / Много потоков» массового ИИ (2.30.0) ----------
// Один и тот же переключатель — в окне «✨ ИИ», в окне массового ИИ и в «Настройках»: ползунок едет к выбранному
// режиму, все переключатели на странице меняются вместе. st — /api/ai/status (или {parallel, streams, key_count}).
function parallelSwitch(st) {
  const on = !!st.parallel;
  return `<div class="threads-box">
    <button type="button" class="threads-switch${on ? " on" : ""}" role="switch" aria-checked="${on}" data-parallel-switch data-perm="ai"
      title="Сколько запросов массового ИИ идёт одновременно">
      <span class="threads-knob" aria-hidden="true"></span>
      <span class="threads-opt opt-one">${esc(I18N.tr("Один поток"))}</span><span class="threads-opt opt-many">${esc(I18N.tr("Много потоков"))}</span>
    </button>
    <div class="small muted threads-note">${esc(parallelNote(st))}</div></div>`;
}

function parallelNote(st) {
  const keys = st.key_count || 0;
  if (!st.parallel) return I18N.tr("Запросы идут по одному — бережно к лимиту ключа.");
  if (keys > 1) return I18N.tr(`Одновременно запросов: ${st.streams} — по потоку на ключ (ключей: ${keys}), у каждого ключа свой лимит, вместе быстрее.`);
  return I18N.tr(`Одновременно запросов: ${st.streams} на одном ключе — быстрее, пока ИИ отвечает медленнее лимита. Добавьте запасные ключи в «Настройках» — потоков станет больше.`);
}

document.addEventListener("click", async (e) => {
  const sw = e.target.closest("[data-parallel-switch]");
  if (!sw || sw.disabled) return;
  const multi = sw.getAttribute("aria-checked") !== "true";
  $$("[data-parallel-switch]").forEach((x) => { x.classList.toggle("on", multi); x.setAttribute("aria-checked", String(multi)); });
  try {
    const st = await api("/api/ai/parallel", { method: "POST", json: { multi } });
    $$(".threads-note").forEach((n) => { n.textContent = parallelNote(st); });
    if (typeof aiWin !== "undefined" && aiWin.status) aiWin.status = st;
    document.dispatchEvent(new CustomEvent("pl-parallel", { detail: st }));
  } catch (err) {
    $$("[data-parallel-switch]").forEach((x) => { x.classList.toggle("on", !multi); x.setAttribute("aria-checked", String(!multi)); });
    toast(err.message, "error");
  }
});

function renderAiLimits() {
  const st = aiWin.status;
  const lim = st.limits || {};
  const pace = `<div class="small muted">Массовый ИИ отправляет не больше <b>${st.pace_per_minute}</b> запросов в минуту
    с каждого ключа — так он не упирается в лимит (меняется в <a href="/settings#ai">Настройках</a>).
    ${st.key_count > 1 ? `Ключей: <b>${st.key_count}</b> — упрётся один в лимит, программа сама перейдёт на следующий.` : ""}</div>`;
  const today = st.summary.today.requests;
  if (!st.enabled) return `<p class="small muted">Появятся, когда выберете сервис.</p>`;
  if (st.provider === "claude") {
    const names = { requests: "Запросы", input_tokens: "Входные токены", output_tokens: "Выходные токены", tokens: "Токены" };
    const rows = Object.entries(names).filter(([k]) => lim[k]).map(([k, label]) => {
      const x = lim[k];
      const low = x.limit && x.remaining !== null && x.remaining / x.limit < 0.1;
      const reset = x.reset ? new Date(x.reset).toLocaleTimeString(uiLocale()) : "";
      return `<tr><td>${label}</td><td class="nowrap${low ? " err-text" : ""}">${x.remaining !== null ? fmtTokens(x.remaining) : "?"} из ${x.limit !== null ? fmtTokens(x.limit) : "?"}</td>
        <td class="small muted">${reset ? `полностью восстановится к ${reset}` : ""}</td></tr>`;
    }).join("");
    if (!rows) return `<p class="small muted">Anthropic сообщает лимиты в каждом ответе — они появятся здесь после первого запроса к Claude.</p>${pace}`;
    return `<p class="small muted" style="margin-top:0">Лимиты вашего ключа в минуту — по последнему ответу Claude (${esc(formatDate(lim.at))}):</p>
      <div class="table-scroll"><table class="list compact"><thead><tr><th></th><th>Осталось</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      ${lim.retry_seconds ? `<p class="small err-text">Лимит был исчерпан — Anthropic просил подождать ${Math.round(lim.retry_seconds)} с.</p>` : ""}${pace}`;
  }
  const last = lim.last_limit;
  return `<p class="small" style="margin-top:0">Сделано запросов: за последнюю минуту <b>${st.summary.last_minute}</b>, сегодня <b>${today}</b>.</p>
    <p class="small muted">Google не сообщает, сколько запросов осталось, — только когда лимит уже исчерпан.
      Лимиты вашего ключа по каждой модели видны в <a href="https://aistudio.google.com/" target="_blank" rel="noopener">Google AI Studio</a>.</p>
    ${last ? `<p class="small">Последний раз упёрлись в лимит ${esc(formatDate(lim.at))}: <b>${esc(last.text || "")}</b></p>` : ""}${pace}`;
}

function renderAiRecent() {
  const rows = aiWin.status.summary.recent;
  if (!rows.length) return `<p class="small muted">Запросов к ИИ ещё не было.</p>`;
  const actions = aiWin.status.actions || {};
  return `<div class="table-scroll"><table class="list compact ai-recent"><thead><tr><th>Когда</th><th>Что</th><th>Модель</th><th>Токены</th><th>Цена</th></tr></thead><tbody>
    ${rows.map((r) => `<tr class="${r.ok ? "" : "failed"}">
      <td class="nowrap small">${esc(formatDate(r.at))}</td>
      <td class="small">${esc(r.action === "check" ? "проверка модели" : actions[r.action] || r.action)}<div class="muted">${esc(AI_SOURCE[r.source] || r.source)}</div></td>
      <td class="small">${esc(aiModelName(r.model))}</td>
      <td class="nowrap small">${r.ok ? `${fmtTokens(r.tokens_in)} + ${fmtTokens(r.tokens_out)}` : ""}</td>
      <td class="small">${r.ok ? (r.cost_usd === null ? "цена неизвестна" : esc(aiMoney(r.cost_usd, aiPriceUah(r.cost_usd)))) : `<span class="err-text" title="${esc(r.error)}">✗ ${esc(r.error.slice(0, 80))}</span>`}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

function bindAiWindow() {
  const el = aiWin.el;
  const choose = async (json, done) => {
    try {
      aiWin.status = { ...aiWin.status, ...(await api("/api/ai/model", { method: "POST", json })) };
      if (done) toast(done, "ok");
    } catch (err) { toast(err.message, "error"); }
    refreshAiMeta();
  };
  const provider = el.querySelector("#aiw-provider");
  if (provider) provider.addEventListener("change", async () => {
    await choose({ provider: provider.value }, `ИИ: ${aiWin.status.providers[provider.value]}`);
    aiWin.models = null;
    loadAiWindow(true);
  });
  el.querySelectorAll('input[name="aiw-model"]').forEach((r) => r.addEventListener("change", async () => {
    await choose({ model: r.value }, r.value ? `Модель: ${aiModelName(r.value)}` : "Модель выбирается автоматически");
    renderAiWindow();
  }));
  const freeBox = el.querySelector("#aiw-free");
  if (freeBox) freeBox.addEventListener("change", async () => {
    await choose({ free_tier: freeBox.checked });
    loadAiWindow(false);
  });
  const check = el.querySelector("#aiw-check");
  if (check) check.addEventListener("click", () => busy(check, async () => {
    const models = aiCheckModels().map((m) => m.id);
    aiWin.checking = true;
    el.querySelector("#aiw-check-note").textContent = aiCheckNote();
    try {
      const res = await api("/api/ai/models/check", { method: "POST", json: { models } });
      const count = (s) => res.items.filter((x) => x.status === s).length;
      aiWin.lastCheck = `Проверено ${res.items.length}: работают ${count("ok")}` +
        (count("unavailable") ? `, недоступны ${count("unavailable")}` : "") + (count("limit") ? `, упёрлись в лимит ${count("limit")}` : "") +
        (count("busy") ? `, перегружены ${count("busy")}` : "") + (count("error") ? `, с ошибкой ${count("error")}` : "") +
        `. Проверка стоила ${aiWin.status.provider === "gemini" && aiWin.status.free_tier ? "0 (бесплатный ключ)" : aiMoney(res.spent_usd, res.spent_uah)}.`;
      aiWin.models = (await api("/api/ai/models")).items;
    } catch (err) {
      toast(err.message, "error");
    } finally {
      aiWin.checking = false;
    }
    await loadAiWindow(false);
    refreshAiMeta();
  }));
}

// RU | UK — язык интерфейса: запоминается в этом браузере, страница перезагружается
function renderLangSwitch() {
  const bar = document.querySelector(".topbar");
  if (!bar || document.getElementById("lang-switch")) return;
  let right = bar.querySelector(".right");
  if (!right) {
    right = document.createElement("div");
    right.className = "right";
    bar.appendChild(right);
  }
  const current = (window.I18N && window.I18N.lang) || "ru";
  const box = document.createElement("span");
  box.id = "lang-switch";
  box.className = "lang-switch";
  box.setAttribute("translate", "no");
  box.innerHTML = [["ru", "Русский"], ["uk", "Українська"]].map(([code, name]) =>
    `<button type="button" data-lang="${code}" title="${name}"${code === current ? ' class="active"' : ""}>${code.toUpperCase()}</button>`).join("");
  box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-lang]");
    if (b && b.dataset.lang !== current && window.I18N) window.I18N.set(b.dataset.lang);
  });
  right.prepend(box);
}

renderNav();
renderLangSwitch();
syncActionMenus();
document.addEventListener("DOMContentLoaded", () => { initNav(); syncActionMenus(); addHelpButton(); });


// ---------- приложение на телефоне (PWA) ----------
// Service worker нужен для значка «как приложение» и push-уведомлений. Браузер разрешает его только на https
// (адрес Cloudflare) или localhost — по Wi‑Fi (http://192.168…) значок на экран добавить можно, push — нет.
let installPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();  // покажем свою кнопку «📲 Установить на телефон» в «Настройках»
  installPrompt = e;
  document.dispatchEvent(new Event("pl-install-ready"));
});
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// ---------- значки вместо эмодзи (2.29.0) ----------
// Эмодзи на Windows, Android и iPhone рисуются по-разному и выглядят несерьёзно. Тексты (и словарь перевода) их
// сохраняют, а здесь, уже после перевода, эмодзи в кнопках, меню, заголовках и плашках заменяются аккуратными
// значками Lucide из static/vendor (без интернета). Данные пользователя (translate="no"), поля ввода, выпадающие
// списки и превью страницы Prom не трогаются. Стрелки, ✓ и ▾ — обычные символы шрифта, их не меняем.
const ICON_SPRITE = "/static/vendor/lucide-1.49.0.svg";
const ICONS = {
  "✨": "sparkles", "📦": "package", "🔒": "lock", "⚠": "triangle-alert", "⬇": "download", "📷": "camera",
  "💲": "circle-dollar-sign", "🚚": "truck", "👤": "user", "👁": "eye", "🔔": "bell", "🔍": "search", "🔎": "search",
  "✅": "circle-check", "💬": "message-circle", "👥": "users", "✏": "pencil", "📉": "trending-down", "⏳": "hourglass",
  "📲": "smartphone", "📱": "smartphone", "⚡": "zap", "🔐": "lock-keyhole", "👋": "hand", "📄": "file-text", "📨": "send",
  "⏰": "alarm-clock", "🔄": "refresh-cw", "☁": "cloud", "🕘": "history", "💱": "arrow-left-right", "💳": "credit-card",
  "🔗": "link", "❌": "circle-x", "🛒": "shopping-cart", "🔧": "wrench", "🖼": "image", "💾": "save", "🖨": "printer",
  "⏸": "pause", "🚀": "rocket", "☀": "sun", "☾": "moon", "📎": "paperclip", "🆘": "life-buoy", "🗂": "folder-tree",
  "🎨": "palette", "▶": "play",
};
const ICON_RE = new RegExp(`(${Object.keys(ICONS).map((e) => e.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})\ufe0f?`, "u");
const ICON_SKIP = "[translate='no'], textarea, input, select, option, script, style, [contenteditable], .pp, .tile, svg";

function iconSvg(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", `i i-${name}`);
  svg.setAttribute("aria-hidden", "true");
  for (const [k, v] of Object.entries({ fill: "none", stroke: "currentColor", "stroke-width": "2",
    "stroke-linecap": "round", "stroke-linejoin": "round" })) svg.setAttribute(k, v);
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", `${ICON_SPRITE}#${name}`);
  svg.appendChild(use);
  return svg;
}

function iconizeText(node) {
  const parent = node.parentElement;
  if (!parent || !ICON_RE.test(node.data) || parent.closest(ICON_SKIP)) return;
  const frag = document.createDocumentFragment();
  let rest = node.data;
  let m;
  while ((m = rest.match(ICON_RE))) {
    if (m.index) frag.appendChild(document.createTextNode(rest.slice(0, m.index)));
    frag.appendChild(iconSvg(ICONS[m[1]]));
    rest = rest.slice(m.index + m[0].length);
  }
  if (rest) frag.appendChild(document.createTextNode(rest));
  node.replaceWith(frag);
}

function iconize(root) {
  if (root.nodeType === 3) { iconizeText(root); return; }
  if (root.nodeType !== 1 || root.closest(ICON_SKIP)) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const found = [];
  while (walker.nextNode()) if (ICON_RE.test(walker.currentNode.data)) found.push(walker.currentNode);
  found.forEach(iconizeText);
}

// Только после перевода (i18n.js переводит по DOMContentLoaded и следит за изменениями): иначе строка «… меню 👤»
// разрезалась бы на кусочки раньше, чем переводчик найдёт её в словаре, и осталась бы по-русски. Наблюдатель
// создаётся позже переводчика — и срабатывает после него на тех же изменениях.
function startIcons() {
  iconize(document.body);
  new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === "characterData") iconizeText(r.target);
      else r.addedNodes.forEach(iconize);
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true });
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startIcons);
else startIcons();

renderBottomNav();  // после значков: панели нужны iconSvg и ICON_SPRITE
