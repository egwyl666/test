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

function renderRows() {
  const tbody = $("#rows");
  $("#empty").classList.toggle("hidden", list.items.length > 0 || list.status !== "" || list.q !== "" || list.supplier !== "");
  tbody.innerHTML = list.items.map((p) => {
    const problems = p.check.errors.length ? `<div class="err-text">${esc(p.check.errors.join(" · "))}</div>` : "";
    const promError = p.status === "error" && p.last_error ? `<div class="err-text" title="${esc(p.last_error)}">Prom: ${esc(p.last_error.slice(0, 120))}</div>` : "";
    return `
    <tr class="item" data-id="${p.id}">
      <td><input type="checkbox" class="sel" ${list.selected.has(p.id) ? "checked" : ""}></td>
      <td>${p.thumb ? `<img class="thumb" src="${esc(p.thumb)}" alt="" loading="lazy">` : `<div class="thumb empty">▢</div>`}</td>
      <td>
        <div class="name">${esc(p.name) || '<span class="muted">Без названия</span>'}</div>
        <div class="small muted">${esc(p.external_id)} · фото: ${p.image_count}${p.supplier_name ? ` · ${esc(p.supplier_name)}` : ""}${p.locked_fields.length ? ` · <span title="Поля, изменённые вручную: ${esc(p.locked_fields.join(", "))}">🔒 ${p.locked_fields.length}</span>` : ""}</div>
        ${problems}${promError}
      </td>
      <td class="hide-sm">${esc(p.group_name)}</td>
      <td class="price">${esc(formatPrice(p.price, p.currency))}${p.cost_price !== null ? `<div class="small muted" style="font-weight:400">закупка ${esc(formatPrice(p.cost_price, p.currency))}</div>` : ""}</td>
      <td class="hide-sm small">${esc(META.presence[p.presence] || "")}${p.quantity !== null ? ` · ${p.quantity}` : ""}</td>
      <td>${statusBadge(p.status)}</td>
      <td class="hide-sm small muted">${esc(formatDate(p.updated_at))}</td>
    </tr>`;
  }).join("");

  $$("#rows tr.item").forEach((tr) => {
    const id = Number(tr.dataset.id);
    tr.addEventListener("click", (e) => {
      if (e.target.classList.contains("sel")) return;
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
    } else if (action === "delete") {
      if (!confirm(`Удалить товаров: ${ids.length}? Это нельзя отменить.`)) return;
      await api("/api/products/delete", { method: "POST", json: { ids } });
      list.selected.clear();
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

onPageFileDrop(quickCreate, { accept: isImage, text: "Отпустите — на каждое фото создастся товар" });
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
  const parts = [];
  for (const [key, label] of [["imported", "импортировано"], ["created", "создано"], ["updated", "обновлено"], ["not_changed", "без изменений"], ["with_errors_count", "с ошибками"]]) {
    if (r[key]) parts.push(`${label}: ${r[key]}`);
  }
  return parts.join(", ") || (r.status ? `статус: ${r.status}` : "");
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
      новых ${state.created}, обновлено ${state.updated}${state.skipped ? `, пропущено (удалённые) ${state.skipped}` : ""}${warn}`;
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
  $("#supplier-filter").innerHTML = `<option value="">Все поставщики</option><option value="none">Без поставщика</option>` +
    META.suppliers.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  $("#supplier-filter").value = list.supplier;
  $("#supplier-filter").classList.toggle("hidden", !META.suppliers.length);
  await loadProducts();
  loadJobs();
})();
