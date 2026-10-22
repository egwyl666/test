// Список товаров, массовые действия и очередь отправки.

const PAGE = 200;
// фильтры списка — те же имена, что у /api/products; хранятся и в адресе страницы (?group=…&sort=…)
const FILTERS = ["status", "q", "supplier", "group", "presence", "on_prom", "no_photo", "gone", "errors", "prom_category", "cat_mismatch",
                 "readiness", "profit", "duplicates", "conflict"];
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
  const limit = append ? PAGE : Math.max(PAGE, list.items.length, list.restoreCount || 0);
  list.restoreCount = 0;
  const params = new URLSearchParams({ ...filterParams(), sort: list.sort, limit, offset });
  // номер запроса: ответ на старый фильтр, пришедший позже нового, не должен перерисовать список
  const seq = (list.seq = (list.seq || 0) + 1);
  if (!append && !list.items.length) {
    // первая загрузка или новый фильтр — заготовка вместо пустоты и «Товаров пока нет», пока ответ в пути
    $("#rows").innerHTML = skeletonRows($$("table.list thead th").length || 9);
    $("#empty").classList.add("hidden");
    $("#nothing-found").classList.add("hidden");
  }
  let data;
  try { data = await api(`/api/products?${params}`); } catch (err) {
    $$("#rows tr.skeleton").forEach((el) => el.remove());  // не оставлять «загрузку» навсегда
    throw err;
  }
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
  if (!append) {
    loadReadiness().catch(() => {});
    loadAlerts().catch(() => {});
  }
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
    if (list.status !== "draft") list.readiness = "";  // «готовность» — фильтр только по черновикам
    applyFilters();
  }));
}

// Предупреждения над списком: конфликты с кабинетом Prom (поменяли и там, и в программе) и возможные дубли
async function loadAlerts() {
  const r = await api("/api/products/alerts");
  const chip = (key, label, n, cls) => `<button class="chip ${list[key] ? "active" : ""} ${cls}" data-alert="${key}">${esc(I18N.tr(label))}<b>${n}</b></button>`;
  const html = [r.conflicts ? chip("conflict", "⚡ Конфликты с кабинетом Prom", r.conflicts, "alert-conflict") : "",
                r.duplicates ? chip("duplicates", "🔍 Возможные дубли", r.duplicates, "") : ""].join("");
  // в режиме «дубли»: одной кнопкой оставить в каждой группе выгодный, остальные — в черновики
  const keep = list.duplicates && r.duplicates
    ? `<button class="btn small primary" id="dup-keep-best" data-perm="catalog">${esc(I18N.tr("Оставить выгодный (скрыть дубли в черновик)"))}</button>` : "";
  $("#list-alerts").innerHTML = html + keep;
  $("#list-alerts").classList.toggle("hidden", !html && !list.conflict && !list.duplicates);
}

async function keepBestDuplicates(btn) {
  // без вопроса: скрытые дубли — это черновики, их можно вернуть «↩ Отменить» в уведомлении
  try {
    const r = await api("/api/products/duplicates/keep-best", { method: "POST" });
    let text = I18N.tr(`Скрыто дублей: ${r.hidden} (групп: ${r.groups})`);
    if (r.on_prom) text += ". " + I18N.tr(`${r.on_prom} из них уже есть на Prom — там они остались; если не нужны, удалите их кнопкой «Удалить» → «и на Prom»`);
    undoToast(text, r.undo, () => applyFilters());
    list.duplicates = "";
    applyFilters();
  } catch (err) { toast(err.message, "error"); }
}

$("#list-alerts").addEventListener("click", (e) => {
  const keep = e.target.closest("#dup-keep-best");
  if (keep) { busy(keep, () => keepBestDuplicates(keep)); return; }
  const b = e.target.closest("[data-alert]");
  if (!b) return;
  list[b.dataset.alert] = list[b.dataset.alert] ? "" : "1";
  applyFilters();
});

