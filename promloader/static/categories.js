// Страница «Категории Prom»: по каждой группе — какие категории каталога Prom стоят у её товаров и правило
// «группа → категория». Названия групп и категорий — данные пользователя и Prom, поэтому translate="no".

let data = null;
let picking = null;  // группа, для которой открыт выбор

function catName(c) {
  if (!c) return "";
  return c.caption || `№${c.id}`;
}

function catChip(c, extra = "") {
  const title = c.path ? ` title="${esc(c.path)}"` : "";
  return `<span class="chip cat-chip"${title}><span translate="no">${esc(catName(c))}</span>${extra}</span>`;
}

function listLink(group, params) {
  const p = new URLSearchParams({ group: group || "-", ...params });
  return `/?${p}`;
}

function render() {
  const groups = data.groups;
  $("#no-send").classList.toggle("hidden", data.send);
  $("#mismatch-box").classList.toggle("hidden", !data.mismatch_total);
  $("#mismatch-n").textContent = data.mismatch_total;
  renderFix(data.fix || {});
  $("#empty").classList.toggle("hidden", groups.length > 0);
  $("#groups").innerHTML = groups.map((g) => {
    const prom = g.prom.length
      ? g.prom.map((c) => catChip(c, ` <b>${c.count}</b>`)).join(" ")
      : `<span class="muted small">${g.on_prom ? "Prom ещё не сообщил — нажмите «↻ Обновить с Prom»" : "товаров группы на Prom ещё нет"}</span>`;
    const notes = [];
    if (g.split) notes.push(`<span class="badge error">Prom разнёс по ${g.prom.length} категориям</span>`);
    if (g.mismatch) notes.push(`<a class="badge error" href="${listLink(g.group, { cat_mismatch: 1 })}">не та категория: ${g.mismatch}</a>`);
    if (g.own) notes.push(`<span class="badge draft" title="У этих товаров категория выбрана в карточке (🔒) — правило группы их не меняет">🔒 своя категория: ${g.own}</span>`);
    return `<div class="panel cat-group">
      <div class="toolbar" style="margin:0 0 8px">
        <h3 style="margin:0"><a href="${listLink(g.group, {})}" translate="no">${esc(g.group || "Без группы")}</a></h3>
        <span class="muted small">товаров: ${g.count} · на Prom: ${g.on_prom}</span>
        <span class="spacer"></span>
        ${notes.join(" ")}
      </div>
      <div class="cat-line"><span class="muted small cat-label">Сейчас на Prom:</span> ${prom}</div>
      <div class="cat-line"><span class="muted small cat-label">Нужная категория:</span>
        ${g.rule ? catChip(g.rule) : `<span class="muted small">не задана — решает Prom</span>`}
        <button class="btn small" data-pick="${esc(g.group)}">${g.rule ? "Изменить" : "Выбрать"}</button>
        ${!g.rule && g.prom.length === 1 ? `<button class="btn small" data-keep="${esc(g.group)}" data-cat="${g.prom[0].id}" title="Закрепить категорию, которую уже поставил Prom">Оставить как на Prom</button>` : ""}
      </div>
    </div>`;
  }).join("");
}

async function load() {
  try {
    data = await api("/api/categories");
    $("#error").textContent = "";
    render();
  } catch (err) {
    $("#error").textContent = err.message;
  }
}

function optionsHtml(items) {
  return items.map((c) => `<button class="cat-option" data-choose="${c.id}">
      <b translate="no">${esc(catName(c))}</b>${c.path ? `<span class="small muted" translate="no">${esc(c.path)}</span>` : ""}
    </button>`).join("");
}

function openPick(group) {
  picking = group;
  $("#pick-group").textContent = group || "Без группы";
  $("#pick-q").value = group || "";
  $("#pick-url").value = "";
  $("#pick-found").innerHTML = "";
  $("#pick-known").innerHTML = data.known.length
    ? optionsHtml(data.known)
    : `<span class="muted small">Пока ни одной — нажмите «Найти» или загрузите каталог с Prom.</span>`;
  $("#pick-modal").classList.remove("hidden");
  $("#pick-q").focus();
}

