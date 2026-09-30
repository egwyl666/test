// Редактор товара: автосохранение, фото перетаскиванием, живое превью.

const SAVE_DELAY = 600;
const RETRY_DELAY = 5000;

const state = {
  id: Number(new URLSearchParams(location.search).get("id")) || null,
  product: null,
  pending: {},        // изменения, ещё не подтверждённые сервером
  saving: null,       // текущий запрос сохранения
  saveTimer: null,
  retryTimer: null,
  view: "page",
  lang: "ru",
  activeImage: 0,
  uploads: [],        // {key, file, status: 'uploading'|'failed', error}
};

const form = $("#form");
const backupKey = () => `promloader:draft:${state.id || "new"}`;

// ---------- резервная копия в браузере ----------

function writeBackup() {
  try {
    if (Object.keys(state.pending).length) {
      localStorage.setItem(backupKey(), JSON.stringify({ pending: state.pending, at: Date.now() }));
    } else {
      localStorage.removeItem(backupKey());
    }
  } catch { /* приватный режим — живём без резервной копии */ }
}

function readBackup() {
  try {
    const raw = localStorage.getItem(backupKey());
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

// ---------- форма ----------

function formValues() {
  const values = {};
  for (const el of form.elements) {
    if (el.name) values[el.name] = el.value;
  }
  values.params = readParams();
  return values;
}

function fillForm(p) {
  for (const el of form.elements) {
    if (!el.name || !(el.name in p)) continue;
    const v = p[el.name];
    el.value = v === null || v === undefined ? "" : v;
  }
  renderParams(p.params || []);
}

function readParams() {
  return $$(".param-row", $("#params")).map((row) => ({
    name: row.querySelector("[data-k=name]").value,
    value: row.querySelector("[data-k=value]").value,
  }));
}

function renderParams(params) {
  const box = $("#params");
  box.innerHTML = "";
  params.forEach((p) => addParamRow(p.name, p.value));
}

function addParamRow(name = "", value = "", focus = false) {
  const row = document.createElement("div");
  row.className = "param-row";
  row.innerHTML = `
    <input data-k="name" placeholder="Например: Цвет" value="${esc(name)}">
    <input data-k="value" placeholder="Белый" value="${esc(value)}">
    <button type="button" class="btn small" title="Удалить">✕</button>`;
  row.querySelector("button").onclick = () => { row.remove(); changed("params"); };
  $("#params").appendChild(row);
  if (focus) row.querySelector("input").focus();
}

function changed(field) {
  const values = formValues();
  state.pending[field] = values[field];
  writeBackup();
  renderPreview();
  scheduleSave();
}

form.addEventListener("input", (e) => {
  const el = e.target;
  if (el.name) changed(el.name);
  else if (el.closest(".param-row")) changed("params");
});
form.addEventListener("change", (e) => { if (e.target.name) changed(e.target.name); });

// ---------- сохранение ----------

function setSaveState(kind, text) {
  const el = $("#save-state");
  el.className = "save-state " + kind;
  el.textContent = text;
}

function scheduleSave(delay = SAVE_DELAY) {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => save().catch(() => {}), delay);
  setSaveState("saving", "Есть изменения…");
}

async function save() {
  clearTimeout(state.saveTimer);
  clearTimeout(state.retryTimer);
  if (state.saving) {
    await state.saving.catch(() => {});
    return save();
  }
  const payload = state.pending;
  if (!Object.keys(payload).length) return;
  state.pending = {};
  setSaveState("saving", "Сохраняю…");
  state.saving = (async () => {
    try {
      let product;
      if (state.id) {
        product = await api(`/api/products/${state.id}`, { method: "PATCH", json: payload });
      } else {
        product = await api("/api/products", { method: "POST", json: payload });
        const oldKey = backupKey();
        state.id = product.id;
        history.replaceState(null, "", `/product?id=${product.id}`);
        try { localStorage.removeItem(oldKey); } catch {}
      }
      applyServer(product);
      writeBackup();
      setSaveState("saved", "Сохранено ✓");
    } catch (err) {
      // вернуть неотправленное, не затирая то, что ввели за время запроса
      state.pending = { ...payload, ...state.pending };
      writeBackup();
      if (err.network || err.status >= 500) {
        setSaveState("failed", "Нет связи — изменения сохранены в браузере, повторяю…");
        state.retryTimer = setTimeout(() => save().catch(() => {}), RETRY_DELAY);
      } else {
        setSaveState("failed", err.message);
        toast(err.message, "error");
      }
      throw err;
    }
  })();
  try { await state.saving; } finally { state.saving = null; }
  if (Object.keys(state.pending).length) scheduleSave(0);
}

async function flush() {
  if (Object.keys(state.pending).length || state.saving) {
    await save();
  }
  return state.id;
}

async function ensureId() {
  if (state.id) return state.id;
  state.pending = { ...formValues(), ...state.pending };
  await save();
  return state.id;
}

window.addEventListener("beforeunload", (e) => {
  if (Object.keys(state.pending).length || state.saving || state.uploads.some((u) => u.status === "uploading")) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// Серверная копия: обновляем служебное, не трогая поля, которые пользователь ещё редактирует.
function applyServer(product) {
  state.product = product;
  const active = document.activeElement;
  for (const el of form.elements) {
    if (!el.name || el === active || el.name in state.pending) continue;
    if (el.name === "external_id" && product.external_id) el.value = product.external_id;
  }
  $("#title").textContent = product.name || "Новый товар";
  $("#status").innerHTML = statusBadge(product.status);
  $("#last-error").textContent = product.status === "error" && product.last_error ? "Ошибка Prom: " + product.last_error : "";
  ["#btn-duplicate", "#btn-delete", "#btn-send"].forEach((s) => ($(s).disabled = false));
  $("#btn-send").disabled = product.status === "sending";
  $("#btn-send").textContent = product.status === "sending" ? "Отправляется…" : "Отправить на Prom";
  renderCheck(product.check);
  renderSupplier(product);
  renderPhotos();
  renderPreview();
  if (product.status === "sending") pollStatus();
}

// ---------- поставщик и закреплённые поля ----------

const LOCK_LABEL = { images: "фото", params: "характеристики" };

function renderSupplier(p) {
  const box = $("#supplier-box");
  const locked = new Set(p.locked_fields || []);
  box.classList.toggle("hidden", !p.supplier);
  if (p.supplier) {
    box.classList.toggle("missing", p.supplier.missing);
    box.innerHTML = `Товар поставщика <a href="/supplier?id=${p.supplier.id}"><b>${esc(p.supplier.name)}</b></a>.
      ${p.supplier.missing ? "<b>Сейчас его нет в прайсе поставщика.</b> " : ""}
      Цена и наличие обновляются из прайса. Поля, которые вы поменяли руками, помечены <span class="lock" style="cursor:default">🔒 своё</span> —
      поставщик их не перезапишет.`;
  }
  // значки у полей
  $$(".lock[data-field]").forEach((b) => b.remove());
  const lockable = p.supplier || p.cost_price !== null || p.rrp !== null;
  if (lockable) {
    for (const field of locked) {
      const input = form.elements[field];
      const target = input && input.closest ? input.closest("label.field")?.querySelector("span") : $(`[data-lock=${field}]`);
      if (!target) continue;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "lock";
      b.dataset.field = field;
      b.textContent = "🔒 своё";
      b.title = p.supplier ? "Изменено вручную. Нажмите, чтобы снова брать значение у поставщика" : "Изменено вручную. Нажмите, чтобы снова считать по наценке";
      b.onclick = (e) => { e.preventDefault(); unlockField(field); };
      target.appendChild(b);
    }
  }
  const cost = p.cost_price;
  const margin = $("#margin");
  if (cost !== null || p.rrp !== null) {
    const parts = [];
    if (cost !== null) parts.push(`закупка ${formatPrice(cost, p.currency)}`);
    if (p.rrp !== null) parts.push(`РРЦ ${formatPrice(p.rrp, p.currency)}`);
    if (cost !== null && p.price) {
      const m = p.price - cost;
      parts.push(`маржа ${formatPrice(m, p.currency)} (${Math.round((m / cost) * 100)}%)`);
    }
    margin.textContent = parts.join(" · ") + (locked.has("price") ? "" : " · цена считается по правилам наценки");
  } else {
    margin.textContent = "";
  }
}

async function unlockField(field) {
  const what = LOCK_LABEL[field] || "это поле";
  const source = state.product.supplier ? "из прайса поставщика" : "по правилам наценки";
  if (!confirm(`Вернуть ${what} ${source}? Ваше значение будет заменено.`)) return;
  try {
    await flush();
    const p = await api(`/api/products/${state.id}/unlock`, { method: "POST", json: { fields: [field] } });
    if (field === "params") renderParams(p.params);
    else if (form.elements[field]) form.elements[field].value = p[field] ?? "";
    applyServer(p);
    toast("Значение возвращено", "ok");
  } catch (err) {
    toast(err.message, "error");
  }
}

function renderCheck(check) {
  const items = [
    ...check.errors.map((t) => `<li class="err">${esc(t)}</li>`),
    ...check.warnings.map((t) => `<li class="warn">${esc(t)}</li>`),
  ];
  if (!items.length) items.push(`<li class="ok">Всё заполнено — можно отправлять</li>`);
  else if (!check.errors.length) items.unshift(`<li class="ok">Можно отправлять</li>`);
  $("#check").innerHTML = items.join("");
}

// ---------- превью ----------

function previewData() {
  const values = formValues();
  return {
    ...(state.product || {}),
    ...values,
    external_id: values.external_id || state.product?.external_id || "",
    images: state.product?.images || [],
  };
}

function renderPreview() {
  const p = previewData();
  const box = $("#preview");
  if (state.view === "page") {
    box.innerHTML = renderProductPage(p, state.lang, Math.min(state.activeImage, Math.max(0, p.images.length - 1)));
    $$(".thumbs img", box).forEach((img) => img.addEventListener("click", () => {
      state.activeImage = Number(img.dataset.idx);
      renderPreview();
    }));
  } else {
    box.innerHTML = `<div class="tile-wrap">${renderTile(p, state.lang)}</div>`;
  }
}

$$(".preview-tabs [data-view]").forEach((b) => b.addEventListener("click", () => {
  state.view = b.dataset.view;
  $$(".preview-tabs [data-view]").forEach((x) => x.classList.toggle("active", x === b));
  renderPreview();
}));

$$("#preview-lang button").forEach((b) => b.addEventListener("click", () => {
  state.lang = b.dataset.lang;
  $$("#preview-lang button").forEach((x) => x.classList.toggle("active", x === b));
  renderPreview();
}));

$$("#lang-tabs button").forEach((b) => b.addEventListener("click", () => {
  $$("#lang-tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$("[data-lang-block]").forEach((blk) => blk.classList.toggle("hidden", blk.dataset.langBlock !== b.dataset.lang));
  // превью переключаем вслед за языком редактирования
  $(`#preview-lang [data-lang=${b.dataset.lang}]`).click();
}));

// ---------- фото ----------

function renderPhotos() {
  const box = $("#photos");
  const images = state.product?.images || [];
  box.innerHTML = "";
  images.forEach((img, i) => {
    const el = document.createElement("div");
    el.className = "photo";
    el.draggable = true;
    el.dataset.id = img.id;
    el.innerHTML = `<img src="${esc(img.src)}" alt="">
      ${i === 0 ? `<span class="main-label">Главное</span>` : ""}
      ${img.external ? `<span class="ext" title="Фото по ссылке">URL</span>` : ""}
      <button type="button" class="del" title="Удалить фото">✕</button>`;
    el.querySelector(".del").onclick = () => deleteImage(img.id);
    box.appendChild(el);
  });
  state.uploads.forEach((u) => {
    const el = document.createElement("div");
    if (u.status === "uploading") {
      el.className = "photo uploading";
      el.textContent = "Загрузка…";
    } else {
      el.className = "photo failed";
      el.innerHTML = `<div>${esc(u.file.name)}<br>${esc(u.error)}</div>`;
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "btn small";
      retry.textContent = "Повторить";
      retry.onclick = () => uploadFiles([u.file], u);
      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "btn small link";
      drop.textContent = "убрать";
      drop.onclick = () => { state.uploads = state.uploads.filter((x) => x !== u); renderPhotos(); };
      el.append(retry, drop);
    }
    box.appendChild(el);
  });
  bindPhotoDrag();
}

async function uploadFiles(files, retryOf = null) {
  files = files.filter(isImage);
  if (!files.length) return;
  if (retryOf) state.uploads = state.uploads.filter((u) => u !== retryOf);
  const entries = files.map((file) => ({ file, status: "uploading", error: "" }));
  state.uploads.push(...entries);
  renderPhotos();
  try {
    await ensureId();
  } catch {
    entries.forEach((u) => { u.status = "failed"; u.error = "товар не сохранён"; });
    renderPhotos();
    return;
  }
  // по одному файлу: сбой одного не мешает остальным, и каждый можно повторить
  for (const entry of entries) {
    const body = new FormData();
    body.append("files", entry.file);
    try {
      const res = await api(`/api/products/${state.id}/images`, { method: "POST", body });
      if (res.errors.length) {
        entry.status = "failed";
        entry.error = res.errors[0].replace(/^[^:]+:\s*/, "");
      } else {
        state.uploads = state.uploads.filter((u) => u !== entry);
      }
      applyServer(res.product);
    } catch (err) {
      entry.status = "failed";
      entry.error = err.network ? "нет связи" : err.message;
      renderPhotos();
    }
  }
  const failed = entries.filter((e) => e.status === "failed").length;
  if (failed) toast(`Не загрузилось фото: ${failed}. Нажмите «Повторить» на карточке фото.`, "error");
}

async function deleteImage(imageId) {
  try {
    applyServer(await api(`/api/products/${state.id}/images/${imageId}`, { method: "DELETE" }));
  } catch (err) {
    toast(err.message, "error");
  }
}

async function addImageUrl(url) {
  try {
    await ensureId();
    applyServer(await api(`/api/products/${state.id}/images/url`, { method: "POST", json: { url } }));
  } catch (err) {
    toast(err.message, "error");
  }
}

// Перестановка фото перетаскиванием.
function bindPhotoDrag() {
  let dragged = null;
  $$(".photo[draggable=true]").forEach((el) => {
    el.addEventListener("dragstart", (e) => {
      dragged = el;
      el.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", el.dataset.id);
    });
    el.addEventListener("dragend", () => {
      el.classList.remove("dragging");
      $$(".photo.drop-before").forEach((x) => x.classList.remove("drop-before"));
    });
    el.addEventListener("dragover", (e) => {
      if (!dragged || dragged === el) return;
      e.preventDefault();
      $$(".photo.drop-before").forEach((x) => x.classList.remove("drop-before"));
      el.classList.add("drop-before");
    });
    el.addEventListener("drop", async (e) => {
      if (!dragged || dragged === el) return;
      e.preventDefault();
      e.stopPropagation();
      el.parentNode.insertBefore(dragged, el);
      const ids = $$(".photo[draggable=true]").map((x) => Number(x.dataset.id));
      dragged = null;
      try {
        applyServer(await api(`/api/products/${state.id}/images/order`, { method: "POST", json: { ids } }));
      } catch (err) {
        toast(err.message, "error");
        applyServer(await api(`/api/products/${state.id}`));
      }
    });
  });
  // перенос в конец списка
  const box = $("#photos");
  box.ondragover = (e) => { if (dragged) e.preventDefault(); };
  box.ondrop = async (e) => {
    if (!dragged || e.target !== box) return;
    e.preventDefault();
    box.appendChild(dragged);
    const ids = $$(".photo[draggable=true]").map((x) => Number(x.dataset.id));
    dragged = null;
    applyServer(await api(`/api/products/${state.id}/images/order`, { method: "POST", json: { ids } }));
  };
}

onPageFileDrop(uploadFiles, { accept: isImage, text: "Отпустите — фото добавятся к товару" });
$("#photo-drop").addEventListener("click", async () => uploadFiles(await pickFiles({ accept: "image/*" })));
$("#btn-pick").addEventListener("click", async () => uploadFiles(await pickFiles({ accept: "image/*" })));
$("#btn-url").addEventListener("click", () => {
  const url = prompt("Ссылка на фото (https://…)");
  if (url) addImageUrl(url);
});

// Вставка фото из буфера обмена.
document.addEventListener("paste", (e) => {
  const files = Array.from(e.clipboardData?.files || []).filter(isImage);
  if (files.length) {
    e.preventDefault();
    uploadFiles(files);
    return;
  }
  const text = e.clipboardData?.getData("text") || "";
  const inField = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (!inField && /^https?:\/\/\S+\.(jpe?g|png|gif|webp)(\?\S*)?$/i.test(text.trim())) {
    e.preventDefault();
    addImageUrl(text.trim());
  }
});

// ---------- действия ----------

$("#btn-add-param").addEventListener("click", () => addParamRow("", "", true));

$("#btn-send").addEventListener("click", async () => {
  try {
    await flush();
    const res = await api("/api/sync", { method: "POST", json: { ids: [state.id] } });
    if (res.rejected.length) {
      toast("Не отправлено: " + res.rejected[0].reasons.join("; "), "error");
    } else {
      toast("Поставлено в очередь на Prom", "ok");
    }
    applyServer(await api(`/api/products/${state.id}`));
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#btn-duplicate").addEventListener("click", async () => {
  try {
    await flush();
    const copy = await api(`/api/products/${state.id}/duplicate`, { method: "POST" });
    location.href = `/product?id=${copy.id}`;
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#btn-delete").addEventListener("click", async () => {
  if (!confirm("Удалить товар? Это действие нельзя отменить.")) return;
  try {
    await api("/api/products/delete", { method: "POST", json: { ids: [state.id] } });
    state.pending = {};
    writeBackup();
    location.href = "/";
  } catch (err) {
    toast(err.message, "error");
  }
});

let pollTimer = null;
function pollStatus() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const p = await api(`/api/products/${state.id}`);
      if (p.status !== state.product.status) {
        applyServer(p);
        if (p.status === "synced") toast("Товар выгружен на Prom", "ok");
        if (p.status === "error") toast("Prom вернул ошибку: " + p.last_error, "error");
      } else {
        pollStatus();
      }
    } catch {
      pollStatus();
    }
  }, 3000);
}

// ---------- старт ----------

async function init() {
  await loadMeta();
  $("#presence").innerHTML = Object.entries(META.presence).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#groups").innerHTML = (META.groups || []).map((g) => `<option value="${esc(g)}">`).join("");

  if (state.id) {
    try {
      const p = await api(`/api/products/${state.id}`);
      fillForm(p);
      applyServer(p);
    } catch (err) {
      toast(err.status === 404 ? "Товар не найден" : err.message, "error");
      if (err.status === 404) {
        state.id = null;
        history.replaceState(null, "", "/product");
      }
    }
  } else {
    fillForm({ currency: "UAH", presence: "available", unit: "шт.", params: [] });
  }

  // восстановление несохранённого после сбоя/закрытия вкладки
  const backup = readBackup();
  if (backup && Object.keys(backup.pending || {}).length) {
    const current = formValues();
    fillForm({ ...current, ...backup.pending, params: backup.pending.params || current.params });
    state.pending = backup.pending;
    toast("Восстановлены несохранённые изменения", "ok");
    scheduleSave(0);
  }
  renderPreview();
  if (!state.id) form.elements.name.focus();
}

init();