// Полоска над черновиками: сколько готовы для Prom (кнопка «В «Готов к отправке»») и чего не хватает остальным.
// Учитывает остальные фильтры списка (поставщик, поиск, группа). Видна, только когда выбраны «Черновики».
const READINESS_ORDER = ["missing_ua", "missing_keywords", "missing_price_or_photo", "needs_manual"];
let readinessSeq = 0;
async function loadReadiness() {
  const bar = $("#readiness");
  if (list.status !== "draft") { bar.classList.add("hidden"); return; }
  const seq = ++readinessSeq;
  const r = await api(`/api/products/readiness?${new URLSearchParams(filterParams())}`);
  if (seq !== readinessSeq) return;
  const pill = (key, n) => `<button class="chip ${list.readiness === key ? "active" : ""}" data-readiness="${key}">${esc(I18N.tr(r.labels[key]))}<b>${n}</b></button>`;
  bar.innerHTML = `<span class="small muted">Черновики:</span> ${pill("ready_to_promote", r.ready_to_promote)}
    ${READINESS_ORDER.filter((k) => r[k]).map((k) => pill(k, r[k])).join(" ")}
    <span class="spacer"></span>
    ${list.readiness === "needs_manual" && r.needs_manual
      ? `<button class="btn small" id="readiness-photo" data-perm="ai" data-n="${r.needs_manual}">📷 Заполнить по фото (${r.needs_manual})</button>`
      : aiTarget(r) ? `<button class="btn small" id="readiness-ai" data-perm="ai" data-n="${aiTarget(r)}">✨ Заполнить через ИИ (${aiTarget(r)})</button>` : ""}
    ${r.ready_to_promote ? `<button class="btn small primary" id="promote-ready">В «Готов к отправке» (${r.ready_to_promote})</button>` : ""}`;
  bar.classList.toggle("hidden", !r.drafts);
}
// «✨ Заполнить через ИИ»: выбран фильтр «без украинского текста» или «без ключевых слов» — ровно эти черновики,
// иначе — все черновики, которым не хватает перевода или ключевых слов (вместе с остальными фильтрами списка)
function aiTarget(r) {
  if (["missing_ua", "missing_keywords"].includes(list.readiness)) return r[list.readiness];
  return r.missing_ai || 0;
}
$("#readiness").addEventListener("click", (e) => {
  const photo = e.target.closest("#readiness-photo");
  if (photo) {
    // «мало данных»: ИИ описывает товар по его фото (по одному товару на запрос)
    const base = Object.fromEntries(Object.entries(filterParams()).filter(([k]) => k !== "readiness"));
    openAiModal({ filter: { ...base, status: "draft", readiness: "needs_manual" } }, { count: Number(photo.dataset.n), action: "from_photo" });
    return;
  }
  const ai = e.target.closest("#readiness-ai");
  if (ai) {
    const base = Object.fromEntries(Object.entries(filterParams()).filter(([k]) => k !== "readiness"));
    const readiness = ["missing_ua", "missing_keywords"].includes(list.readiness) ? list.readiness : "missing_ai";
    openAiModal({ filter: { ...base, status: "draft", readiness } }, { count: Number(ai.dataset.n), action: "prepare_prom" });
    return;
  }
  const pill = e.target.closest("[data-readiness]");
  if (pill) {
    list.readiness = list.readiness === pill.dataset.readiness ? "" : pill.dataset.readiness;
    applyFilters();
    return;
  }
  const btn = e.target.closest("#promote-ready");
  if (!btn) return;
  busy(btn, async () => {
    try {
      const filter = Object.fromEntries(Object.entries(filterParams()).filter(([k]) => !["status", "readiness"].includes(k)));
      const r = await api("/api/products/promote-ready", { method: "POST", json: { filter } });
      undoToast(I18N.tr(`В «Готов к отправке»: ${r.moved}`), r.undo, () => applyFilters());
      if (list.readiness === "ready_to_promote") list.readiness = "";
      applyFilters();
    } catch (err) { toast(err.message, "error"); }
  });
});

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
    notes.push(`<span class="${up ? "price-up" : "price-down"}" title="${esc(formatDate(ch.at))} · ${esc(I18N.tr(ch.source))}">было ${esc(formatPrice(ch.old, ch.old_currency || p.currency))}</span>`);
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
  if (!list.items.length && filtered) $("#nothing-text").textContent = I18N.tr(nothingText());
  patchChildren(tbody, list.items, (p) => p.id, (p) => {
    const problems = p.check.errors.length ? `<div class="err-text">${esc(p.check.errors.map(I18N.tr).join(" · "))}</div>` : "";
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
        ${p.name ? `<a class="name" href="/product?id=${p.id}" translate="no">${esc(p.name)}</a>`
          : `<a class="name" href="/product?id=${p.id}"><span class="muted">Без названия</span></a>`}
        <div class="small muted"><span translate="no">${esc(p.external_id)}</span> · <span>фото: ${p.image_count}</span>${p.supplier_name ? ` · <span translate="no">${esc(p.supplier_name)}</span>` : ""}${p.locked_fields.length ? ` · <span title="Поля, изменённые вручную: ${esc(p.locked_fields.join(", "))}">🔒 ${p.locked_fields.length}</span>` : ""}</div>
        <div class="show-sm" style="margin-top:4px">${statusBadge(p.status)}</div>
        ${p.dup ? `<div class="small dup-note">${p.dup.best ? `<b class="dup-best">★ ${esc(I18N.tr("выгодный"))}</b> · ` : ""}${esc(I18N.tr("дубль по"))} ${esc(I18N.tr(p.dup.kind))}: <span translate="no">${esc(p.dup.key.slice(0, 40))}</span>${p.margin_uah !== null && p.margin_uah !== undefined ? ` · ${esc(I18N.tr("наценка"))} ${esc(formatPrice(p.margin_uah, "UAH"))}` : ""}</div>` : ""}
        ${p.dup_of ? `<div class="small muted">👥 ${esc(I18N.tr("скрыт как дубль"))}</div>` : ""}
        ${p.prom_conflict ? `<div class="small conflict-note">⚡ ${esc(I18N.tr("конфликт с кабинетом Prom — откройте товар"))}</div>` : ""}
        ${problems}${promError}${deleting}
      </td>
      <td class="hide-sm" translate="no">${esc(p.group_name)}</td>
      <td class="hide-sm cost">${costCell(p)}</td>
      <td class="price">${priceCell(p)}</td>
      <td class="hide-sm small">${esc(META.presence[p.presence] || "")}${p.quantity !== null ? ` · ${p.quantity}` : ""}</td>
      <td class="hide-sm">${statusBadge(p.status)}${p.synced_at && p.status !== "synced" ? `<span class="on-prom-note" title="Товар уже есть на Prom; изменения уйдут при следующей отправке">● есть на Prom</span>` : ""}</td>
      <td class="hide-sm small muted">${esc(formatDate(p.updated_at))}</td>
    </tr>`;
  });
  updateBulk();
}

// Обработчики — один раз на всю таблицу: строки перерисовываются по одной (patchChildren), а клик ловится здесь
$("#rows").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr.item");
  if (!tr || e.target.classList.contains("sel") || e.target.closest("a")) return;  // ссылка-название откроется сама
  const id = Number(tr.dataset.id);
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
$("#rows").addEventListener("change", (e) => {
  if (!e.target.classList.contains("sel")) return;
  const id = Number(e.target.closest("tr.item").dataset.id);
  if (e.target.checked) list.selected.add(id); else list.selected.delete(id);
  list.allMatching = false;
  updateBulk();
});

// Пусто по фильтру-«проверке» — это хорошая новость, а не «ничего не найдено»
function nothingText() {
  const only = FILTERS.filter((k) => list[k] !== "");
  if (only.length === 1) {
    const good = { duplicates: "Возможных дублей нет ✓", conflict: "Конфликтов с кабинетом Prom нет ✓",
      errors: "Товаров с ошибками заполнения нет ✓", no_photo: "Все товары с фото ✓", gone: "Пропавших у поставщика нет ✓",
      cat_mismatch: "Категории на Prom совпадают с нужными ✓" }[only[0]];
    if (good) return good;
    if (only[0] === "status" && list.status === "error") return "Товаров с ошибкой отправки нет ✓";
  }
  return list.q ? "Ничего не найдено — проверьте написание или поищите по артикулу." : "Ничего не найдено.";
}

function updateBulk() {
  const n = selectionCount();
  const pageAll = list.selected.size > 0 && list.selected.size === list.items.length;
  $("#selected-count").innerHTML = !n ? "" : list.allMatching
    ? `выбраны все ${n} по фильтру · <a href="#" id="select-page">только показанные</a>`
    : `выбрано: ${n}` + (pageAll && list.total > list.items.length
      ? ` · <a href="#" id="select-all-matching">выбрать все ${list.total} по фильтру</a>` : "");
  $$("#bulk [data-action]").forEach((b) => (b.disabled = !n));
  $("#bulk").classList.toggle("has-selection", !!n);
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
const FLAG_LABELS = { errors: "с ошибками заполнения", no_photo: "без фото", gone: "пропали у поставщика",
  cat_mismatch: "Prom поставил не ту категорию" };
const ON_PROM_LABELS = { yes: "есть на Prom", no: "нет на Prom" };

const READINESS_FILTER_LABELS = { ready_to_promote: "черновики: готовы для Prom", missing_ua: "черновики: без украинского текста",
  missing_keywords: "черновики: без ключевых слов", missing_price_or_photo: "черновики: без цены или фото",
  needs_manual: "черновики: мало данных — заполнить вручную" };

function renderFilterControls() {
  $("#f-group").value = list.group;
  $("#f-presence").value = list.presence;
  $("#f-profit").value = list.profit;
  $("#f-on_prom").value = list.on_prom;
  $("#f-prom_category").value = list.prom_category;
  $("#f-sort").value = list.sort;
  $("#supplier-filter").value = list.supplier;
  for (const k of Object.keys(FLAG_LABELS)) $(`#f-${k}`).checked = !!list[k];
  // активные фильтры — чипы с крестиком
  const active = [];
  if (list.group) active.push(["group", `группа: ${list.group === "-" ? "без группы" : list.group}`]);
  if (list.presence) active.push(["presence", META.presence[list.presence] || list.presence]);
  if (list.on_prom) active.push(["on_prom", ON_PROM_LABELS[list.on_prom]]);
  if (list.prom_category) {
    const cat = (META.prom_categories || []).find((c) => String(c.id) === list.prom_category);
    active.push(["prom_category", list.prom_category === "none" ? "без категории Prom"
      : `${I18N.tr("категория Prom:")} ${cat ? cat.caption || "№" + cat.id : "№" + list.prom_category}`]);
  }
  for (const [k, label] of Object.entries(FLAG_LABELS)) if (list[k]) active.push([k, label]);
  if (list.readiness) active.push(["readiness", READINESS_FILTER_LABELS[list.readiness] || list.readiness]);
  if (list.profit) active.push(["profit", $(`#f-profit option[value="${list.profit}"]`)?.textContent || list.profit]);
  if (list.conflict) active.push(["conflict", "конфликты с кабинетом Prom"]);
  if (list.duplicates) active.push(["duplicates", "возможные дубли"]);
  $("#active-filters").innerHTML = active.map(([k, label]) =>
    `<button class="chip active" data-clear="${k}" title="Убрать фильтр"><span>${esc(label)}</span> ×</button>`).join("");
  const more = Object.keys(FLAG_LABELS).filter((k) => list[k]).length + (list.on_prom ? 1 : 0) + (list.prom_category ? 1 : 0);
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
$("#f-profit").addEventListener("change", (e) => {
  list.profit = e.target.value;
  // «выгодные» удобнее смотреть сразу по убыванию выгоды, «невыгодные» — по возрастанию
  if (list.profit && list.sort === "updated") list.sort = list.profit === "high_profit" ? "profit_desc" : "profit_asc";
  applyFilters();
});
$("#f-on_prom").addEventListener("change", (e) => { list.on_prom = e.target.value; applyFilters(); });
$("#f-prom_category").addEventListener("change", (e) => { list.prom_category = e.target.value; applyFilters(); });
$("#f-sort").addEventListener("change", (e) => { list.sort = e.target.value; list.items = []; syncUrl(); loadProducts(); });
for (const k of Object.keys(FLAG_LABELS)) {
  $(`#f-${k}`).addEventListener("change", (e) => { list[k] = e.target.checked ? "1" : ""; applyFilters(); });
}

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { list.q = e.target.value.trim(); applyFilters(); }, 250);
});

