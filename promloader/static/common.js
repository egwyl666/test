// Общие помощники для всех страниц.

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
    let detail = body && body.detail !== undefined ? body.detail : body;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    const err = new Error(typeof detail === "string" && detail ? detail : `Ошибка ${response.status}`);
    err.status = response.status;
    err.field = body && body.field;
    throw err;
  }
  return body;
}

async function loadMeta() {
  Object.assign(META, await api("/api/meta"));
  renderAiChip();
  showUpdateBanner();
  showTokenBanner();
  showFailedSuppliers();
  showMissedSchedules();
  const link = document.querySelector('.topbar nav a[href="/orders"]');
  if (link) link.innerHTML = `Заказы${META.orders_unseen ? ` <span class="nav-badge">${META.orders_unseen}</span>` : ""}`;
  return META;
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
  bar.innerHTML = `⚠️ Поставщик <b>«${esc(first.name)}»</b> не обновился: ${esc((first.message || "").slice(0, 160))}
    <a href="/supplier?id=${first.id}">Открыть</a>${show.length > 1 ? ` · и ещё ${show.length - 1} — <a href="/suppliers">все поставщики</a>` : ""}`;
  document.querySelector(".topbar")?.insertAdjacentElement("afterend", bar);
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
      ${Object.keys(p.fields || {}).length ? `<p class="small muted">Что меняется: ${Object.entries(p.fields).map(([k, n]) => `${esc(k)} — ${n}`).join(", ")}</p>` : ""}
      ${p.samples.prices.length ? `<h3>Цены${p.price_changed > p.samples.prices.length ? ` (первые ${p.samples.prices.length})` : ""}</h3>` : ""}
      ${table(p.samples.prices, "<th>Товар</th><th>Было</th><th>Станет</th>", (r) =>
        `<tr><td>${esc(r.name)}<div class="small muted">${esc(r.code)}</div></td><td class="nowrap">${money(r.old, r.currency)}</td>
         <td class="nowrap"><b>${money(r.new, r.currency)}</b>${pct(r.old, r.new)}</td></tr>`)}
      ${p.samples.created.length ? `<h3>Новые товары${p.created > p.samples.created.length ? ` (первые ${p.samples.created.length})` : ""}</h3>` : ""}
      ${table(p.samples.created, "<th>Товар</th><th>Артикул</th>", (r) => `<tr><td>${esc(r.name)}</td><td class="small">${esc(r.code)}</td></tr>`)}
      ${p.samples.gone.length ? `<h3>Станут «нет в наличии»</h3>` : ""}
      ${table(p.samples.gone, "<th>Товар</th><th>Артикул</th>", (r) => `<tr><td>${esc(r.name)}</td><td class="small">${esc(r.code)}</td></tr>`)}`}
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
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year, hour: "2-digit", minute: "2-digit" });
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
const NAV = [
  ["/", "Товары", ["/product"]],
  ["/orders", "Заказы"],
  ["/suppliers", "Поставщики", ["/supplier"]],
  ["/pricing", "Наценка"],
  ["/changes", "Журнал"],
  ["/import", "Импорт файла"],
  ["/diagnose", "Проверка выгрузки"],
  ["/settings", "Настройки"],
  ["/support", "Поддержка"],
];

function renderNav() {
  const nav = document.querySelector(".topbar nav");
  if (!nav || nav.children.length) return;
  const path = location.pathname;
  nav.innerHTML = NAV.map(([href, label, also = []]) =>
    `<a href="${href}"${href === path || also.includes(path) ? ' class="active"' : ""}>${esc(label)}</a>`).join("");
}

function initNav() { renderNav(); }

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
  a.textContent = "🆘 Не получается?";
  a.title = "Сообщить о проблеме: описание и скриншот — разработчик разберётся";
  document.body.appendChild(a);
}

// Второстепенные кнопки (details.actions-menu): на компьютере — в ряд, как обычно; на телефоне — одной кнопкой
// «Действия ▾», чтобы не занимать три-четыре ряда экрана. Нажатие на действие или мимо меню его закрывает.
const NARROW = window.matchMedia("(max-width: 640px)");
function syncActionMenus() {
  $$("details.actions-menu").forEach((d) => { d.open = !NARROW.matches; });
}
NARROW.addEventListener("change", syncActionMenus);
document.addEventListener("click", (e) => {
  if (!NARROW.matches) return;
  $$("details.actions-menu[open]").forEach((d) => {
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

function fmtTokens(n) { return Number(n || 0).toLocaleString("ru-RU"); }

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
  chip.title = `ИИ: ${model}${ai.auto ? " (программа выбирает сама)" : ""}. Сегодня запросов: ${ai.today_requests}`
    + `${spent ? `, ${spent}` : ""}.\nНажмите — сменить модель, посмотреть цены, расходы и лимиты.`;
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
      label = until > new Date() ? `лимит до ${until.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}` : "лимит сброшен";
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

function renderAiLimits() {
  const st = aiWin.status;
  const lim = st.limits || {};
  const pace = `<div class="small muted">Массовый ИИ отправляет не больше <b>${st.pace_per_minute}</b> запросов в минуту
    — так он не упирается в лимит (меняется в <a href="/settings#ai">Настройках</a>).</div>`;
  const today = st.summary.today.requests;
  if (!st.enabled) return `<p class="small muted">Появятся, когда выберете сервис.</p>`;
  if (st.provider === "claude") {
    const names = { requests: "Запросы", input_tokens: "Входные токены", output_tokens: "Выходные токены", tokens: "Токены" };
    const rows = Object.entries(names).filter(([k]) => lim[k]).map(([k, label]) => {
      const x = lim[k];
      const low = x.limit && x.remaining !== null && x.remaining / x.limit < 0.1;
      const reset = x.reset ? new Date(x.reset).toLocaleTimeString("ru-RU") : "";
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

renderNav();
syncActionMenus();
document.addEventListener("DOMContentLoaded", () => { initNav(); syncActionMenus(); addHelpButton(); });
