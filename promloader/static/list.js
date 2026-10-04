// Список товаров, массовые действия и очередь отправки.

const PAGE = 200;
const list = {
  status: "", q: "", supplier: new URLSearchParams(location.search).get("supplier") || "",
  items: [], total: 0, selected: new Set(),
};

async function loadProducts(append = false) {
  const offset = append ? list.items.length : 0;
  // при автообновлении перечитываем столько, сколько уже показано
  const limit = append ? PAGE : Math.max(PAGE, list.items.length);
  const params = new URLSearchParams({ status: list.status, q: list.q, supplier: list.supplier, limit, offset });
  const data = await api(`/api/products?${params}`);
  list.items = append ? list.items.concat(data.items) : data.items;
  list.total = data.total;
  $("#shown").textContent = list.total ? `показано ${list.items.length} из ${list.total}` : "";
  $("#more").classList.toggle("hidden", list.items.length >= list.total);
  const ids = new Set(data.items.map((p) => p.id));
  list.selected = new Set([...list.selected].filter((id) => ids.has(id)));
  renderChips(data.counts);
  renderRows();
  // пока товары удаляются с Prom — обновляем список, чтобы было видно, как они уходят
  clearTimeout(list.deletingTimer);
  if (data.counts.deleting) list.deletingTimer = setTimeout(() => loadProducts(), 5000);
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
    list.items = [];
    loadProducts();
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
  $("#empty").classList.toggle("hidden", list.items.length > 0 || list.status !== "" || list.q !== "" || list.supplier !== "");
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
        <div class="name">${esc(p.name) || '<span class="muted">Без названия</span>'}</div>
        <div class="small muted">${esc(p.external_id)} · фото: ${p.image_count}${p.supplier_name ? ` · ${esc(p.supplier_name)}` : ""}${p.locked_fields.length ? ` · <span title="Поля, изменённые вручную: ${esc(p.locked_fields.join(", "))}">🔒 ${p.locked_fields.length}</span>` : ""}</div>
        ${problems}${promError}${deleting}
      </td>
      <td class="hide-sm">${esc(p.group_name)}</td>
      <td class="hide-sm cost">${costCell(p)}</td>
      <td class="price">${priceCell(p)}</td>
      <td class="hide-sm small">${esc(META.presence[p.presence] || "")}${p.quantity !== null ? ` · ${p.quantity}` : ""}</td>
      <td>${statusBadge(p.status)}${p.synced_at && p.status !== "synced" ? `<span class="on-prom-note" title="Товар уже есть на Prom; изменения уйдут при следующей отправке">● есть на Prom</span>` : ""}</td>
      <td class="hide-sm small muted">${esc(formatDate(p.updated_at))}</td>
    </tr>`;
  }).join("");

  $$("#rows tr.item").forEach((tr) => {
    const id = Number(tr.dataset.id);
    tr.addEventListener("click", async (e) => {
      if (e.target.classList.contains("sel")) return;
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
      updateBulk();
    });
  });
  updateBulk();
}

function updateBulk() {
  const n = list.selected.size;
  $("#selected-count").textContent = n ? `выбрано: ${n}` : "";
  $$("#bulk [data-action]").forEach((b) => (b.disabled = !n));
  $("#check-all").checked = n > 0 && n === list.items.length;
}

$("#check-all").addEventListener("change", (e) => {
  list.selected = e.target.checked ? new Set(list.items.map((p) => p.id)) : new Set();
  renderRows();
});

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { list.q = e.target.value.trim(); list.items = []; loadProducts(); }, 250);
});

$("#more").addEventListener("click", () => loadProducts(true));
$("#supplier-filter").addEventListener("change", (e) => {
  list.supplier = e.target.value;
  list.items = [];
  const url = new URL(location.href);
  if (list.supplier) url.searchParams.set("supplier", list.supplier); else url.searchParams.delete("supplier");
  history.replaceState(null, "", url);
  loadProducts();
});

$$("#bulk [data-action]").forEach((b) => b.addEventListener("click", async () => {
  const ids = [...list.selected];
  const action = b.dataset.action;
  try {
    if (action === "send") {
      const res = await api("/api/sync", { method: "POST", json: { ids } });
      if (res.accepted) toast(`В очереди на Prom: ${res.accepted}`, "ok");
      if (res.rejected.length) {
        const first = res.rejected[0];
        toast(`Не отправлено ${res.rejected.length}: «${first.name || "без названия"}» — ${first.reasons.join("; ")}`, "error");
      }
      loadJobs();
    } else if (action === "ai") {
      openAiModal(ids);
      return;
    } else if (action === "prices") {
      openPricesModal(ids);
      return;
    } else if (action === "synced") {
      const res = await api("/api/products/status", { method: "POST", json: { ids, status: "synced" } });
      toast(res.changed ? `Снова «На Prom»: ${res.changed}` : "Эти товары ещё не выгружались на Prom", res.changed ? "ok" : "error");
    } else if (action === "delete") {
      openDeleteModal(ids);
      return;
    } else {
      await api("/api/products/status", { method: "POST", json: { ids, status: action } });
    }
    loadProducts();
  } catch (err) {
    toast(err.message, "error");
  }
}));

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
    $$("#jobs [data-retry]").forEach((b) => b.addEventListener("click", async () => {
      try {
        const res = await api(`/api/sync/jobs/${b.dataset.retry}/retry`, { method: "POST" });
        toast(`Повторно в очереди: ${res.accepted}`, "ok");
      } catch (err) {
        toast(err.message, "error");
      }
      loadJobs();
      loadProducts();
    }));
  }
  if (active) {
    jobsTimer = setTimeout(() => { loadJobs(); loadProducts(); }, 3000);
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
let aiIds = [];
let aiRate = 8;

function openAiModal(ids) {
  if (!META.ai || !META.ai.enabled) {
    toast("Сначала подключите ИИ в «Настройках» (бесплатно через Google Gemini)", "error");
    return;
  }
  aiIds = ids;
  $("#ai-count").textContent = ids.length;
  $("#ai-action").innerHTML = AI_BULK.map((k) => `<option value="${k}">${esc(META.ai.actions[k])}</option>`).join("");
  updateAiModal();
  $("#ai-modal").classList.remove("hidden");
}

function updateAiModal() {
  const action = $("#ai-action").value;
  $("#ai-instr-wrap").classList.toggle("hidden", action !== "custom");
  $("#ai-empty-wrap").classList.toggle("hidden", !["translate_ua", "keywords", "improve"].includes(action));
  const minutes = Math.ceil(aiIds.length / aiRate);
  $("#ai-eta").textContent = `Темп: до ${aiRate} товаров в минуту (лимит ИИ) — примерно ${minutes} мин. Можно закрыть страницу, работа продолжится.`;
}

$("#ai-action").addEventListener("change", updateAiModal);
$("#ai-cancel-modal").addEventListener("click", () => $("#ai-modal").classList.add("hidden"));
$("#ai-start").addEventListener("click", async () => {
  try {
    await api("/api/ai/bulk", { method: "POST", json: {
      ids: aiIds, action: $("#ai-action").value, instruction: $("#ai-instr").value, only_empty: $("#ai-only-empty").checked,
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
  $$("[data-ai-revert]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Вернуть прежние тексты у всех товаров этого задания?")) return;
    const r = await api(`/api/ai/bulk/${b.dataset.aiRevert}/revert`, { method: "POST" });
    toast(`Откачено: ${r.restored}`, "ok");
    loadAiJobs();
    loadProducts();
  }));
  if (data.items.some((j) => j.status === "running")) {
    aiTimer = setTimeout(() => { loadAiJobs(); loadProducts(); }, 4000);
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
      if (!next.running) { loadProducts(); loadMeta(); }
      showCatalogState(next);
    }, 1500);
  } else if (state.error) {
    box.innerHTML = `<span class="badge error">Каталог не загружен</span> ${esc(state.error)}`;
  } else {
    const warn = state.no_external_id
      ? `<div class="err-text" style="color:var(--warn)">У ${state.no_external_id} товаров в кабинете Prom нет «внешнего ID». Перед массовой отправкой
         из программы проверьте на одном таком товаре, что Prom обновил его, а не создал копию.</div>` : "";
    box.innerHTML = `<span class="badge synced">Каталог загружен</span> ${esc(formatDate(state.finished_at))}:
      новых ${state.created}, обновлено ${state.updated}${state.skipped ? `, пропущено (удалённые) ${state.skipped}` : ""}${
        state.kept_local ? `, оставлены ваши неотправленные правки: ${state.kept_local}` : ""}${warn}${
        state.missing_on_prom ? `<div class="err-text">${state.missing_on_prom} товаров считались выгруженными, но на Prom их нет —
          они в фильтре «Ошибка»: отправьте заново или удалите из программы.</div>` : ""}`;
  }
}

$("#prom-catalog").addEventListener("click", async () => {
  if (!confirm("Загрузить в программу все товары из вашего кабинета Prom? Уже существующие в программе товары с тем же артикулом обновятся.")) return;
  try {
    showCatalogState(await api("/api/prom/catalog", { method: "POST" }));
  } catch (err) {
    toast(err.message, "error");
  }
});

(async () => {
  await loadMeta();
  api("/api/prom/catalog").then(showCatalogState).catch(() => {});
  loadAiJobs();
  $("#supplier-filter").innerHTML = `<option value="">Все поставщики</option><option value="none">Без поставщика</option>` +
    META.suppliers.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  $("#supplier-filter").value = list.supplier;
  $("#supplier-filter").classList.toggle("hidden", !META.suppliers.length);
  await loadProducts();
  loadJobs();
})();

// ---------- 💲 цены ----------
let pricesIds = [];
async function openPricesModal(ids) {
  pricesIds = ids;
  $("#prices-count").textContent = ids.length;
  $("#prices-modal").classList.remove("hidden");
  $("#prices-currency").value = "";
  try {
    const sum = await api("/api/products/currencies", { method: "POST", json: { ids } });
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
    const res = await api("/api/products/prices", { method: "POST", json: { ids: pricesIds, action, value, currency } });
    $("#prices-modal").classList.add("hidden");
    toast(`Цен изменено: ${res.changed}` + (res.queued ? `, отправлено на Prom: ${res.queued}` : ""), "ok");
    res.warnings.forEach((w) => toast(w, "error"));
    loadProducts();
  } catch (err) { toast(err.message, "error"); }
  $("#prices-apply").disabled = false;
};

// ---------- удаление ----------
let deleteIds = [];
async function openDeleteModal(ids) {
  deleteIds = ids;
  let info;
  try { info = await api("/api/products/delete/check", { method: "POST", json: { ids } }); }
  catch (err) { toast(err.message, "error"); return; }
  if (!info.on_prom) {
    if (!confirm(`Удалить товаров: ${ids.length}? На Prom их нет. Это нельзя отменить.`)) return;
    return doDelete(false);
  }
  $("#delete-count").textContent = ids.length;
  $("#delete-on-prom").textContent = info.on_prom;
  $("#delete-keep-count").textContent = info.on_prom;
  $("#delete-sending").textContent = info.sending
    ? `${info.sending} сейчас отправляются на Prom — их удалить не получится, пока отправка не закончится.` : "";
  $("input[name=delete-mode][value=prom]").checked = true;
  $("#delete-modal").classList.remove("hidden");
}
async function doDelete(fromProm) {
  try {
    const res = await api("/api/products/delete", { method: "POST", json: { ids: deleteIds, prom: fromProm } });
    const parts = [];
    if (res.deleted) parts.push(`удалено: ${res.deleted}`);
    if (res.deleting) parts.push(`удаляются с Prom: ${res.deleting} — уйдут из списка, когда Prom подтвердит`);
    if (res.kept_on_prom) parts.push(`на Prom остались: ${res.kept_on_prom}`);
    if (parts.length) toast(parts.join(" · "), "ok");
    if (res.rejected.length) toast(`Не удалено ${res.rejected.length}: ${res.rejected[0].reason}`, "error");
    list.selected.clear();
    loadProducts();
  } catch (err) { toast(err.message, "error"); }
}
$("#delete-cancel").onclick = () => $("#delete-modal").classList.add("hidden");
$("#delete-apply").onclick = () => {
  $("#delete-modal").classList.add("hidden");
  doDelete($("input[name=delete-mode]:checked").value === "prom");
};
