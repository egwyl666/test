// Список товаров, массовые действия и очередь отправки.

const PAGE = 200;
// фильтры списка — те же имена, что у /api/products; хранятся и в адресе страницы (?group=…&sort=…)
const FILTERS = ["status", "q", "supplier", "group", "presence", "on_prom", "no_photo", "gone", "errors"];
const START = new URLSearchParams(location.search);
const list = {
  ...Object.fromEntries(FILTERS.map((k) => [k, START.get(k) || ""])),
  sort: START.get("sort") || "updated",
  items: [], total: 0, selected: new Set(),
  allMatching: false,  // «выбраны все N по фильтру», а не только загруженные строки
};

function filterParams() { return Object.fromEntries(FILTERS.map((k) => [k, list[k]])); }

// адрес страницы = текущий вид списка: «← К списку» из карточки вернёт сюда же
function syncUrl() {
  const url = new URL(location.href);
  for (const k of [...FILTERS, "sort"]) {
    const v = list[k];
    if (v && !(k === "sort" && v === "updated")) url.searchParams.set(k, v); else url.searchParams.delete(k);
  }
  history.replaceState(null, "", url);
  try { sessionStorage.setItem("promloader-list-url", url.pathname + url.search); } catch {}
}

function applyFilters() {
  list.items = [];
  resetSelection();
  syncUrl();
  renderFilterControls();
  loadProducts();
}

// Что отправлять в массовые действия: отмеченные строки или «все по текущему фильтру»
function selection() {
  return list.allMatching ? { filter: filterParams() } : { ids: [...list.selected] };
}
function selectionCount() { return list.allMatching ? list.total : list.selected.size; }

async function loadProducts(append = false) {
  const offset = append ? list.items.length : 0;
  // при автообновлении перечитываем столько, сколько уже показано
  const limit = append ? PAGE : Math.max(PAGE, list.items.length);
  const params = new URLSearchParams({ ...filterParams(), sort: list.sort, limit, offset });
  // номер запроса: ответ на старый фильтр, пришедший позже нового, не должен перерисовать список
  const seq = (list.seq = (list.seq || 0) + 1);
  const data = await api(`/api/products?${params}`);
  if (seq !== list.seq) return;
  $("#export-xlsx").href = "/api/products.xlsx?" + new URLSearchParams({ ...filterParams(), sort: list.sort });
  list.items = append ? list.items.concat(data.items) : data.items;
  list.total = data.total;
  $("#shown").textContent = list.total ? `показано ${list.items.length} из ${list.total}` : "";
  $("#more").classList.toggle("hidden", list.items.length >= list.total);
  // отметки сохраняем для всех показанных строк (раньше «Показать ещё» сбрасывал отмеченные выше)
  const ids = new Set(list.items.map((p) => p.id));
  list.selected = new Set([...list.selected].filter((id) => ids.has(id)));
  renderChips(data.counts);
  renderRows();
  // пока товары удаляются с Prom — обновляем список, чтобы было видно, как они уходят
  if (data.counts.deleting) refreshList(5000);
}

// Фоновое обновление списка (очередь отправки, ИИ, удаление, каталог) — один общий таймер: раньше их было до
// трёх, и список перечитывался по несколько раз подряд
function refreshList(delay = 0) {
  clearTimeout(list.refreshTimer);
  list.refreshTimer = setTimeout(() => loadProducts().catch(() => {}), delay);
}

function renderChips(counts) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const chips = [["", "Все", total], ...Object.entries(META.statuses).map(([k, v]) => [k, v, counts[k] || 0])];
  $("#chips").innerHTML = chips
    .filter(([k, , n]) => !k || n)
    .map(([k, label, n]) => `<button class="chip ${k === list.status ? "active" : ""}" data-status="${k}">${esc(label)}<b>${n}</b></button>`)
    .join("");
  $$("#chips .chip").forEach((c) => c.addEventListener("click", () => {
    list.status = c.dataset.status;
    applyFilters();
  }));
}

// Закупка в своей валюте и в гривнах по курсу. Нет закупки — курс и наценка на цену не действуют.
function costCell(p) {
  const cur = p.cost_currency || "UAH";
  const uah = (v) => cur !== "UAH" && v !== null ? `<div class="small muted">≈ ${esc(formatPrice(v, "UAH"))}</div>` : "";
  if (p.cost_price !== null) return `${esc(formatPrice(p.cost_price, cur))}${uah(p.cost_uah)}`;
  if (p.rrp !== null) return `<span class="small muted">РРЦ</span> ${esc(formatPrice(p.rrp, cur))}${uah(p.rrp_uah)}`;
  return `<span class="muted" title="Закупки нет: курс и наценка на эту цену не действуют. Если цена — это опт: отметьте товар → «💲 Цены»">—</span>`;
}

