// Настройка поставщика: источник, колонки, правила обновления, история.

const sid = Number(new URLSearchParams(location.search).get("id"));
let supplier = null;
let gridOpened = false;
let items = [];
let pollTimer;
const grid = createMappingGrid($("#mapping"), {
  openEnded: true,
  required: ["external_id", "name", ["cost_price", "price", "rrp"]],
});

function settingsBody() {
  const body = {
    name: $("#name").value,
    url: $("#url").value.trim(),
    interval_hours: Number($("#interval").value),
    missing_action: $("#missing").value,
    prefix: $("#prefix").value,
    new_status: $("#new-status").value,
    auto_sync: $("#auto-sync").checked,
    merge_by_barcode: $("#merge-barcode").checked,
    clean_names: $("#clean-names").checked,
    rate_mode: $("#rate-mode").value,
    rate_value: $("#rate-value").value || 0,
    rate_add: 0,
    rate_currency: $("#def-currency").value === "UAH" ? "USD" : $("#def-currency").value,
    defaults: { group_name: $("#def-group").value, currency: $("#def-currency").value },
  };
  if (gridOpened) Object.assign(body, grid.body());
  return body;
}

function fill(s) {
  supplier = s;
  document.title = `${s.name} — Prom Loader`;
  $("#name").value = s.name;
  $("#url").value = s.url;
  $("#interval").value = String(s.interval_hours);
  if ($("#interval").value !== String(s.interval_hours)) {
    $("#interval").insertAdjacentHTML("beforeend", `<option value="${s.interval_hours}">каждые ${s.interval_hours} ч</option>`);
    $("#interval").value = String(s.interval_hours);
  }
  $("#missing").value = s.missing_action;
  $("#prefix").value = s.prefix;
  $("#new-status").value = s.new_status;
  $("#auto-sync").checked = s.auto_sync;
  $("#merge-barcode").checked = s.merge_by_barcode;
  $("#clean-names").checked = s.clean_names;
  $("#def-group").value = s.defaults.group_name || "";
  $("#def-currency").value = s.defaults.currency || "UAH";
  $("#rate-mode").value = s.rate_mode === "manual" ? "manual" : "";
  $("#rate-value").value = s.rate_value || "";
  showRate();
  $("#products-link").href = `/?supplier=${s.id}`;
  $("#source-info").innerHTML = s.source_name
    ? `Текущий прайс: <b>${esc(s.source_name)}</b> · в прайсе ${s.items_active} товаров` +
      (gridOpened ? "" : ` · <a href="#" id="open-current">открыть для настройки колонок</a>`) +
      (s.items_deleted ? ` · <a href="#deleted-panel">удалено вами: ${s.items_deleted}</a>` : "")
    : "";
  const open = $("#open-current");
  if (open) open.onclick = (e) => { e.preventDefault(); openSource(false); };
  renderRuns(s);
  loadDeleted(s.items_deleted);
}

// ---------- удалённые вами товары ----------
let deletedShown = -1;
async function loadDeleted(count) {
  $("#deleted-panel").hidden = !count;
  if (!count || count === deletedShown) return;  // список не менялся — не перерисовываем (страница обновляется сама)
  deletedShown = count;
  let rows = [];
  try { rows = await api(`/api/suppliers/${sid}/deleted`); } catch (err) { toast(err.message, "error"); return; }
  $("#deleted-count").textContent = rows.length;
  $("#deleted-all").checked = false;
  $("#deleted-table tbody").innerHTML = rows.map((r) => `<tr>
    <td><input type="checkbox" class="del-sel" value="${esc(r.sku)}"></td>
    <td class="small">${esc(r.sku)}</td><td>${esc(r.name) || '<span class="muted">без названия</span>'}</td>
    <td class="small">${r.price != null ? esc(formatPrice(r.price, r.currency || "UAH")) : ""}</td>
    <td class="small muted">${r.missing ? "сейчас нет в прайсе" : ""}</td></tr>`).join("");
  updateDeletedButtons();
}
function selectedDeleted() { return $$(".del-sel:checked").map((x) => x.value); }
function updateDeletedButtons() { $("#deleted-restore-selected").disabled = !selectedDeleted().length; }
$("#deleted-table").addEventListener("change", (e) => {
  if (e.target.id === "deleted-all") $$(".del-sel").forEach((x) => (x.checked = e.target.checked));
  updateDeletedButtons();
});
async function restoreDeleted(skus) {
  const n = skus ? skus.length : Number($("#deleted-count").textContent);
  if (!confirm(`Вернуть товаров: ${n}? Программа сразу обновит прайс, и они появятся в списке товаров.`)) return;
  try {
    const r = await api(`/api/suppliers/${sid}/restore-deleted`, { method: "POST", json: skus ? { skus, run: true } : { run: true } });
    toast(r.started ? `Возвращаю ${r.count}: обновляю прайс…` : `Вернётся при обновлении: ${r.count}`, "ok");
    deletedShown = -1;
    await reload();
  } catch (err) { toast(err.message, "error"); }
}
$("#deleted-restore-selected").onclick = () => restoreDeleted(selectedDeleted());
$("#deleted-restore-all").onclick = () => restoreDeleted(null);

