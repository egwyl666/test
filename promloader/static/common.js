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
    throw err;
  }
  return body;
}

async function loadMeta() {
  Object.assign(META, await api("/api/meta"));
  showUpdateBanner();
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
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

// Описание: обычный текст -> абзацы; HTML -> только безопасные теги.
const ALLOWED_TAGS = new Set(["P", "BR", "B", "STRONG", "I", "EM", "U", "UL", "OL", "LI", "H2", "H3", "H4", "TABLE", "TBODY", "THEAD", "TR", "TD", "TH", "SPAN", "DIV", "A", "IMG"]);
const ALLOWED_ATTRS = { A: ["href"], IMG: ["src", "alt"] };

// Эти теги удаляются вместе с содержимым; остальные неразрешённые — «разворачиваются» (остаётся текст).
const DROP_TAGS = new Set(["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "XML", "META", "TITLE", "HEAD", "LINK",
  "NOSCRIPT", "TEMPLATE", "SVG", "MATH", "FORM", "INPUT", "BUTTON", "SELECT", "TEXTAREA"]);

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
        const ok = (ALLOWED_ATTRS[tag] || []).includes(attr.name) && !/^\s*(javascript|vbscript):/i.test(attr.value);
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
function onPageFileDrop(handler, { accept = (f) => true, text = "Отпустите, чтобы добавить" } = {}) {
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
    const files = Array.from(e.dataTransfer.files).filter(accept);
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

document.addEventListener("DOMContentLoaded", initNav);
