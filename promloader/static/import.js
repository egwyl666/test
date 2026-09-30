// Мастер импорта: файл -> выбор строк и колонок -> проверка -> импорт.

const imp = {
  token: null, sheets: [], sheet: "", rows: [], totalRows: 0, letters: [], headers: [],
  mapping: {}, mappingTouched: false, selected: new Set(), imageRows: {}, lastClicked: null, items: [],
};

function showStep(n) {
  [1, 2, 3].forEach((i) => $(`#step${i}`).classList.toggle("hidden", i !== n));
  $$(".steps .step").forEach((s) => {
    const i = Number(s.dataset.step);
    s.classList.toggle("active", i === n);
    s.classList.toggle("done", i < n);
  });
}

// ---------- шаг 1: файл ----------

async function uploadFile(files) {
  const file = files[0];
  if (!file) return;
  if (!/\.(xlsx|xlsm|csv)$/i.test(file.name)) {
    toast("Нужен файл .xlsx или .csv. Старый .xls откройте в Excel и сохраните как .xlsx", "error");
    return;
  }
  const body = new FormData();
  body.append("file", file);
  $("#file-drop").innerHTML = "<b>Загружаю и читаю файл…</b>";
  try {
    const res = await api("/api/import/upload", { method: "POST", body });
    imp.token = res.token;
    imp.sheets = res.sheets;
    $("#filename").value = res.filename;
    $("#sheet").innerHTML = res.sheets.map((s) => `<option>${esc(s)}</option>`).join("");
    imp.mappingTouched = false;
    await loadSheet(res.sheets[0]);
    showStep(2);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    $("#file-drop").innerHTML = "<b style=\"font-size:17px\">Перетащите сюда файл Excel (.xlsx) или CSV</b><br>или нажмите, чтобы выбрать";
  }
}

const isSheetFile = (f) => /\.(xlsx|xlsm|csv|xls)$/i.test(f.name);
onPageFileDrop((files) => { if (!$("#step1").classList.contains("hidden")) uploadFile(files); },
  { accept: isSheetFile, text: "Отпустите файл Excel" });
$("#file-drop").addEventListener("click", async () => uploadFile(await pickFiles({ accept: ".xlsx,.xlsm,.csv", multiple: false })));

// ---------- шаг 2: строки и колонки ----------

async function loadSheet(sheet) {
  imp.sheet = sheet;
  const headerRow = Number($("#header-row").value) || 0;
  const data = await api(`/api/import/${imp.token}/sheet?${new URLSearchParams({ sheet, header_row: headerRow })}`);
  imp.rows = data.rows;
  imp.totalRows = data.total_rows;
  imp.letters = data.letters;
  imp.headers = data.headers;
  imp.imageRows = data.image_rows;
  if (!imp.mappingTouched) imp.mapping = data.mapping;
  if (!$("#rows-spec").dataset.touched) {
    $("#rows-spec").value = imp.totalRows > headerRow ? `${headerRow + 1}-${imp.totalRows}` : "";
  }
  selectionFromSpec();
  const embedded = Object.values(imp.imageRows).reduce((a, b) => a + b, 0);
  $("#embedded-wrap").classList.toggle("hidden", !embedded);
  $("#embedded-label").textContent = `прикреплять картинки, вставленные в Excel (найдено: ${embedded})`;
  $("#rows-info").textContent = imp.totalRows > imp.rows.length
    ? `В таблице показаны первые ${imp.rows.length} из ${imp.totalRows} строк — диапазон в поле выше работает для всех.` : "";
  renderGrid();
}

function parseSpec(spec, maxRow) {
  const out = new Set();
  for (const part of spec.split(/[,;\s]+/).filter(Boolean)) {
    const m = part.match(/^(\d+)?-(\d+)?$|^(\d+)$/);
    if (!m) throw new Error(`Не понял «${part}». Пример: 2-50, 55, 60-`);
    let a, b;
    if (m[3]) { a = b = Number(m[3]); } else { a = Number(m[1] || 1); b = Number(m[2] || maxRow); }
    if (a > b) [a, b] = [b, a];
    for (let i = Math.max(1, a); i <= Math.min(b, maxRow); i++) out.add(i);
  }
  return out;
}

function specFromSelection(set) {
  const nums = [...set].sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < nums.length; i++) {
    const start = nums[i];
    while (i + 1 < nums.length && nums[i + 1] === nums[i] + 1) i++;
    parts.push(start === nums[i] ? `${start}` : `${start}-${nums[i]}`);
  }
  return parts.join(", ");
}

