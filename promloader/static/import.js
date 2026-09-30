// Разовый импорт: файл -> строки и колонки -> проверка -> импорт.

const imp = { filename: "", items: [] };
const grid = createMappingGrid($("#mapping"));

function showStep(n) {
  [1, 2, 3].forEach((i) => $(`#step${i}`).classList.toggle("hidden", i !== n));
  $$(".steps .step").forEach((s) => {
    const i = Number(s.dataset.step);
    s.classList.toggle("active", i === n);
    s.classList.toggle("done", i < n);
  });
}

// ---------- шаг 1: файл ----------

const DROP_TEXT = $("#file-drop").innerHTML;

async function uploadFile(files) {
  const file = files[0];
  if (!file) return;
  if (/\.xls$/i.test(file.name)) {
    toast("Старый формат .xls: откройте файл в Excel и сохраните как .xlsx", "error");
    return;
  }
  const body = new FormData();
  body.append("file", file);
  $("#file-drop").innerHTML = "<b>Загружаю и читаю файл…</b>";
  try {
    const res = await api("/api/import/upload", { method: "POST", body });
    $("#filename").value = res.filename;
    await grid.open(res.token, res.sheets);
    const embedded = grid.embeddedCount;
    $("#embedded-wrap").classList.toggle("hidden", !embedded);
    $("#embedded-label").textContent = `прикреплять картинки, вставленные в Excel (найдено: ${embedded})`;
    showStep(2);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    $("#file-drop").innerHTML = DROP_TEXT;
  }
}

const isPriceFile = (f) => /\.(xlsx|xlsm|csv|xls|xml|yml)$/i.test(f.name);
onPageFileDrop((files) => { if (!$("#step1").classList.contains("hidden")) uploadFile(files); },
  { accept: isPriceFile, text: "Отпустите файл" });
$("#file-drop").addEventListener("click", async () =>
  uploadFile(await pickFiles({ accept: ".xlsx,.xlsm,.csv,.xml,.yml", multiple: false })));
$("#back-1").addEventListener("click", () => showStep(1));

// ---------- шаг 3: проверка ----------

function requestBody() {
  return {
    ...grid.body(),
    defaults: {
      group_name: $("#def-group").value,
      presence: $("#def-presence").value,
      currency: $("#def-currency").value,
    },
    use_embedded_images: $("#use-embedded").checked,
  };
}

$("#to-3").addEventListener("click", async () => {
  if (!grid.selectedCount) return toast("Не выбрано ни одной строки", "error");
  try {
    const res = await api(`/api/import/${grid.token}/preview`, { method: "POST", json: requestBody() });
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
  renderItemsPreview($("#preview"), items, { token: grid.token, sheet: grid.sheet, onlyBad: $("#only-bad").checked });
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
    const res = await api(`/api/import/${grid.token}/commit`, { method: "POST", json: body });
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