// 📷 штрихкод товара камерой → поиск в списке (поиск ищет и по штрихкоду, и по артикулу поставщика)
$("#scan-product").addEventListener("click", () => openScanner({
  title: I18N.tr("Найти товар по штрихкоду"), placeholder: I18N.tr("штрихкод или артикул"),
  hint: I18N.tr("Наведите камеру на штрихкод товара или упаковки."),
  onCode: async (code, close) => {
    close();
    $("#search").value = code;
    list.q = code;
    applyFilters();
  },
}));

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
        toast(`Не отправлено ${res.rejected.length}: «${first.name || "без названия"}» — ${first.reasons.map(I18N.tr).join("; ")}`, "error");
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
      if (res.changed) undoToast(I18N.tr(`Снова «На Prom»: ${res.changed}`), res.undo, () => loadProducts());
      else toast("Эти товары ещё не выгружались на Prom", "error");
    } else if (action === "delete") {
      openDeleteModal(sel);
      return;
    } else {
      const res = await api("/api/products/status", { method: "POST", json: { ...sel, status: action } });
      const done = action === "draft" ? `В черновики: ${res.changed}` : `Готов к отправке: ${res.changed}`;
      undoToast(I18N.tr(done), res.undo, () => loadProducts());
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
  ].filter(Boolean).map(I18N.tr).join(" · ");
  if (jobs.length) {
    patchChildren($("#jobs"), jobs.slice(0, 6), (j) => j.id, (j) => {
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
    });
  }
  if (active) {
    jobsTimer = setTimeout(() => { loadJobs(); refreshList(); }, 3000);
  }
}