// Цена + наценка к закупке, «вручную», если цену меняли руками, и прежняя цена из журнала
function priceCell(p) {
  const notes = [];
  const base = p.cost_price !== null ? p.cost_uah ?? p.cost_price : null;
  if (base && p.price && (p.currency || "UAH") === "UAH") {
    const pct = Math.round((p.price / base - 1) * 100);
    notes.push(`<span title="Наценка к закупке по текущему курсу">${pct >= 0 ? "+" : ""}${pct}%</span>`);
  }
  if (p.locked_fields.includes("price")) notes.push(`<span title="Цену поменяли руками — по курсу и наценке не пересчитывается">🔒 вручную</span>`);
  const ch = p.price_change;
  if (ch && ch.old && ch.old !== ch.new) {
    const up = Number(ch.new) > Number(ch.old);
    notes.push(`<span class="${up ? "price-up" : "price-down"}" title="${esc(formatDate(ch.at))} · ${esc(ch.source)}">было ${esc(formatPrice(ch.old, ch.old_currency || p.currency))}</span>`);
  }
  const costSm = p.cost_price !== null
    ? `<div class="small muted show-sm">закупка ${esc(formatPrice(p.cost_price, p.cost_currency || "UAH"))}</div>` : "";
  return `${esc(formatPrice(p.price, p.currency))}${notes.length ? `<div class="small price-notes">${notes.join(" · ")}</div>` : ""}${costSm}`;
}

function renderRows() {
  const tbody = $("#rows");
  const filtered = FILTERS.some((k) => list[k] !== "");
  $("#empty").classList.toggle("hidden", list.items.length > 0 || filtered);
  $("#nothing-found").classList.toggle("hidden", list.items.length > 0 || !filtered);
  tbody.innerHTML = list.items.map((p) => {
    const problems = p.check.errors.length ? `<div class="err-text">${esc(p.check.errors.join(" · "))}</div>` : "";
    const promError = p.status === "error" && p.last_error ? `<div class="err-text" title="${esc(p.last_error)}">Prom: ${esc(p.last_error.slice(0, 120))}</div>` : "";
    const deleting = p.status === "deleting" ? `<div class="small deleting-note">${p.last_error
      ? `<span class="err-text">${esc(p.last_error.slice(0, 160))}</span>` : "Ждём подтверждения от Prom…"}
      <button class="btn small" data-del="retry">Повторить</button>
      <button class="btn small" data-del="cancel">Отменить удаление</button></div>` : "";
    return `
    <tr class="item" data-id="${p.id}">
      <td><input type="checkbox" class="sel" ${list.selected.has(p.id) ? "checked" : ""}></td>
      <td>${p.thumb ? `<img class="thumb" src="${esc(p.thumb)}" alt="" loading="lazy">` : `<div class="thumb empty">▢</div>`}</td>
      <td>
        <a class="name" href="/product?id=${p.id}">${esc(p.name) || '<span class="muted">Без названия</span>'}</a>
        <div class="small muted">${esc(p.external_id)} · фото: ${p.image_count}${p.supplier_name ? ` · ${esc(p.supplier_name)}` : ""}${p.locked_fields.length ? ` · <span title="Поля, изменённые вручную: ${esc(p.locked_fields.join(", "))}">🔒 ${p.locked_fields.length}</span>` : ""}</div>
        <div class="show-sm" style="margin-top:4px">${statusBadge(p.status)}</div>
        ${problems}${promError}${deleting}
      </td>
      <td class="hide-sm">${esc(p.group_name)}</td>
      <td class="hide-sm cost">${costCell(p)}</td>
      <td class="price">${priceCell(p)}</td>
      <td class="hide-sm small">${esc(META.presence[p.presence] || "")}${p.quantity !== null ? ` · ${p.quantity}` : ""}</td>
      <td class="hide-sm">${statusBadge(p.status)}${p.synced_at && p.status !== "synced" ? `<span class="on-prom-note" title="Товар уже есть на Prom; изменения уйдут при следующей отправке">● есть на Prom</span>` : ""}</td>
      <td class="hide-sm small muted">${esc(formatDate(p.updated_at))}</td>
    </tr>`;
  }).join("");

  $$("#rows tr.item").forEach((tr) => {
    const id = Number(tr.dataset.id);
    tr.addEventListener("click", async (e) => {
      if (e.target.classList.contains("sel") || e.target.closest("a")) return;  // ссылка-название откроется сама
      const del = e.target.closest("[data-del]");
      if (del) {
        try {
          await api(`/api/products/delete/${del.dataset.del}`, { method: "POST", json: { ids: [id] } });
          toast(del.dataset.del === "retry" ? "Пробую удалить ещё раз" : "Удаление отменено", "ok");
          loadProducts();
        } catch (err) { toast(err.message, "error"); }
        return;
      }
      location.href = `/product?id=${id}`;
    });
    tr.querySelector(".sel").addEventListener("change", (e) => {
      if (e.target.checked) list.selected.add(id); else list.selected.delete(id);
      list.allMatching = false;
      updateBulk();
    });
  });
  updateBulk();
}

