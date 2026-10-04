// Общий компонент «строки и колонки»: лист, строка заголовков, какие строки брать и что лежит в каждой колонке.
// Используется в импорте из Excel и в настройке поставщика.

function parseRowSpec(spec, maxRow) {
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

function rowSpecFromSet(set) {
  const nums = [...set].sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < nums.length; i++) {
    const start = nums[i];
    while (i + 1 < nums.length && nums[i + 1] === nums[i] + 1) i++;
    parts.push(start === nums[i] ? `${start}` : `${start}-${nums[i]}`);
  }
  return parts.join(", ");
}

// required: список обязательных целей; элемент-массив = «хотя бы одна из».
function createMappingGrid(container, { openEnded = false, required = ["name", ["price", "cost_price", "rrp"]], onChange = () => {} } = {}) {
  container.innerHTML = `
    <div class="import-options">
      <label class="field" style="margin:0"><span>Лист</span><select data-el="sheet"></select></label>
      <label class="field" style="margin:0"><span>Строка с заголовками (0 — нет)</span><input data-el="header" type="number" min="0" value="1"></label>
      <label class="field" style="margin:0"><span>Какие строки брать</span><input data-el="spec" placeholder="например: 2-50, 55, 60-"></label>
    </div>
    <p class="small muted" style="margin:0 0 10px">
      Над каждой колонкой выберите, что в ней лежит. Строки отмечаются галочками (Shift+клик — диапазон) или диапазоном в поле выше.
      ${openEnded ? "<b>Для поставщика лучше диапазон с открытым концом (например <code>2-</code>): тогда новые строки прайса тоже попадут в обновление.</b>" : ""}
      <span data-el="info"></span>
    </p>
    <div class="grid-wrap"><table class="grid" data-el="grid"></table></div>
    <div class="small muted" data-el="selected" style="margin-top:8px"></div>`;
  const el = (name) => container.querySelector(`[data-el=${name}]`);
  const g = {
    token: null, sheet: "", rows: [], totalRows: 0, letters: [], headers: [], mapping: {},
    mappingTouched: false, specTouched: false, selected: new Set(), imageRows: {}, lastClicked: null, shown: 200,
  };

  const headerRow = () => Number(el("header").value) || 0;

  function defaultSpec() {
    const h = headerRow();
    if (g.totalRows <= h) return "";
    return openEnded ? `${h + 1}-` : `${h + 1}-${g.totalRows}`;
  }

  function syncSelection() {
    try {
      g.selected = parseRowSpec(el("spec").value, g.totalRows);
      el("spec").style.borderColor = "";
    } catch {
      el("spec").style.borderColor = "var(--err)";
      return false;
    }
    g.selected.delete(headerRow());
    return true;
  }

  async function loadSheet(sheet) {
    g.sheet = sheet;
    const data = await api(`/api/import/${g.token}/sheet?${new URLSearchParams({ sheet, header_row: headerRow() })}`);
    g.rows = data.rows;
    g.totalRows = data.total_rows;
    g.letters = data.letters;
    g.headers = data.headers;
    g.imageRows = data.image_rows;
    g.shown = 200;
    if (!g.mappingTouched) g.mapping = data.mapping;
    if (!g.specTouched) el("spec").value = defaultSpec();
    syncSelection();
    el("info").textContent = g.totalRows > g.rows.length
      ? `Показаны первые ${g.rows.length} из ${g.totalRows} строк — диапазон в поле выше работает для всех.` : "";
    render();
    onChange();
  }

  function targetOptions(letter, idx) {
    const current = g.mapping[letter] || "";
    return Object.entries(META.targets).map(([k, label]) => {
      if (k === "param") label = `Характеристика: ${(g.headers[idx] || "колонка " + letter).replace(/^param:/, "")}`;
      return `<option value="${k}" ${k === current ? "selected" : ""}>${esc(label)}</option>`;
    }).join("");
  }

  function render() {
    const h = headerRow();
    const mappedIdx = new Set(g.letters.map((l, i) => (g.mapping[l] ? i : -1)).filter((i) => i >= 0));
    let html = `<thead><tr><th class="rn">№</th>${g.letters.map((l) => `<th>${l}</th>`).join("")}</tr>
      <tr class="map"><th class="rn"></th>${g.letters.map((l, i) =>
        `<th><select data-col="${l}" class="${g.mapping[l] ? "mapped" : ""}">${targetOptions(l, i)}</select></th>`).join("")}</tr></thead><tbody>`;
    // на большом прайсе таблица в тысячи строк тормозит: показываем частями, диапазон строк работает для всех
    const cell = (v) => (v.length > 120 ? v.slice(0, 120) + "…" : v);
    g.rows.slice(0, g.shown).forEach((row, i) => {
      const n = i + 1;
      const isHeader = n === h;
      const on = g.selected.has(n);
      const pics = g.imageRows[String(n)];
      html += `<tr class="${isHeader ? "header" : on ? "on" : "off"}" data-row="${n}">
        <td class="rn">${isHeader ? `заголовок ${n}` : `<label>${pics ? `<span title="картинок в строке: ${pics}">📷</span>` : ""}${n}<input type="checkbox" ${on ? "checked" : ""}></label>`}</td>
        ${row.map((v, ci) => `<td class="${mappedIdx.has(ci) ? "mapped-col" : ""}" title="${esc(v.slice(0, 500))}">${esc(cell(v))}</td>`).join("")}
      </tr>`;
    });
    const rest = g.rows.length - g.shown;
    if (rest > 0) {
      html += `<tr><td class="rn"></td><td colspan="${g.letters.length}">
        <button type="button" class="btn small" data-more="1">Показать ещё ${Math.min(rest, 300)} строк</button>
        <span class="small muted">показано ${g.shown} из ${g.totalRows} — галочки и диапазон выше работают для всех строк</span></td></tr>`;
    }
    el("grid").innerHTML = html + "</tbody>";
    const mapped = Object.values(g.mapping).filter(Boolean);
    const missing = required
      .filter((r) => (Array.isArray(r) ? !r.some((x) => mapped.includes(x)) : !mapped.includes(r)))
      .map((r) => (Array.isArray(r) ? r.map((x) => META.targets[x]).join(" или ") : META.targets[r]));
    el("selected").innerHTML = `строк выбрано: <b>${g.selected.size}</b>` +
      (missing.length ? ` · <span style="color:var(--err)">не выбраны колонки: ${esc(missing.join("; "))}</span>` : "");
  }

  el("grid").addEventListener("change", (e) => {
    const sel = e.target.closest("select[data-col]");
    if (!sel) return;
    const target = sel.value;
    // одно поле — одна колонка (кроме характеристик и фото)
    if (target && !["param", "images"].includes(target)) {
      for (const [l, t] of Object.entries(g.mapping)) if (t === target && l !== sel.dataset.col) g.mapping[l] = "";
    }
    g.mapping[sel.dataset.col] = target;
    g.mappingTouched = true;
    render();
    onChange();
  });

  el("grid").addEventListener("click", (e) => {
    if (e.target.closest("[data-more]")) {
      g.shown += 300;
      render();
      return;
    }
    const box = e.target.closest("td.rn input[type=checkbox]");
    if (!box) return;
    const n = Number(box.closest("tr").dataset.row);
    const h = headerRow();
    if (e.shiftKey && g.lastClicked) {
      const [a, b] = [Math.min(n, g.lastClicked), Math.max(n, g.lastClicked)];
      for (let i = a; i <= b; i++) if (i !== h) box.checked ? g.selected.add(i) : g.selected.delete(i);
    } else {
      box.checked ? g.selected.add(n) : g.selected.delete(n);
    }
    g.lastClicked = n;
    el("spec").value = rowSpecFromSet(g.selected);
    g.specTouched = true;
    render();
    onChange();
  });

  let specTimer = null;
  el("spec").addEventListener("input", () => {
    g.specTouched = true;
    clearTimeout(specTimer);
    specTimer = setTimeout(() => { if (syncSelection()) { render(); onChange(); } }, 300);
  });
  el("header").addEventListener("change", () => loadSheet(g.sheet).catch((err) => toast(err.message, "error")));
  el("sheet").addEventListener("change", (e) => {
    g.mappingTouched = false;
    g.specTouched = false;
    loadSheet(e.target.value).catch((err) => toast(err.message, "error"));
  });

  return {
    // preset — сохранённые настройки поставщика: {sheet, header_row, rows, mapping}
    async open(token, sheets, preset = null) {
      g.token = token;
      el("sheet").innerHTML = sheets.map((s) => `<option>${esc(s)}</option>`).join("");
      g.mappingTouched = false;
      g.specTouched = false;
      let sheet = sheets[0];
      if (preset) {
        if (preset.sheet && sheets.includes(preset.sheet)) sheet = preset.sheet;
        el("header").value = preset.header_row ?? 1;
        if (preset.mapping && Object.keys(preset.mapping).length) {
          g.mapping = { ...preset.mapping };
          g.mappingTouched = true;
        }
        if (preset.rows) {
          el("spec").value = preset.rows;
          g.specTouched = true;
        }
      }
      el("sheet").value = sheet;
      await loadSheet(sheet);
    },
    body() {
      return { sheet: g.sheet, header_row: headerRow(), rows: el("spec").value.trim() || defaultSpec(), mapping: { ...g.mapping } };
    },
    get token() { return g.token; },
    get sheet() { return g.sheet; },
    get selectedCount() { return g.selected.size; },
    get embeddedCount() { return Object.values(g.imageRows).reduce((a, b) => a + b, 0); },
  };
}

