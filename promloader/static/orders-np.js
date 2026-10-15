// «Заказы» → Новая Почта: номер ТТН и статус посылки у заказа, «📦 Создать ТТН» (форма из данных заказа Prom),
// этикетка 100×100, удаление ТТН до отправки. Имена, адреса и номера — данные (translate="no").
// Подключается до основного скрипта страницы: orderCard() вызывает shipmentBlock() уже при первой отрисовке.

const NP_STAGE_CLASS = { created: "draft", on_the_way: "sending", arrived: "ready", received: "synced",
                         refused: "error", failed: "error", deleted: "draft", not_found: "draft" };
let ttnOrder = null;
let ttnDraft = null;

function shipmentBlock(o) {
  const s = o.shipment;
  if (s) {
    const label = s.own ? `<button class="btn small" data-ttn-label="${o.id}">🖨 Этикетка 100×100</button>` : "";
    const del = s.own && s.stage === "created" ? `<button class="btn small danger" data-ttn-delete="${o.id}">Удалить ТТН</button>` : "";
    return `<div class="toolbar ttn-row" style="margin:10px 0 0">
      <span>🚚 ТТН <b translate="no">${esc(s.ttn)}</b></span>
      <span class="badge ${NP_STAGE_CLASS[s.stage] || "draft"}" title="${esc(s.status)}">${esc(s.stage_label || I18N.tr("Ждёт проверки"))}</span>
      ${label}${del}
      ${s.prom_error ? `<span class="small err-text">${esc(s.prom_error)}</span>` : ""}
    </div>`;
  }
  if (o.prom_ttn) return `<div class="small" style="margin-top:8px">🚚 ТТН <b translate="no">${esc(o.prom_ttn)}</b></div>`;
  if (o.nova_poshta && !["canceled", "delivered"].includes(o.status)) {
    return `<div class="toolbar" style="margin:10px 0 0"><button class="btn small primary" data-ttn-create="${o.id}">📦 Создать ТТН</button></div>`;
  }
  return "";
}

function handleTtnClick(e) {
  const create = e.target.closest("[data-ttn-create]");
  if (create) { busy(create, () => openTtn(Number(create.dataset.ttnCreate))); return true; }
  const label = e.target.closest("[data-ttn-label]");
  if (label) { window.open(`/api/orders/${label.dataset.ttnLabel}/ttn/label.pdf`, "_blank"); return true; }
  const del = e.target.closest("[data-ttn-delete]");
  if (del) {
    busy(del, async () => {
      if (!confirm(`Удалить ТТН у заказа №${del.dataset.ttnDelete}? Посылка ещё не отправлена — удалить можно.`)) return;
      try { await api(`/api/orders/${del.dataset.ttnDelete}/ttn`, { method: "DELETE" }); toast(I18N.tr("ТТН удалена"), "ok"); await load(); }
      catch (err) { toast(err.message, "error"); }
    });
    return true;
  }
  return false;
}

async function openTtn(id) {
  try { ttnDraft = await api(`/api/orders/${id}/ttn/draft`); } catch (err) { toast(err.message, "error"); return; }
  ttnOrder = id;
  const d = ttnDraft;
  $("#ttn-order").textContent = id;
  $("#ttn-not-ready").classList.toggle("hidden", d.ready);
  $("#ttn-last").value = d.last_name; $("#ttn-first").value = d.first_name; $("#ttn-middle").value = d.middle_name;
  $("#ttn-phone").value = d.phone;
  $("#ttn-city").value = d.city; $("#ttn-city").dataset.ref = d.city_ref;
  $("#ttn-hint").textContent = d.address_hint ? `(${I18N.tr("в заказе")}: ${d.address_hint})` : "";
  $("#ttn-weight").value = d.weight; $("#ttn-cost").value = d.cost; $("#ttn-desc").value = d.description;
  $("#ttn-payer").innerHTML = Object.entries(d.payers).map(([k, v]) => `<option value="${k}" ${k === d.payer ? "selected" : ""}>${esc(v)}</option>`).join("");
  $("#ttn-cod").checked = d.cod; $("#ttn-cod-amount").value = d.cod_amount || d.cost;
  showCod();
  $("#ttn-warehouse").innerHTML = d.warehouse_ref ? `<option value="${esc(d.warehouse_ref)}">${esc(d.warehouse)}</option>` : "";
  if (d.city_ref) loadWarehouses(d.city_ref, d.warehouse_ref);
  $("#ttn-modal").classList.remove("hidden");
}

function showCod() {
  $("#ttn-cod-wrap").classList.toggle("hidden", !$("#ttn-cod").checked);
  $("#ttn-cod-warn").classList.toggle("hidden", !(ttnDraft && ttnDraft.cod && !$("#ttn-cod").checked));
}
$("#ttn-cod").onchange = showCod;

async function loadWarehouses(cityRef, selected = "") {
  try {
    const r = await api(`/api/np/warehouses?${new URLSearchParams({ city_ref: cityRef })}`);
    $("#ttn-warehouse").innerHTML = r.items.map((w) => `<option value="${esc(w.ref)}" ${w.ref === selected ? "selected" : ""}>${esc(w.name)}</option>`).join("");
  } catch (err) { toast(err.message, "error"); }
}

let cityTimer = null;
let cityOptions = [];
$("#ttn-city").addEventListener("input", (e) => {
  clearTimeout(cityTimer);
  const q = e.target.value;
  const hit = cityOptions.find((c) => c.name === q);
  if (hit) { e.target.dataset.ref = hit.ref; loadWarehouses(hit.ref); return; }
  e.target.dataset.ref = "";
  cityTimer = setTimeout(async () => {
    try {
      cityOptions = (await api(`/api/np/cities?${new URLSearchParams({ q })}`)).items;
      $("#ttn-cities").innerHTML = cityOptions.map((c) => `<option value="${esc(c.name)}">${esc(c.area)}</option>`).join("");
    } catch (err) { toast(err.message, "error"); }
  }, 300);
});

$("#ttn-close").onclick = () => $("#ttn-modal").classList.add("hidden");
$("#ttn-go").onclick = () => busy($("#ttn-go"), async () => {
  const form = {
    last_name: $("#ttn-last").value, first_name: $("#ttn-first").value, middle_name: $("#ttn-middle").value,
    phone: $("#ttn-phone").value, city_ref: $("#ttn-city").dataset.ref || "", warehouse_ref: $("#ttn-warehouse").value,
    weight: $("#ttn-weight").value.replace(",", "."), cost: $("#ttn-cost").value.replace(",", "."),
    description: $("#ttn-desc").value, payer: $("#ttn-payer").value, cod: $("#ttn-cod").checked,
    cod_amount: $("#ttn-cod-amount").value.replace(",", "."),
  };
  if (!confirm(`Создать ТТН для заказа №${ttnOrder}? Покупатель получит SMS от Новой Почты.`)) return;
  try {
    const r = await api(`/api/orders/${ttnOrder}/ttn`, { method: "POST", json: form });
    $("#ttn-modal").classList.add("hidden");
    toast(`${I18N.tr("ТТН создана:")} ${r.ttn}`, "ok");
    if (r.prom_error) toast(r.prom_error, "error");
    await load();
  } catch (err) { toast(err.message, "error"); }
});