function updateBulk() {
  const n = selectionCount();
  const pageAll = list.selected.size > 0 && list.selected.size === list.items.length;
  $("#selected-count").innerHTML = !n ? "" : list.allMatching
    ? `выбраны все ${n} по фильтру · <a href="#" id="select-page">только показанные</a>`
    : `выбрано: ${n}` + (pageAll && list.total > list.items.length
      ? ` · <a href="#" id="select-all-matching">выбрать все ${list.total} по фильтру</a>` : "");
  $$("#bulk [data-action]").forEach((b) => (b.disabled = !n));
  $("#check-all").checked = pageAll || list.allMatching;
  const allLink = $("#select-all-matching");
  if (allLink) allLink.onclick = (e) => { e.preventDefault(); list.allMatching = true; updateBulk(); };
  const pageLink = $("#select-page");
  if (pageLink) pageLink.onclick = (e) => { e.preventDefault(); list.allMatching = false; updateBulk(); };
}

$("#check-all").addEventListener("change", (e) => {
  list.selected = e.target.checked ? new Set(list.items.map((p) => p.id)) : new Set();
  list.allMatching = false;
  renderRows();
});

// фильтр поменялся — «все по фильтру» больше не про то же самое
function resetSelection() { list.selected = new Set(); list.allMatching = false; }
$("#reset-filters").addEventListener("click", () => {
  FILTERS.forEach((k) => (list[k] = ""));
  $("#search").value = "";
  applyFilters();
});

// ---------- фильтры и сортировка ----------
const FLAG_LABELS = { errors: "с ошибками заполнения", no_photo: "без фото", gone: "пропали у поставщика" };
const ON_PROM_LABELS = { yes: "есть на Prom", no: "нет на Prom" };

function renderFilterControls() {
  $("#f-group").value = list.group;
  $("#f-presence").value = list.presence;
  $("#f-on_prom").value = list.on_prom;
  $("#f-sort").value = list.sort;
  $("#supplier-filter").value = list.supplier;
  for (const k of Object.keys(FLAG_LABELS)) $(`#f-${k}`).checked = !!list[k];
  // активные фильтры — чипы с крестиком
  const active = [];
  if (list.group) active.push(["group", `группа: ${list.group === "-" ? "без группы" : list.group}`]);
  if (list.presence) active.push(["presence", META.presence[list.presence] || list.presence]);
  if (list.on_prom) active.push(["on_prom", ON_PROM_LABELS[list.on_prom]]);
  for (const [k, label] of Object.entries(FLAG_LABELS)) if (list[k]) active.push([k, label]);
  $("#active-filters").innerHTML = active.map(([k, label]) =>
    `<button class="chip active" data-clear="${k}" title="Убрать фильтр">${esc(label)} ×</button>`).join("");
  const more = Object.keys(FLAG_LABELS).filter((k) => list[k]).length + (list.on_prom ? 1 : 0);
  $("#more-filters-label").textContent = more ? `Ещё фильтры (${more}) ▾` : "Ещё фильтры ▾";
}
$("#active-filters").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-clear]");
  if (!chip) return;
  list[chip.dataset.clear] = "";
  applyFilters();
});
$("#f-group").addEventListener("change", (e) => { list.group = e.target.value; applyFilters(); });
$("#f-presence").addEventListener("change", (e) => { list.presence = e.target.value; applyFilters(); });
$("#f-on_prom").addEventListener("change", (e) => { list.on_prom = e.target.value; applyFilters(); });
$("#f-sort").addEventListener("change", (e) => { list.sort = e.target.value; list.items = []; syncUrl(); loadProducts(); });
for (const k of Object.keys(FLAG_LABELS)) {
  $(`#f-${k}`).addEventListener("change", (e) => { list[k] = e.target.checked ? "1" : ""; applyFilters(); });
}

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { list.q = e.target.value.trim(); applyFilters(); }, 250);
});

$("#more").addEventListener("click", () => loadProducts(true));
$("#supplier-filter").addEventListener("change", (e) => { list.supplier = e.target.value; applyFilters(); });

$$("#bulk [data-action]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
  const sel = selection();
  const action = b.dataset.action;
  try {
    if (action === "send") {
      const res = await api("/api/sync", { method: "POST", json: sel });
      if (res.accepted) toast(`В очереди на Prom: ${res.accepted}`, "ok");
      if (res.rejected.length) {
        const first = res.rejected[0];
        toast(`Не отправлено ${res.rejected.length}: «${first.name || "без названия"}» — ${first.reasons.join("; ")}`, "error");
      }
      loadJobs();
    } else if (action === "ai") {
      openAiModal(sel);
      return;
    } else if (action === "prices") {
      openPricesModal(sel);
      return;
    } else if (action === "edit") {
      openEditModal(sel);
      return;
    } else if (action === "synced") {
      const res = await api("/api/products/status", { method: "POST", json: { ...sel, status: "synced" } });
      toast(res.changed ? `Снова «На Prom»: ${res.changed}` : "Эти товары ещё не выгружались на Prom", res.changed ? "ok" : "error");
    } else if (action === "delete") {
      openDeleteModal(sel);
      return;
    } else {
      await api("/api/products/status", { method: "POST", json: { ...sel, status: action } });
    }
    loadProducts();
  } catch (err) {
    toast(err.message, "error");
  }
})));

