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
    const err = new Error("Нет связи с сервером");
    err.network = true;
    throw err;
  }
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

function initNav() {
  const path = location.pathname;
  $$(".topbar nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === path));
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
  a.textContent = "🆘 Не получается?";
  a.title = "Сообщить о проблеме: описание и скриншот — разработчик разберётся";
  document.body.appendChild(a);
}

document.addEventListener("DOMContentLoaded", () => { initNav(); addHelpButton(); });