// Плитки предпросмотра с ошибками и предупреждениями по строкам.
function renderItemsPreview(box, items, { token, sheet, onlyBad = false, limit = 60 } = {}) {
  // на большом прайсе сотни карточек с фото поставщика тормозят: сначала ошибки, дальше — частями
  const list = items.filter((i) => !onlyBad || i.errors.length)
    .sort((a, b) => (b.errors.length > 0) - (a.errors.length > 0));
  const shown = list.slice(0, limit);
  box.innerHTML = shown.map((i) => {
    const embedded = Array.from({ length: i.embedded_images }, (_, n) => ({
      src: `/api/import/${token}/image?${new URLSearchParams({ sheet, row: i.row, n })}`,
    }));
    const p = { ...i.data, params: i.params, images: [...embedded, ...i.image_urls.map((src) => ({ src }))] };
    const checks = [
      ...i.errors.map((t) => `<li class="err">${esc(t)}</li>`),
      ...i.warnings.map((t) => `<li class="warn">${esc(t)}</li>`),
    ].join("");
    const cost = i.data.cost_price != null ? ` · закупка ${esc(formatPrice(i.data.cost_price, i.data.currency))}` : "";
    return `<div>
      <div class="row-no"><span>строка ${i.row}${i.existing_id ? " · обновит существующий" : ""}${cost}</span>
        <span>${i.embedded_images ? `📷 из Excel: ${i.embedded_images}` : ""}</span></div>
      <div class="${i.errors.length ? "bad" : ""}" style="border-radius:10px">${renderTile(p)}</div>
      ${checks ? `<ul class="check-list" style="margin-top:6px">${checks}</ul>` : ""}
    </div>`;
  }).join("") + (list.length > shown.length
    ? `<div style="grid-column:1/-1;text-align:center"><button type="button" class="btn" data-more-cards>
         Показать ещё ${Math.min(60, list.length - shown.length)} из ${list.length - shown.length}</button></div>` : "");
  const more = box.querySelector("[data-more-cards]");
  if (more) more.onclick = () => renderItemsPreview(box, items, { token, sheet, onlyBad, limit: limit + 60 });
  box.querySelectorAll("img").forEach((img) => { img.loading = "lazy"; });
}