// ---------- быстрое создание из фото ----------

async function quickCreate(files) {
  files = files.filter(isImage);
  if (!files.length) return;
  let done = 0;
  for (const file of files) {
    const name = file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim();
    try {
      const p = await api("/api/products", { method: "POST", json: { name } });
      const body = new FormData();
      body.append("files", file);
      await api(`/api/products/${p.id}/images`, { method: "POST", body });
      done++;
    } catch (err) {
      toast(`${file.name}: ${err.message}`, "error");
    }
  }
  if (done) toast(`Создано черновиков: ${done}`, "ok");
  loadProducts();
}

onPageFileDrop(quickCreate, { accept: isImage, text: "Отпустите — на каждое фото создастся товар",
                              rejectText: "это не фото. Excel и прайсы загружайте через «Импорт файла»" });
$("#quick-drop").addEventListener("click", async () => quickCreate(await pickFiles({ accept: "image/*" })));

// ---------- очередь отправки ----------

const JOB_STATUS = {
  pending: ["sending", "В очереди"],
  waiting: ["sending", "Prom обрабатывает"],
  done: ["synced", "Готово"],
  failed: ["error", "Ошибка"],
  retried: ["draft", "Повторена"],
};

let jobsTimer;
async function loadJobs() {
  clearTimeout(jobsTimer);
  let jobs = [];
  try { jobs = await api("/api/sync/jobs"); } catch { /* повторим позже */ }
  const active = jobs.some((j) => j.status === "pending" || j.status === "waiting");
  let photos = null;
  try { photos = await api("/api/sync/photos"); } catch { /* не критично */ }
  $("#jobs-hint").textContent = [
    photos && photos.active ? "📷 фото с компьютера сейчас доступны Prom через временный адрес" : "",
    photos && photos.error ? `📷 ${photos.error}` : "",
    active ? "обновляется автоматически" : "",
  ].filter(Boolean).join(" · ");
  if (jobs.length) {
    $("#jobs").innerHTML = jobs.slice(0, 6).map((j) => {
      const [cls, label] = JOB_STATUS[j.status] || ["draft", j.status];
      let msg = j.last_error || "";
      if (j.status === "done" && j.result) msg = summarizeResult(j.result);
      if (j.status === "pending" && j.attempts) msg = `попытка ${j.attempts + 1}: ${j.last_error}`;
      return `<div class="job">
        <span class="badge ${cls}">${label}</span>
        <span>#${j.id} · товаров: ${j.count}</span>
        <span class="msg" title="${esc(msg)}">${esc(msg)}</span>
        <span class="muted">${esc(formatDate(j.updated_at))}</span>
        <a class="btn small" href="/api/sync/jobs/${j.id}/file" title="Файл, который ушёл на Prom: можно загрузить в кабинете вручную">Скачать файл</a>
        ${j.status === "failed" ? `<button class="btn small" data-retry="${j.id}">Повторить</button>` : ""}
      </div>`;
    }).join("");
    $$("#jobs [data-retry]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
      try {
        const res = await api(`/api/sync/jobs/${b.dataset.retry}/retry`, { method: "POST" });
        toast(`Повторно в очереди: ${res.accepted}`, "ok");
      } catch (err) {
        toast(err.message, "error");
      }
      loadJobs();
      loadProducts();
    })));
  }
  if (active) {
    jobsTimer = setTimeout(() => { loadJobs(); refreshList(); }, 3000);
  }
}

function summarizeResult(r) {
  if (r.mode === "quick") {
    const errs = Object.keys(r.errors || {}).length;
    return `быстрое обновление цен и наличия${errs ? `, с ошибками: ${errs}` : ""}`;
  }
  const parts = [];
  for (const [key, label] of [["imported", "импортировано"], ["created", "создано"], ["updated", "обновлено"], ["not_changed", "без изменений"], ["with_errors_count", "с ошибками"]]) {
    if (r[key]) parts.push(`${label}: ${r[key]}`);
  }
  return parts.join(", ") || (r.status ? `статус: ${r.status}` : "");
}

// ---------- массовый ИИ ----------

const AI_BULK = ["translate_ua", "improve", "keywords", "name", "shorten", "custom"];
let aiSel = { ids: [] };
let aiRate = 8;