$("#jobs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-retry]");
  if (!b) return;
  busy(b, async () => {
    try {
      const res = await api(`/api/sync/jobs/${b.dataset.retry}/retry`, { method: "POST" });
      toast(`Повторно в очереди: ${res.accepted}`, "ok");
    } catch (err) {
      toast(err.message, "error");
    }
    loadJobs();
    loadProducts();
  });
});

function summarizeResult(r) {
  if (r.mode === "quick") {
    const errs = Object.keys(r.errors || {}).length;
    return `быстрое обновление цен и наличия${errs ? `, с ошибками: ${errs}` : ""}`;
  }
  const parts = [];
  for (const [key, label] of [["imported", "импортировано"], ["created", "создано"], ["updated", "обновлено"], ["not_changed", "без изменений"], ["with_errors_count", "с ошибками"]]) {
    if (r[key]) parts.push(`${I18N.tr(label)}: ${r[key]}`);
  }
  return parts.join(", ") || (r.status ? `статус: ${r.status}` : "");
}

// ---------- массовый ИИ ----------

// «Подготовить для Prom» — первым: перевод и ключевые слова одним заданием (и одним запросом на 5 товаров)
const AI_BULK = ["prepare_prom", "from_photo", "match_category", "translate_ua", "translate_ru", "improve", "keywords", "name", "shorten", "custom"];
let aiSel = { ids: [] };
let aiRate = 8;