function selectionFromSpec() {
  try {
    imp.selected = parseSpec($("#rows-spec").value, imp.totalRows);
    $("#rows-spec").style.borderColor = "";
  } catch (err) {
    $("#rows-spec").style.borderColor = "var(--err)";
    return false;
  }
  const headerRow = Number($("#header-row").value) || 0;
  imp.selected.delete(headerRow);
  return true;
}

function targetOptions(letter, idx) {
  const current = imp.mapping[letter] || "";
  return Object.entries(META.targets).map(([k, label]) => {
    if (k === "param") label = `Характеристика: ${imp.headers[idx] || "колонка " + letter}`;
    return `<option value="${k}" ${k === current ? "selected" : ""}>${esc(label)}</option>`;
  }).join("");
}

function renderGrid() {
  const headerRow = Number($("#header-row").value) || 0;
  const mappedIdx = new Set(imp.letters.map((l, i) => (imp.mapping[l] ? i : -1)).filter((i) => i >= 0));
  let html = `<thead><tr><th class="rn">№</th>${imp.letters.map((l) => `<th>${l}</th>`).join("")}</tr>
    <tr class="map"><th class="rn"></th>${imp.letters.map((l, i) =>
      `<th><select data-col="${l}" class="${imp.mapping[l] ? "mapped" : ""}">${targetOptions(l, i)}</select></th>`).join("")}</tr></thead><tbody>`;
  imp.rows.forEach((row, i) => {
    const n = i + 1;
    const isHeader = n === headerRow;
    const on = imp.selected.has(n);
    const pics = imp.imageRows[String(n)];
    html += `<tr class="${isHeader ? "header" : on ? "on" : "off"}" data-row="${n}">
      <td class="rn">${isHeader ? `заголовок ${n}` : `<label>${pics ? `<span title="картинок в строке: ${pics}">📷</span>` : ""}${n}<input type="checkbox" ${on ? "checked" : ""}></label>`}</td>
      ${row.map((v, ci) => `<td class="${mappedIdx.has(ci) ? "mapped-col" : ""}" title="${esc(v)}">${esc(v)}</td>`).join("")}
    </tr>`;
  });
  $("#grid").innerHTML = html + "</tbody>";
  updateSelectedInfo();
}

function updateSelectedInfo() {
  const mapped = Object.values(imp.mapping).filter(Boolean);
  const missing = ["name", "price"].filter((t) => !mapped.includes(t)).map((t) => META.targets[t]);
  $("#selected-info").innerHTML = `строк выбрано: <b>${imp.selected.size}</b>` +
    (missing.length ? ` · <span style="color:var(--err)">не указаны колонки: ${esc(missing.join(", "))}</span>` : "");
}

$("#grid").addEventListener("change", (e) => {
  const sel = e.target.closest("select[data-col]");
  if (sel) {
    const target = sel.value;
    // одно поле — одна колонка (кроме характеристик и фото)
    if (target && !["param", "images"].includes(target)) {
      for (const [l, t] of Object.entries(imp.mapping)) if (t === target && l !== sel.dataset.col) imp.mapping[l] = "";
    }
    imp.mapping[sel.dataset.col] = target;
    imp.mappingTouched = true;
    renderGrid();
  }
});

$("#grid").addEventListener("click", (e) => {
  const box = e.target.closest("td.rn input[type=checkbox]");
  if (!box) return;
  const n = Number(box.closest("tr").dataset.row);
  if (e.shiftKey && imp.lastClicked) {
    const [a, b] = [Math.min(n, imp.lastClicked), Math.max(n, imp.lastClicked)];
    const headerRow = Number($("#header-row").value) || 0;
    for (let i = a; i <= b; i++) if (i !== headerRow) box.checked ? imp.selected.add(i) : imp.selected.delete(i);
  } else {
    box.checked ? imp.selected.add(n) : imp.selected.delete(n);
  }
  imp.lastClicked = n;
  $("#rows-spec").value = specFromSelection(imp.selected);
  $("#rows-spec").dataset.touched = "1";
  renderGrid();
});