function openAiModal(sel) {
  if (!META.ai || !META.ai.enabled) {
    toast("Сначала подключите ИИ в «Настройках» (бесплатно через Google Gemini)", "error");
    return;
  }
  aiSel = sel;
  $("#ai-count").textContent = selectionCount();
  $("#ai-action").innerHTML = AI_BULK.map((k) => `<option value="${k}">${esc(META.ai.actions[k])}</option>`).join("");
  updateAiModal();
  $("#ai-modal").classList.remove("hidden");
}

function updateAiModal() {
  const action = $("#ai-action").value;
  $("#ai-instr-wrap").classList.toggle("hidden", action !== "custom");
  $("#ai-empty-wrap").classList.toggle("hidden", !["translate_ua", "keywords", "improve"].includes(action));
  const minutes = Math.ceil(selectionCount() / aiRate);
  $("#ai-eta").textContent = `Темп: до ${aiRate} товаров в минуту (лимит ИИ) — примерно ${minutes} мин. Можно закрыть страницу, работа продолжится.`;
}

$("#ai-action").addEventListener("change", updateAiModal);
$("#ai-cancel-modal").addEventListener("click", () => $("#ai-modal").classList.add("hidden"));
$("#ai-start").addEventListener("click", async () => {
  try {
    await api("/api/ai/bulk", { method: "POST", json: {
      ...aiSel, action: $("#ai-action").value, instruction: $("#ai-instr").value, only_empty: $("#ai-only-empty").checked,
    } });
    $("#ai-modal").classList.add("hidden");
    toast("Задание для ИИ запущено", "ok");
    loadAiJobs();
  } catch (err) {
    toast(err.message, "error");
  }
});

const AI_JOB_STATUS = {
  running: ["sending", "Идёт"], paused: ["draft", "Пауза"], done: ["synced", "Готово"],
  cancelled: ["draft", "Отменено"], reverted: ["draft", "Откачено"],
};

let aiTimer;
async function loadAiJobs() {
  clearTimeout(aiTimer);
  let data;
  try { data = await api("/api/ai/bulk"); } catch { return; }
  aiRate = data.rate;
  $("#ai-jobs-panel").classList.toggle("hidden", !data.items.length);
  $("#ai-jobs").innerHTML = data.items.map((j) => {
    const [cls, label] = AI_JOB_STATUS[j.status] || ["draft", j.status];
    const c = j.counts;
    const doneCount = (c.done || 0) + (c.skipped || 0) + (c.error || 0);
    const stats = [`${doneCount} из ${j.total}`, c.done ? `изменено ${c.done}` : "", c.skipped ? `пропущено ${c.skipped}` : "",
      c.error ? `ошибок ${c.error}` : ""].filter(Boolean).join(" · ");
    const active = j.status === "running" || j.status === "paused";
    return `<div class="job">
      <span class="badge ${cls}">${label}</span>
      <span>${esc(j.label)}${j.instruction ? `: «${esc(j.instruction.slice(0, 60))}»` : ""}</span>
      <span class="msg">${esc(stats)}${j.message ? ` · ${esc(j.message)}` : ""}${j.errors.length ? ` · ${esc(j.errors[0].message)}` : ""}</span>
      ${j.status === "running" ? `<button class="btn small" data-ai-job="${j.id}" data-st="paused">Пауза</button>` : ""}
      ${j.status === "paused" ? `<button class="btn small" data-ai-job="${j.id}" data-st="running">Продолжить</button>` : ""}
      ${active ? `<button class="btn small" data-ai-job="${j.id}" data-st="cancelled">Отменить</button>` : ""}
      ${c.done && j.status !== "reverted" ? `<button class="btn small danger" data-ai-revert="${j.id}">Откатить</button>` : ""}
    </div>`;
  }).join("");
  $$("[data-ai-job]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/ai/bulk/${b.dataset.aiJob}/status`, { method: "POST", json: { status: b.dataset.st } }); }
    catch (err) { toast(err.message, "error"); }
    loadAiJobs();
  }));
  $$("[data-ai-revert]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
    if (!confirm("Вернуть прежние тексты у всех товаров этого задания?")) return;
    try {
      const r = await api(`/api/ai/bulk/${b.dataset.aiRevert}/revert`, { method: "POST" });
      toast(`Откачено: ${r.restored}`, "ok");
    } catch (err) { toast(err.message, "error"); }  // раньше ошибка отката пропадала молча
    loadAiJobs();
    loadProducts();
  })));
  if (data.items.some((j) => j.status === "running")) {
    aiTimer = setTimeout(() => { loadAiJobs(); refreshList(); }, 4000);
  }
}

// ---------- каталог с Prom ----------