// правило сохраняется только после окна «Что изменится»: оно может отправить на Prom сразу много товаров
async function choose(categoryId) {
  const group = picking;
  try {
    const p = await api("/api/categories/group/preview", { method: "POST", json: { group, category_id: categoryId } });
    const note = data.send
      ? "Товары группы, которые уже на Prom, станут «Готов к отправке» — категория уйдёт со следующей отправкой."
      : "Prom пока не принимает категорию из программы — она сохранится для расхождений и отчётов.";
    if (!await showPreview(categoryId ? "Категория для группы: что изменится" : "Убрать правило группы: что изменится",
      p, { applyLabel: "Сохранить", note })) return;
    const res = await api("/api/categories/group", { method: "POST", json: { group, category_id: categoryId } });
    $("#pick-modal").classList.add("hidden");
    toast(res.changed ? `Сохранено. Категория изменена у товаров: ${res.changed}` : "Сохранено", "ok");
    await load();
  } catch (err) { toast(err.message, "error"); }
}

$("#groups").addEventListener("click", (e) => {
  const pick = e.target.closest("[data-pick]");
  if (pick) { openPick(pick.dataset.pick); return; }
  const keep = e.target.closest("[data-keep]");
  if (keep) { picking = keep.dataset.keep; busy(keep, () => choose(Number(keep.dataset.cat))); }
});

$("#pick-modal").addEventListener("click", (e) => {
  const opt = e.target.closest("[data-choose]");
  if (opt) busy(opt, () => choose(Number(opt.dataset.choose)));
});

$("#pick-search").onclick = () => busy($("#pick-search"), async () => {
  const q = $("#pick-q").value.trim();
  if (!q) return;
  $("#pick-found").innerHTML = `<span class="muted small">Ищу на prom.ua…</span>`;
  try {
    const res = await api(`/api/categories/suggest?q=${encodeURIComponent(q)}`);
    $("#pick-found").innerHTML = res.items.length
      ? optionsHtml(res.items)
      : `<span class="muted small">prom.ua ничего не нашёл — попробуйте другие слова или ссылку на категорию.</span>`;
  } catch (err) { $("#pick-found").innerHTML = `<span class="err-text">${esc(err.message)}</span>`; }
});
$("#pick-q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#pick-search").click(); });

$("#pick-url-go").onclick = () => busy($("#pick-url-go"), async () => {
  const url = $("#pick-url").value.trim();
  if (!url) return;
  try {
    const c = await api("/api/categories/url", { method: "POST", json: { url } });
    $("#pick-found").innerHTML = optionsHtml([c]);
  } catch (err) { toast(err.message, "error"); }
});

$("#pick-clear").onclick = () => busy($("#pick-clear"), () => choose(null));
$("#pick-close").onclick = () => $("#pick-modal").classList.add("hidden");

$("#refresh").onclick = () => busy($("#refresh"), async () => {
  try {
    const r = await api("/api/categories/refresh", { method: "POST" });
    toast(`Категории с Prom обновлены: товаров на Prom ${r.seen}, изменилось ${r.updated}`, "ok");
    await load();
  } catch (err) { toast(err.message, "error"); }
});

loadMeta().catch(() => {});
load();

// «Исправить категории на Prom»: импорт идёт в фоне несколько минут — показываем ход и обновляем страницу в конце
let fixTimer = null;
function renderFix(st) {
  $("#fix-btn").disabled = !!st.running;
  $("#fix-state").innerHTML = st.running
    ? `<span class="badge sending">${esc(I18N.tr("Исправляю категории на Prom"))}</span> ${esc(st.stage || "")} · ${esc(I18N.tr("готово"))} ${st.done || 0} / ${st.total || "…"}`
    : st.error ? `<span class="err-text">${esc(`Категории исправлены не все: ${st.error}`)}</span>`
    : st.finished_at ? `<span style="color:var(--ok)">${esc(`✓ Категории на Prom исправлены: ${st.done || 0}`)}</span>` : "";
  clearTimeout(fixTimer);
  if (st.running) fixTimer = setTimeout(load, 5000);
}

$("#fix-btn").onclick = () => busy($("#fix-btn"), async () => {
  if (!confirm(`Исправить категорию на Prom у товаров: ${data.mismatch_total}? Prom обработает файл за несколько минут.`)) return;
  try {
    await api("/api/categories/fix", { method: "POST" });
    await load();
  } catch (err) { toast(err.message, "error"); }
});
