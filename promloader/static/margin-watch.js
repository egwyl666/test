// «Наценка» → «Сторож наценки»: товары в продаже в минус или ниже минимальной наценки, пересчёт по правилу.
// Названия товаров — данные (translate="no").

function renderWatch(r) {
  $("#w-min").value = r.min_margin_pct;
  $("#w-recalc").classList.toggle("hidden", !r.items.length);
  if (!r.items.length) {
    $("#w-list").innerHTML = `<p class="small muted">${esc(I18N.tr("Всё в порядке: товаров в минус или ниже минимальной наценки нет."))}</p>`;
    return;
  }
  $("#w-list").innerHTML = `<div class="table-scroll"><table class="list report-table">
    <thead><tr><th><input type="checkbox" id="w-all" checked></th><th>${esc(I18N.tr("Товар"))}</th>
      <th class="num">${esc(I18N.tr("Цена"))}</th><th class="num">${esc(I18N.tr("Закупка, грн"))}</th>
      <th class="num">${esc(I18N.tr("Наценка"))}</th></tr></thead>
    <tbody>${r.items.map((x) => `<tr>
      <td><input type="checkbox" class="w-sel" value="${x.id}" checked></td>
      <td><a href="/product?id=${x.id}" translate="no">${esc(x.name)}</a>
        <div class="small muted"><span translate="no">${esc(x.external_id || "")}</span>${x.price_locked ? ` · ${esc(I18N.tr("🔒 цена вручную"))}` : ""}</div></td>
      <td class="num nowrap">${esc(formatPrice(x.price, "UAH"))}</td>
      <td class="num nowrap">${esc(formatPrice(x.cost_uah, "UAH"))}</td>
      <td class="num nowrap ${x.loss ? "err-text" : ""}">${esc(formatPrice(x.margin_uah, "UAH"))} (${x.margin_pct ?? "—"}%)</td>
    </tr>`).join("")}</tbody></table></div>`;
}

async function loadWatch() {
  try { renderWatch(await api("/api/pricing/low-margin")); } catch (err) { if (!err.network) toast(err.message, "error"); }
}

$("#w-list").addEventListener("change", (e) => {
  if (e.target.id === "w-all") $$(".w-sel").forEach((x) => { x.checked = e.target.checked; });
});
$("#w-save").onclick = () => busy($("#w-save"), async () => {
  try {
    renderWatch(await api("/api/pricing/min-margin", { method: "POST", json: { value: Number($("#w-min").value.replace(",", ".")) } }));
    toast(I18N.tr("Сохранено"), "ok");
  } catch (err) { toast(err.message, "error"); }
});
$("#w-recalc").onclick = () => busy($("#w-recalc"), async () => {
  const ids = $$(".w-sel").filter((x) => x.checked).map((x) => Number(x.value));
  if (!ids.length) return;
  if (!confirm(`Пересчитать цену по правилу наценки у товаров: ${ids.length}? Ручное закрепление цены будет снято.`)) return;
  try {
    await api("/api/products/prices", { method: "POST", json: { action: "recalc", ids } });
    toast(I18N.tr("Цены пересчитаны"), "ok");
    await loadWatch();
  } catch (err) { toast(err.message, "error"); }
});

loadWatch();