let catalogTimer;
async function showCatalogState(state) {
  clearTimeout(catalogTimer);
  const box = $("#catalog-state");
  if (!state.running && !state.finished_at) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  if (state.running) {
    box.innerHTML = `<span class="badge sending">Загружаю каталог с Prom</span> обработано товаров: ${state.seen || 0}…`;
    catalogTimer = setTimeout(async () => {
      const next = await api("/api/prom/catalog");
      if (!next.running) { refreshList(); loadMeta(); }
      showCatalogState(next);
    }, 1500);
  } else if (state.error) {
    box.innerHTML = `<span class="badge error">Каталог не загружен</span> ${esc(state.error)}`;
  } else {
    const warn = state.no_external_id
      ? `<div class="err-text" style="color:var(--warn)">У ${state.no_external_id} товаров в кабинете Prom нет «внешнего ID» —
         без него Prom при отправке может создать копию товара. <button class="btn small" id="ext-fix">Как исправить</button></div>` : "";
    box.innerHTML = `<span class="badge synced">Каталог загружен</span> ${esc(formatDate(state.finished_at))}:
      новых ${state.created}, обновлено ${state.updated}${state.skipped ? `, пропущено (удалённые) ${state.skipped}` : ""}${
        state.kept_local ? `, оставлены ваши неотправленные правки: ${state.kept_local}` : ""}${warn}${
        state.missing_on_prom ? `<div class="err-text">${state.missing_on_prom} товаров считались выгруженными, но на Prom их нет —
          они в фильтре «Ошибка»: отправьте заново или удалите из программы.</div>` : ""}`;
    const fix = $("#ext-fix");
    if (fix) fix.onclick = () => busy(fix, openExtIds);
  }
}

// «Ідентифікатор_товару» в кабинет Prom: API Prom его не меняет, поэтому — файл для импорта в кабинете
async function openExtIds() {
  let info;
  try { info = await api("/api/prom/external-ids"); } catch (err) { toast(err.message, "error"); return; }
  let modal = $("#ext-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "ext-modal";
    modal.className = "modal hidden";
    document.body.appendChild(modal);
  }
  const sample = info.sample.map((p) => `<li>${esc(p.name)} — ID <b>${esc(p.external_id)}</b>
    · <a href="${esc(p.prom_url)}" target="_blank" rel="noopener">открыть на Prom ↗</a></li>`).join("");
  modal.innerHTML = `<div class="modal-box preview-box">
    <h2>Прописать ID товарам на Prom</h2>
    <p>Программа узнаёт товары на Prom по полю «Ідентифікатор_товару». У части ваших товаров в кабинете оно пустое,
      и при отправке Prom может создать копию. Программа уже дала этим товарам ID — их код, а если кода нет или он
      повторяется, <code>PROM-номер</code>. Осталось один раз записать эти ID в кабинет Prom: сделать это можно только
      импортом файла (API Prom такое поле не меняет).</p>
    ${info.flagged ? "" : `<p class="small muted">Каталог загружали прошлой версией программы, поэтому в файле все
      ${info.count} товаров с Prom. У товаров, где ID уже есть, в файле он тот же — для них ничего не изменится.</p>`}
    <ol class="ext-steps">
      <li><b>Сначала проба на 2 товарах.</b> <a class="btn small" href="/api/prom/external-ids.xlsx?limit=2">⬇ Пробный файл</a>
        <ul class="small">${sample}</ul></li>
      <li>Кабинет Prom → «Товари та послуги» → «Імпорт» → из файла → выберите скачанный файл. Если Prom спросит, что
        обновлять, ничего лишнего не отмечайте: в файле только номер товара на Prom и ID.</li>
      <li>Откройте эти 2 товара в кабинете: название, цена и фото не изменились, копий не появилось, в поле
        «Ідентифікатор товару» стоит ID из списка выше.</li>
      <li>Всё хорошо — <a class="btn small" href="/api/prom/external-ids.xlsx">⬇ Файл для всех (${info.count})</a> и так же
        загрузите в кабинете. Что-то не так — не загружайте и напишите в «🆘 Не получается?».</li>
    </ol>
    <div class="toolbar" style="margin:12px 0 0;justify-content:flex-end">
      <button class="btn" data-a="close">Закрыть</button>
      <button class="btn primary" data-a="done">Готово, файл загружен</button>
    </div></div>`;
  modal.querySelector('[data-a="close"]').onclick = () => modal.classList.add("hidden");
  modal.querySelector('[data-a="done"]').onclick = (e) => busy(e.target, async () => {
    try {
      const r = await api("/api/prom/external-ids/done", { method: "POST" });
      modal.classList.add("hidden");
      toast("Готово. При следующей загрузке каталога программа перепроверит ID", "ok");
      showCatalogState(r.state);
    } catch (err) { toast(err.message, "error"); }
  });
  modal.classList.remove("hidden");
}

$("#prom-catalog").addEventListener("click", async () => {
  if (!confirm("Загрузить в программу все товары из вашего кабинета Prom? Уже существующие в программе товары с тем же артикулом обновятся.")) return;
  try {
    showCatalogState(await api("/api/prom/catalog", { method: "POST" }));
  } catch (err) {
    toast(err.message, "error");
  }
});