let aiCount = 0;
const aiLimit = () => Number($("#ai-limit").value) || null;
// opts.count — сколько товаров (запуск из полоски готовности: выборка — фильтр, а не отмеченные строки),
// opts.action — какое действие выбрать сразу
function openAiModal(sel, opts = {}) {
  if (!META.ai || !META.ai.enabled) {
    toast("Сначала подключите ИИ в «Настройках» (бесплатно через Google Gemini)", "error");
    return;
  }
  aiSel = sel;
  aiCount = opts.count ?? selectionCount();
  $("#ai-count").textContent = aiCount;
  $("#ai-action").innerHTML = AI_BULK.map((k) => `<option value="${k}">${esc(META.ai.actions[k] || (META.ai.bulk_only || {})[k] || k)}</option>`).join("");
  if (opts.action) $("#ai-action").value = opts.action;
  // «первые N по выгоде» — пробный прогон на лучших товарах, а не на всех тысячах; для маленькой выборки не нужен
  $("#ai-limit").value = "";
  $("#ai-limit-wrap").classList.toggle("hidden", aiCount <= 20);
  $("#ai-auto-promote").checked = ["prepare_prom", "from_photo"].includes($("#ai-action").value);
  updateAiModal();
  $("#ai-modal").classList.remove("hidden");
}