$("#rows-spec").addEventListener("input", () => {
  $("#rows-spec").dataset.touched = "1";
  if (selectionFromSpec()) renderGrid();
});
$("#header-row").addEventListener("change", () => loadSheet(imp.sheet).catch((err) => toast(err.message, "error")));
$("#sheet").addEventListener("change", (e) => {
  imp.mappingTouched = false;
  delete $("#rows-spec").dataset.touched;
  loadSheet(e.target.value).catch((err) => toast(err.message, "error"));
});
$("#back-1").addEventListener("click", () => showStep(1));

// ---------- шаг 3: проверка ----------

function requestBody() {
  return {
    sheet: imp.sheet,
    header_row: Number($("#header-row").value) || 0,
    rows: specFromSelection(imp.selected),
    mapping: imp.mapping,
    defaults: {
      group_name: $("#def-group").value,
      presence: $("#def-presence").value,
      currency: $("#def-currency").value,
    },
    use_embedded_images: $("#use-embedded").checked,
  };
}

$("#to-3").addEventListener("click", async () => {
  if (!imp.selected.size) return toast("Не выбрано ни одной строки", "error");
  try {
    const res = await api(`/api/import/${imp.token}/preview`, { method: "POST", json: requestBody() });
    imp.items = res.items;
    $("#result").style.display = "none";
    renderPreview();
    showStep(3);
  } catch (err) {
    toast(err.message, "error");
  }
});

function renderPreview() {
  const items = imp.items;
  const bad = items.filter((i) => i.errors.length);
  const existing = items.filter((i) => i.existing_id && !i.errors.length);
  $("#summary").innerHTML = `
    <div>Товаров: <b>${items.length}</b></div>
    <div style="color:var(--ok)">Готовы к импорту: <b>${items.length - bad.length}</b></div>
    <div style="color:var(--err)">С ошибками (пропустятся): <b>${bad.length}</b></div>
    <div class="muted">Уже есть (по артикулу): <b>${existing.length}</b></div>`;
  const onlyBad = $("#only-bad").checked;
  $("#preview").innerHTML = items.filter((i) => !onlyBad || i.errors.length).map((i) => {
    const embedded = Array.from({ length: i.embedded_images }, (_, n) => ({
      src: `/api/import/${imp.token}/image?${new URLSearchParams({ sheet: imp.sheet, row: i.row, n })}`,
    }));
    const p = { ...i.data, params: i.params, images: [...embedded, ...i.image_urls.map((src) => ({ src }))] };
    const checks = [
      ...i.errors.map((t) => `<li class="err">${esc(t)}</li>`),
      ...i.warnings.map((t) => `<li class="warn">${esc(t)}</li>`),
    ].join("");
    return `<div>
      <div class="row-no"><span>строка ${i.row}${i.existing_id ? " · обновит существующий" : ""}</span>
        <span>${i.embedded_images ? `📷 из Excel: ${i.embedded_images}` : ""}</span></div>
      <div class="${i.errors.length ? "bad" : ""}" style="border-radius:10px">${renderTile(p)}</div>
      ${checks ? `<ul class="check-list" style="margin-top:6px">${checks}</ul>` : ""}
    </div>`;
  }).join("");
  $("#commit").disabled = bad.length === items.length;
}

$("#only-bad").addEventListener("change", renderPreview);
$("#back-2").addEventListener("click", () => showStep(2));

$("#commit").addEventListener("click", async () => {
  const btn = $("#commit");
  btn.disabled = true;
  btn.textContent = "Импортирую…";
  try {
    const body = {
      ...requestBody(),
      status: $("input[name=status]:checked").value,
      update_existing: $("#update-existing").checked,
    };
    const res = await api(`/api/import/${imp.token}/commit`, { method: "POST", json: body });
    const failed = res.failed.map((f) => `<li class="err">строка ${f.row}: ${esc(f.error)}</li>`).join("");
    $("#result").style.display = "";
    $("#result").innerHTML = `
      <h2>Готово</h2>
      <div class="summary">
        <div>Создано: <b>${res.created}</b></div>
        <div>Обновлено: <b>${res.updated}</b></div>
        <div>Пропущено: <b>${res.skipped}</b></div>
      </div>
      ${failed ? `<ul class="check-list">${failed}</ul>` : ""}
      <a class="btn primary" href="/">Перейти к товарам</a>`;
    $("#result").scrollIntoView({ behavior: "smooth" });
    toast("Импорт завершён", "ok");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Импортировать";
  }
});

(async () => {
  await loadMeta();
  $("#def-presence").innerHTML += Object.entries(META.presence).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#groups").innerHTML = (META.groups || []).map((g) => `<option value="${esc(g)}">`).join("");
})();