// ---------- первые шаги ----------

const FIRST_STEPS_KEY = "promloader-first-steps-hidden";

function renderFirstSteps() {
  const f = META.first_steps;
  let hidden = false;
  try { hidden = localStorage.getItem(FIRST_STEPS_KEY) === "1"; } catch {}
  const photo = {
    r2: [true, "Фото хранятся в Cloudflare R2 — Prom забирает их в любое время."],
    site: [true, "Фото отдаются по постоянному адресу сайта."],
    tunnel: [true, "Фото с компьютера Prom забирает через временный адрес — пока компьютер включён. " +
      "Надёжнее — <a href=\"/settings\">хранилище R2</a> (бесплатно)."],
    off: [false, "Prom не сможет забрать фото с компьютера: включите временный адрес или R2 в <a href=\"/settings\">Настройках</a>."],
  }[f.photos] || [false, ""];
  const steps = [
    [f.token, "Подключить Prom", f.token ? "API-токен указан." :
      "Вставьте API-токен из кабинета Prom в <a href=\"/settings\">Настройках</a> — без него товары не отправить."],
    [photo[0], "Фото для Prom", photo[1]],
    [f.products > 0, "Добавить товары", f.products > 0
      ? `Товаров в программе: ${f.products}${f.suppliers ? `, поставщиков: ${f.suppliers}` : ""}.`
      : `Подключите <a href="/suppliers">поставщика</a> (прайс по ссылке обновляется сам), загрузите
         <a href="/import">файл Excel</a>, создайте <a href="/product">товар вручную</a> или нажмите «⬇ Каталог с Prom».`],
    [f.sent, "Отправить первый товар на Prom", f.sent ? "Товары уже есть на Prom." :
      "Отметьте товар в списке ниже → «Отправить на Prom». Через 1–3 минуты он появится в кабинете Prom."],
  ];
  const done = steps.filter(([ok]) => ok).length;
  $("#first-steps").classList.toggle("hidden", hidden || done === steps.length);
  $("#first-steps-count").textContent = `сделано ${done} из ${steps.length}`;
  $("#first-steps-list").innerHTML = steps.map(([ok, title, text]) =>
    `<li class="${ok ? "done" : ""}"><span class="mark">${ok ? "✓" : ""}</span><div><b>${title}</b>
      <div class="small muted">${text}</div></div></li>`).join("");
}
$("#first-steps-hide").onclick = () => {
  try { localStorage.setItem(FIRST_STEPS_KEY, "1"); } catch {}
  $("#first-steps").classList.add("hidden");
};

(async () => {
  await loadMeta();
  renderFirstSteps();
  api("/api/prom/catalog").then(showCatalogState).catch(() => {});
  loadAiJobs();
  $("#supplier-filter").innerHTML = `<option value="">Все поставщики</option><option value="none">Без поставщика</option>` +
    META.suppliers.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  $("#supplier-filter").classList.toggle("hidden", !META.suppliers.length);
  $("#f-group").innerHTML = `<option value="">Все группы</option><option value="-">Без группы</option>` +
    (META.groups || []).map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("");
  $("#f-presence").innerHTML = `<option value="">Любое наличие</option>` +
    Object.entries(META.presence).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#e-presence").innerHTML = `<option value="">не менять</option>` +
    Object.entries(META.presence).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#e-groups").innerHTML = (META.groups || []).map((g) => `<option value="${esc(g)}">`).join("");
  $("#search").value = list.q;
  renderFilterControls();
  syncUrl();
  await loadProducts();
  loadJobs();
})();

// ---------- 💲 цены ----------
let pricesSel = { ids: [] };
async function openPricesModal(sel) {
  pricesSel = sel;
  $("#prices-count").textContent = selectionCount();
  $("#prices-modal").classList.remove("hidden");
  $("#prices-currency").value = "";
  try {
    const sum = await api("/api/products/currencies", { method: "POST", json: sel });
    const parts = Object.entries(sum).map(([c, n]) => `${c === "UAH" ? "в гривнах" : "в " + c} — ${n}`);
    $("#prices-currency-hint").textContent = parts.length ? `Сейчас у выбранных товаров цена ${parts.join(", ")}. ` +
      (sum.UAH ? "Если на самом деле это доллары — выберите «доллары $»." : "") : "";
  } catch { $("#prices-currency-hint").textContent = ""; }
  try {
    const r = (await api("/api/rates")).current.USD;
    $("#prices-rate").innerHTML = r && r.rate
      ? `Курс сейчас: 1 $ = ${r.rate.toFixed(2)} грн. Курс и правила наценки — на странице <a href="/pricing">«Наценка»</a>.`
      : `Курс не настроен — <a href="/pricing">настройте на странице «Наценка»</a>.`;
  } catch { $("#prices-rate").textContent = ""; }
}
$("#prices-cancel").onclick = () => $("#prices-modal").classList.add("hidden");
$("#prices-apply").onclick = async () => {
  const action = $("input[name=price-action]:checked").value;
  const value = Number(String($("#prices-percent").value).replace(",", ".")) || 0;
  const currency = action === "as_cost" ? $("#prices-currency").value : "";
  $("#prices-apply").disabled = true;
  try {
    const res = await api("/api/products/prices", { method: "POST", json: { ...pricesSel, action, value, currency } });
    $("#prices-modal").classList.add("hidden");
    toast(`Цен изменено: ${res.changed}` + (res.queued ? `, отправлено на Prom: ${res.queued}` : ""), "ok");
    res.warnings.forEach((w) => toast(w, "error"));
    loadProducts();
  } catch (err) { toast(err.message, "error"); }
  $("#prices-apply").disabled = false;
};