let aiEstimateSeq = 0;
async function updateAiModal() {
  const action = $("#ai-action").value;
  $("#ai-instr-wrap").classList.toggle("hidden", action !== "custom");
  $("#ai-empty-wrap").classList.toggle("hidden", !["prepare_prom", "from_photo", "match_category", "translate_ua", "translate_ru", "keywords", "improve"].includes(action));
  const count = Math.min(aiCount, aiLimit() || aiCount);
  $("#ai-count").textContent = count;
  const minutes = Math.ceil(count / aiRate);
  $("#ai-promote-wrap").classList.toggle("hidden", !["prepare_prom", "from_photo", "match_category", "translate_ua", "keywords"].includes(action));
  const pace = `Темп: до ${aiRate} товаров в минуту (лимит ИИ) — примерно ${minutes} мин. Можно закрыть страницу, работа продолжится.`;
  $("#ai-eta").textContent = pace;
  $("#ai-cost").innerHTML = `<span class="muted">Считаю, сколько это будет стоить…</span>`;
  const seq = ++aiEstimateSeq;
  let e;
  try {
    e = await api("/api/ai/bulk/estimate", { method: "POST", json: { ...aiSel, action, limit: aiLimit() } });
  } catch (err) {
    if (seq === aiEstimateSeq) $("#ai-cost").textContent = "";
    return;
  }
  if (seq !== aiEstimateSeq) return;  // пока считали, выбрали другое действие
  if (e.batch > 1) {
    // двумя переводимыми кусками: склеенное в одну строку предложение словарь не узнаёт
    $("#ai-eta").innerHTML = `<span>${esc(I18N.tr(`По ${e.batch} товаров в одном запросе — запросов ${e.requests}, примерно ${Math.max(1, e.minutes)} мин.`))}</span> ` +
      `<span>${esc(I18N.tr("Так бесплатного лимита хватает на гораздо большее число товаров. Можно закрыть страницу, работа продолжится."))}</span>`;
  }
  const model = e.model ? aiModelName(e.model) : "модель выберется автоматически";
  let text;
  if (e.free) {
    text = `Модель: <b>${esc(model)}</b>. Ключ Gemini бесплатный — денег не стоит, но займёт время из-за лимита запросов.`;
  } else if (e.total_usd === null) {
    text = `Модель: <b>${esc(model)}</b>. Цена этой модели программе неизвестна — расход будет виден в окне «✨ ИИ» по мере работы.`;
  } else {
    // за один товар: при пачках цена запроса делится на товары пачки
    const share = e.count ? e.total_usd / e.count : e.per_request_usd;
    const per = aiMoney(share, e.total_uah !== null && e.count ? e.total_uah / e.count : e.per_request_uah, { withUsd: false });
    text = `Модель: <b>${esc(model)}</b>. Обойдётся не дороже <b>${aiMoney(e.total_usd, e.total_uah)}</b>
      (${per} за товар — ${e.measured ? "средняя цена ваших последних запросов" : "по типичному размеру карточки, точнее станет после первых запросов"};
      товары, которые пропустятся, не оплачиваются). Сменить модель — значок «✨» вверху.`;
  }
  $("#ai-cost").innerHTML = text;
}