function renderRuns(s) {
  const running = s.running;
  $("#run").disabled = running;
  $("#run").textContent = running ? "Обновляется…" : "Обновить сейчас";
  $("#run-state").innerHTML = running ? statusPill("running") : "";
  const broken = s.runs[0] && s.runs[0].status === "failed" && /сломан/.test(s.runs[0].message);
  $("#runs").innerHTML = s.runs.length ? s.runs.map((r, i) => `
    <tr>
      <td style="white-space:nowrap">${statusPill(r.status)}</td>
      <td class="muted" style="white-space:nowrap">${esc(formatDate(r.started_at))}<br>${r.trigger === "schedule" ? "по расписанию" : "вручную"}</td>
      <td>${r.status === "ok" ? esc(runSummary(r.stats)) : `<span class="run-msg">${esc(r.message)}</span>`}
        ${i === 0 && broken ? `<div style="margin-top:6px"><button class="btn small danger" id="force">Обновить принудительно</button></div>` : ""}
        ${r.stats && r.stats.error_samples && r.stats.error_samples.length ? `<details><summary>строки с ошибками (${r.stats.errors})</summary>
          <ul class="check-list">${r.stats.error_samples.map((e) => `<li class="err">${esc(e)}</li>`).join("")}</ul></details>` : ""}
      </td>
    </tr>`).join("") : `<tr><td class="muted">Ещё не обновлялся</td></tr>`;
  const force = $("#force");
  if (force) force.onclick = () => {
    if (confirm("Товары, которых нет в новом прайсе, будут сняты с продажи. Продолжить?")) runNow(true);
  };
  clearTimeout(pollTimer);
  if (running) pollTimer = setTimeout(reload, 2000);
}

async function reload() {
  const s = await api(`/api/suppliers/${sid}`);
  const wasRunning = supplier && supplier.running;
  fill(s);
  if (wasRunning && !s.running && s.runs[0]) {
    const r = s.runs[0];
    toast(r.status === "ok" ? `Обновлено: ${runSummary(r.stats)}` : r.message, r.status === "ok" ? "ok" : "error");
  }
}

async function showGrid(res) {
  const preset = gridOpened ? grid.body() : {
    sheet: supplier.sheet, header_row: supplier.header_row, rows: supplier.rows, mapping: supplier.mapping,
  };
  await grid.open(res.token, res.sheets, preset);
  gridOpened = true;
  $("#mapping-panel").classList.remove("hidden");
  $("#preview-btn").disabled = false;
  fill(supplier);
}

async function openSource(refetch) {
  const btn = $("#fetch");
  btn.disabled = true;
  btn.textContent = "Скачиваю…";
  try {
    if (refetch) await api(`/api/suppliers/${sid}`, { method: "PATCH", json: { url: $("#url").value.trim() } });
    const res = await api(`/api/suppliers/${sid}/open`, { method: "POST", json: { refetch } });
    await reload();
    await showGrid(res);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Скачать и открыть";
  }
}