// ---------- удаление ----------
let deleteSel = { ids: [] };
async function openDeleteModal(sel) {
  deleteSel = sel;
  let info;
  try { info = await api("/api/products/delete/check", { method: "POST", json: sel }); }
  catch (err) { toast(err.message, "error"); return; }
  if (!info.on_prom) {
    if (!confirm(`Удалить товаров: ${info.total}? На Prom их нет. Это нельзя отменить.`)) return;
    return doDelete(false);
  }
  $("#delete-count").textContent = info.total;
  $("#delete-on-prom").textContent = info.on_prom;
  $("#delete-keep-count").textContent = info.on_prom;
  $("#delete-sending").textContent = info.sending
    ? `${info.sending} сейчас отправляются на Prom — их удалить не получится, пока отправка не закончится.` : "";
  $("input[name=delete-mode][value=prom]").checked = true;
  $("#delete-modal").classList.remove("hidden");
}
async function doDelete(fromProm) {
  try {
    const res = await api("/api/products/delete", { method: "POST", json: { ...deleteSel, prom: fromProm } });
    const parts = [];
    if (res.deleted) parts.push(`удалено: ${res.deleted}`);
    if (res.deleting) parts.push(`удаляются с Prom: ${res.deleting} — уйдут из списка, когда Prom подтвердит`);
    if (res.kept_on_prom) parts.push(`на Prom остались: ${res.kept_on_prom}`);
    if (parts.length) toast(parts.join(" · "), "ok");
    if (res.rejected.length) toast(`Не удалено ${res.rejected.length}: ${res.rejected[0].reason}`, "error");
    resetSelection();
    loadProducts();
  } catch (err) { toast(err.message, "error"); }
}
$("#delete-cancel").onclick = () => $("#delete-modal").classList.add("hidden");
$("#delete-apply").onclick = () => busy($("#delete-apply"), async () => {
  await doDelete($("input[name=delete-mode]:checked").value === "prom");
  $("#delete-modal").classList.add("hidden");
});

// ---------- ✏ изменить поля у многих товаров ----------
let editSel = { ids: [] };
function openEditModal(sel) {
  editSel = sel;
  $("#edit-count").textContent = selectionCount();
  ["#e-group", "#e-quantity", "#e-vendor", "#e-keywords"].forEach((s) => ($(s).value = ""));
  $("#e-presence").value = "";
  $("#edit-modal").classList.remove("hidden");
  $("#e-group").focus();
}
$("#edit-cancel").onclick = () => $("#edit-modal").classList.add("hidden");
$("#edit-apply").onclick = async () => {
  const fields = { group_name: $("#e-group").value.trim(), presence: $("#e-presence").value,
    quantity: $("#e-quantity").value.trim(), vendor: $("#e-vendor").value.trim(), keywords: $("#e-keywords").value.trim() };
  if (!Object.values(fields).some((v) => v)) { toast("Заполните то, что нужно изменить", "error"); return; }
  $("#edit-apply").disabled = true;
  try {
    const res = await api("/api/products/bulk-edit", { method: "POST", json: { ...editSel, fields } });
    $("#edit-modal").classList.add("hidden");
    toast(`Изменено товаров: ${res.changed}`, "ok");
    if (res.errors.length) toast(`Не изменено ${res.errors.length}: «${res.errors[0].name}» — ${res.errors[0].error}`, "error");
    if (fields.group_name) await loadMeta().then(() => {
      $("#f-group").innerHTML = `<option value="">Все группы</option><option value="-">Без группы</option>` +
        (META.groups || []).map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("");
      renderFilterControls();
    });
    loadProducts();
  } catch (err) { toast(err.message, "error"); }
  $("#edit-apply").disabled = false;
};

// «Ещё фильтры» закрывается кликом мимо окна
document.addEventListener("click", (e) => {
  const box = $(".more-filters");
  if (box && box.open && !box.contains(e.target)) box.open = false;
});