$("#ai-limit").addEventListener("change", updateAiModal);
$("#ai-action").addEventListener("change", () => {
  // «сразу в «Готов к отправке»» по умолчанию включено для «Подготовить для Prom»
  $("#ai-auto-promote").checked = ["prepare_prom", "from_photo"].includes($("#ai-action").value);
  updateAiModal();
});
$("#ai-cancel-modal").addEventListener("click", () => $("#ai-modal").classList.add("hidden"));
$("#ai-start").addEventListener("click", async () => {
  try {
    await api("/api/ai/bulk", { method: "POST", json: {
      ...aiSel, action: $("#ai-action").value, instruction: $("#ai-instr").value, only_empty: $("#ai-only-empty").checked,
      auto_promote: !$("#ai-promote-wrap").classList.contains("hidden") && $("#ai-auto-promote").checked,
      limit: aiLimit(),
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
  patchChildren($("#ai-jobs"), data.items, (j) => j.id, (j) => {
    const [cls, label] = AI_JOB_STATUS[j.status] || ["draft", j.status];
    const c = j.counts;
    const doneCount = (c.done || 0) + (c.skipped || 0) + (c.error || 0);
    const stats = [`${doneCount} из ${j.total}`, c.done ? `изменено ${c.done}` : "", c.skipped ? `пропущено ${c.skipped}` : "",
      c.error ? `ошибок ${c.error}` : "", j.promoted ? `в «Готов к отправке» ${j.promoted}` : ""].filter(Boolean).map(I18N.tr).join(" · ");
    const active = j.status === "running" || j.status === "paused";
    return `<div class="job">
      <span class="badge ${cls}">${label}</span>
      <span><span>${esc(j.label)}</span>${j.instruction ? `: «<span translate="no">${esc(j.instruction.slice(0, 60))}</span>»` : ""}</span>
      <span class="msg">${esc(stats)}${j.spent && j.spent.requests ? ` · потрачено ${esc(aiMoney(j.spent.cost_usd, j.spent.cost_uah, { withUsd: false }))}` : ""}${j.message ? ` · ${esc(j.message)}` : ""}${j.errors.length ? ` · ${esc(j.errors[0].message)}` : ""}</span>
      ${j.status === "running" ? `<button class="btn small" data-ai-job="${j.id}" data-st="paused">Пауза</button>` : ""}
      ${j.status === "paused" ? `<button class="btn small" data-ai-job="${j.id}" data-st="running">Продолжить</button>` : ""}
      ${active ? `<button class="btn small" data-ai-job="${j.id}" data-st="cancelled">Отменить</button>` : ""}
      ${c.done && j.status !== "reverted" ? `<button class="btn small danger" data-ai-revert="${j.id}">Откатить</button>` : ""}
    </div>`;
  });
  if (data.items.some((j) => j.status === "running")) {
    aiTimer = setTimeout(() => { loadAiJobs(); refreshList(); }, 4000);
  }
}

$("#ai-jobs").addEventListener("click", async (e) => {
  const job = e.target.closest("[data-ai-job]");
  if (job) {
    try { await api(`/api/ai/bulk/${job.dataset.aiJob}/status`, { method: "POST", json: { status: job.dataset.st } }); }
    catch (err) { toast(err.message, "error"); }
    loadAiJobs();
    return;
  }
  const b = e.target.closest("[data-ai-revert]");
  if (b) busy(b, async () => {
    if (!confirm("Вернуть прежние тексты у всех товаров этого задания?")) return;
    try {
      const r = await api(`/api/ai/bulk/${b.dataset.aiRevert}/revert`, { method: "POST" });
      toast(`Откачено: ${r.restored}`, "ok");
    } catch (err) { toast(err.message, "error"); }  // раньше ошибка отката пропадала молча
    loadAiJobs();
    loadProducts();
  });
});

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
    const ext = state.ext_running
      ? `<div><span class="badge sending">Записываю ID на Prom</span> ${esc(state.ext_stage || "")} · готово ${state.ext_done || 0}
         из ${state.ext_total || "…"}</div>`
      : state.ext_error ? `<div class="err-text">ID на Prom записаны не все: ${esc(state.ext_error)}
         <button class="btn small" id="ext-fix">Попробовать снова</button>
         <button class="btn small" id="ext-dismiss">Скрыть</button></div>`
      : state.ext_finished_at && !state.no_external_id ? `<div class="small" style="color:var(--ok)">✓ ID записаны на Prom
         (${state.ext_done || 0})${state.ext_skipped ? ` · пропущено ${state.ext_skipped}: на Prom уже другой ID или товара нет` : ""}</div>` : "";
    const warn = state.no_external_id && !state.ext_running && !state.ext_error
      ? `<div class="err-text" style="color:var(--warn)">У ${state.no_external_id} товаров в кабинете Prom нет «внешнего ID» —
         без него Prom при отправке может создать копию товара. <button class="btn small" id="ext-fix">Как исправить</button></div>` : "";
    box.innerHTML = `<span class="badge synced">Каталог загружен</span> ${esc(formatDate(state.finished_at))}:
      новых ${state.created}, обновлено ${state.updated}${state.skipped ? `, пропущено (удалённые) ${state.skipped}` : ""}${
        state.kept_local ? `, оставлены ваши неотправленные правки: ${state.kept_local}` : ""}${ext}${warn}${
        state.missing_on_prom ? `<div class="err-text">${state.missing_on_prom} товаров считались выгруженными, но на Prom их нет —
          <a href="/?status=error">показать их</a>: отправьте заново или удалите из программы.</div>` : ""}`;
    const dismiss = $("#ext-dismiss");
    if (dismiss) dismiss.onclick = () => busy(dismiss, async () => showCatalogState(await api("/api/prom/external-ids/dismiss", { method: "POST" })));
    const fix = $("#ext-fix");
    if (fix) fix.onclick = () => busy(fix, openExtIds);
    if (state.ext_running) {
      catalogTimer = setTimeout(async () => {
        try { showCatalogState(await api("/api/prom/catalog")); } catch { /* повторим при следующем обновлении */ }
      }, 5000);
    }
  }
}

// «Ідентифікатор_товару» на Prom: программа записывает его сама импортом через API — сначала 2 товара с проверкой
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
  const sample = info.sample.map((p) => `<li><span translate="no">${esc(p.name)}</span> — ID <b translate="no">${esc(p.external_id)}</b>
    · <a href="${esc(p.prom_url)}" target="_blank" rel="noopener">открыть на Prom ↗</a></li>`).join("");
  modal.innerHTML = `<div class="modal-box preview-box">
    <h2>Записать ID товарам на Prom</h2>
    <p>Программа узнаёт товары на Prom по полю «Ідентифікатор_товару». У части ваших товаров в кабинете оно пустое,
      и при отправке Prom может создать копию. Программа уже дала этим товарам ID — их код, а если кода нет или он
      повторяется, <code>PROM-номер</code>. Осталось записать эти ID на Prom — программа сделает это сама.</p>
    <ol class="ext-steps">
      <li>Сначала <b>проба на 2 товарах</b>: <ul class="small">${sample}</ul>
        Программа проверит, что ID записался именно этим товарам, а цена и наличие не изменились.</li>
      <li>Всё в порядке — остальные ${Math.max(0, info.count - 2)} товаров одним файлом. Если что-то не так, программа
        остановится и скажет, что именно.</li>
    </ol>
    <p class="small muted">Меняется только «Ідентифікатор_товару»: названия, цены, наличие, описания и фото остаются
      как есть, копий не появляется (проверено на тестовых товарах). Prom обрабатывает такие файлы медленно — всё
      займёт 10–20 минут, окно можно закрыть. ID, которые на Prom уже есть, программа не трогает.</p>
    <div class="toolbar" style="margin:12px 0 0;justify-content:flex-end">
      <button class="btn" data-a="close">Закрыть</button>
      <button class="btn primary" data-a="write">Записать ID на Prom (${info.count})</button>
    </div></div>`;
  modal.querySelector('[data-a="close"]').onclick = () => modal.classList.add("hidden");
  modal.querySelector('[data-a="write"]').onclick = (e) => busy(e.target, async () => {
    try {
      const state = await api("/api/prom/external-ids/write", { method: "POST" });
      modal.classList.add("hidden");
      toast("Записываю ID на Prom — ход виден над списком", "ok");
      showCatalogState(state);
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
    tunnel: [true, `Фото с компьютера Prom забирает через временный адрес — пока компьютер включён. Надёжнее —
      <a href="/settings">хранилище R2</a> (бесплатно).`],
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
  // новая установка (товаров ещё нет) — сразу предлагаем переезд, чтобы не искать его в «Настройках»
  $("#move-here").classList.toggle("hidden", f.products > 0);
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
    META.suppliers.map((s) => `<option value="${s.id}" translate="no">${esc(s.name)}</option>`).join("");
  $("#supplier-filter").classList.toggle("hidden", !META.suppliers.length);
  $("#f-group").innerHTML = `<option value="">Все группы</option><option value="-">Без группы</option>` +
    (META.groups || []).map((g) => `<option value="${esc(g)}" translate="no">${esc(g)}</option>`).join("");
  $("#f-presence").innerHTML = `<option value="">Любое наличие</option>` +
    Object.entries(META.presence).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#f-prom_category").innerHTML = `<option value="">все</option><option value="none">без категории Prom</option>` +
    (META.prom_categories || []).map((c) => `<option value="${c.id}" translate="no">${esc(c.caption || "№" + c.id)}</option>`).join("");
  $("#e-presence").innerHTML = `<option value="">не менять</option>` +
    Object.entries(META.presence).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#e-groups").innerHTML = (META.groups || []).map((g) => `<option value="${esc(g)}">`).join("");
  $("#search").value = list.q;
  renderFilterControls();
  syncUrl();
  const place = savedPlace();
  if (place) {
    // вернулись из карточки: столько же строк, те же отметки и та же прокрутка, что были
    list.restoreCount = place.count;
    list.selected = new Set(place.selected);
  }
  await loadProducts();
  if (place) requestAnimationFrame(() => window.scrollTo(0, place.scrollY));
  loadJobs();
})();

// ---------- список помнит место; порядок товаров для «← Пред. / След. →» в карточке ----------
const PLACE_KEY = "promloader-list-place";
function saveListPlace() {
  try {
    sessionStorage.setItem(PLACE_KEY, JSON.stringify({
      url: location.pathname + location.search, at: Date.now(), scrollY: Math.round(window.scrollY),
      count: list.items.length, selected: [...list.selected], ids: list.items.map((p) => p.id),
    }));
  } catch { /* приватный режим — просто не запоминаем */ }
}
function savedPlace() {
  try {
    const p = JSON.parse(sessionStorage.getItem(PLACE_KEY) || "null");
    return p && p.url === location.pathname + location.search && Date.now() - p.at < 3 * 3600 * 1000 ? p : null;
  } catch { return null; }
}
window.addEventListener("pagehide", saveListPlace);

// ---------- 💲 цены ----------
let pricesSel = { ids: [] };
async function openPricesModal(sel) {
  pricesSel = sel;
  $("#prices-count").textContent = selectionCount();
  $("#prices-modal").classList.remove("hidden");
  $("#prices-currency").value = "";
  try {
    const sum = await api("/api/products/currencies", { method: "POST", json: sel });
    const parts = Object.entries(sum).map(([c, n]) => I18N.tr(c === "UAH" ? `в гривнах — ${n}` : `в ${c} — ${n}`));
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
    if (parts.length) toast(parts.map(I18N.tr).join(" · "), "ok");
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
        (META.groups || []).map((g) => `<option value="${esc(g)}" translate="no">${esc(g)}</option>`).join("");
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