async function uploadSource(files) {
  const file = files[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    const res = await api(`/api/suppliers/${sid}/source`, { method: "POST", body });
    await reload();
    await showGrid(res);
    toast("Прайс загружен", "ok");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function save() {
  const s = await api(`/api/suppliers/${sid}`, { method: "PATCH", json: settingsBody() });
  fill(s);
  return s;
}

async function runNow(force = false) {
  try {
    await api(`/api/suppliers/${sid}/run`, { method: "POST", json: { force } });
    toast("Обновление запущено", "ok");
    await reload();
  } catch (err) {
    toast(err.message, "error");
  }
}

$("#fetch").onclick = () => {
  if (!$("#url").value.trim()) return toast("Вставьте ссылку на прайс", "error");
  openSource(true);
};
$("#pick").onclick = async (e) => { e.preventDefault(); uploadSource(await pickFiles({ accept: ".xlsx,.xlsm,.csv,.xml,.yml", multiple: false })); };
onPageFileDrop(uploadSource, { accept: (f) => /\.(xlsx|xlsm|csv|xml|yml)$/i.test(f.name), text: "Отпустите — это станет прайсом поставщика" });

$("#save").onclick = async () => {
  try { await save(); toast("Сохранено", "ok"); } catch (err) { toast(err.message, "error"); }
};
$("#save-run").onclick = async () => {
  try { await save(); await runNow(); } catch (err) { toast(err.message, "error"); }
};
$("#run").onclick = () => runNow(false);
$("#delete").onclick = async () => {
  if (!confirm("Удалить поставщика? Его товары останутся в списке как обычные товары, но перестанут обновляться.")) return;
  await api(`/api/suppliers/${sid}`, { method: "DELETE" });
  location.href = "/suppliers";
};

async function preview() {
  try {
    const body = { ...grid.body(), defaults: settingsBody().defaults, supplier_id: sid, clean_names: $("#clean-names").checked };
    const res = await api(`/api/import/${grid.token}/preview`, { method: "POST", json: body });
    items = res.items;
    renderPreview();
    $("#preview-panel").classList.remove("hidden");
    $("#preview-panel").scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    toast(err.message, "error");
  }
}

function renderPreview() {
  const bad = items.filter((i) => i.errors.length || !i.data.external_id);
  $("#summary").innerHTML = `
    <div>Строк: <b>${items.length}</b></div>
    <div style="color:var(--ok)">Годных: <b>${items.length - bad.length}</b></div>
    <div style="color:var(--err)">С ошибками (пропустятся): <b>${bad.length}</b></div>`;
  const shown = items.slice(0, 60).map((i) => (i.data.external_id ? i : { ...i, errors: ["Нет артикула", ...i.errors] }));
  renderItemsPreview($("#preview"), shown, { token: grid.token, sheet: grid.sheet, onlyBad: $("#only-bad").checked });
  if (items.length > 60) $("#preview").insertAdjacentHTML("beforeend", `<p class="muted">…и ещё ${items.length - 60}</p>`);
}
$("#preview-btn").onclick = preview;
$("#only-bad").onchange = renderPreview;

(async () => {
  await loadMeta();
  $("#groups").innerHTML = (META.groups || []).map((g) => `<option value="${esc(g)}">`).join("");
  try {
    await reload();
  } catch (err) {
    toast(err.message, "error");
  }
})();

// ---------- курс ----------
const CUR_SIGN = { USD: "$", EUR: "€", PLN: "zł", GBP: "£" };
async function showRate() {
  const own = $("#rate-mode").value === "manual";
  const cur = $("#def-currency").value === "UAH" ? "USD" : $("#def-currency").value;
  $$(".rate-field").forEach((el) => el.classList.toggle("hidden", !own));
  $$(".rate-cur").forEach((el) => { el.textContent = CUR_SIGN[cur] || cur; });
  const info = $("#rate-info");
  info.classList.remove("hidden");
  let k = Number(String($("#rate-value").value || 0).replace(",", ".")) || 0;
  let note = "Свой курс поставщика. ";
  if (!own) {
    try {
      const r = (await api("/api/rates")).current[cur] || {};
      k = r.rate || 0;
      note = r.error ? r.error + " " : `Общий курс: 1 ${CUR_SIGN[cur] || cur} = ${k.toFixed(2)} грн (меняется на странице «Наценка»). `;
    } catch (err) { note = err.message + " "; }
  }
  info.textContent = note + (k ? `Закупка 10 ${CUR_SIGN[cur] || cur} = ${(10 * k).toFixed(2)} грн, дальше — наценка. ` : "") +
    "Цены в гривнах не пересчитываются. Чтобы работала наценка, колонку с ценой поставщика отметьте как «Цена закупки».";
}
["#rate-mode", "#rate-value", "#def-currency"].forEach((sel) => $(sel).addEventListener("input", showRate));
