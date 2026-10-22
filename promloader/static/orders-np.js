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
    // посылка на отделении: сколько дней ждёт и «🔔 Напомнить в чат» (раз в сутки, отметка — у всех операторов)
    const waiting = s.stage === "arrived" && s.days_waiting !== null && s.days_waiting !== undefined
      ? `<span class="small ${s.days_waiting >= 3 ? "err-text" : "muted"}">${esc(I18N.tr(`ждёт на отделении: ${s.days_waiting} дн.`))}</span>` : "";
    const remind = s.stage !== "arrived" ? ""
      : s.can_remind ? `<button class="btn small" data-remind="${o.id}" data-perm="chat">🔔 Напомнить в чат</button>`
      : `<span class="small muted" title="${esc(formatDate(s.reminded_at))}">${esc(I18N.tr("🔔 Напомнили"))} ${esc(formatDate(s.reminded_at))}</span>`;
    return `<div class="toolbar ttn-row" style="margin:10px 0 0">
      <span>🚚 ТТН <b translate="no">${esc(s.ttn)}</b></span>
      <span class="badge ${NP_STAGE_CLASS[s.stage] || "draft"}" title="${esc(s.status)}">${esc(s.stage_label || I18N.tr("Ждёт проверки"))}</span>
      ${waiting}${remind}${label}${del}
      ${s.prom_error ? `<span class="small err-text">${esc(s.prom_error)}</span>` : ""}
    </div>`;
  }
  if (o.prom_ttn) return `<div class="small" style="margin-top:8px">🚚 ТТН <b translate="no">${esc(o.prom_ttn)}</b></div>`;
  return "";
}

// «📦 Создать ТТН» — в общей строке действий заказа (вместе с «Упакован» и сменой статуса), а не отдельной строкой
function ttnCreateButton(o) {
  if (o.shipment || o.prom_ttn || !o.nova_poshta || ["canceled", "delivered"].includes(o.status)) return "";
  return `<button class="btn small primary" data-ttn-create="${o.id}">📦 Создать ТТН</button>`;
}

// ⚠️ у покупателя были отказы от посылок или отмены — спросите предоплату вместо наложенного платежа
function buyerBadge(b) {
  if (!b || !b.risky) return "";
  const parts = [];
  if (b.refused) parts.push(I18N.tr(`⚠️ Отказ от посылки в прошлом (${b.refused})`));
  if (b.canceled >= 2) parts.push(I18N.tr(`⚠️ Отменял заказы (${b.canceled})`));
  const title = I18N.tr(`Заказов с этого телефона: ${b.orders}, выполнено: ${b.delivered}, отменено: ${b.canceled}, отказов от посылок: ${b.refused}. Лучше попросить предоплату вместо наложенного платежа.`);
  return `<div class="buyer-risk" title="${esc(title)}">${parts.map((t) => `<span>${esc(t)}</span>`).join(" ")}</div>`;
}

// Пометка «нужна предоплата» у заказа рискового покупателя + «💬 Запросить предоплату в чат Prom»
const PREPAY_ACTIVE = ["pending", "received", "paid"];
function prepayBlock(o) {
  const b = o.buyer;
  const flagged = ["required", "requested"].includes(o.prepay_state);
  if (!(flagged || (b && b.risky)) || !PREPAY_ACTIVE.includes(o.status)) return "";
  const asked = o.prepay_requested_at
    ? `<span class="small muted">💬 ${esc(I18N.tr("Предоплату попросили"))} ${esc(formatDate(o.prepay_requested_at))}</span>`
    : `<button class="btn small" data-prepay-request="${o.id}" data-perm="chat">💬 ${esc(I18N.tr("Запросить предоплату в чат Prom"))}</button>`;
  // одна плашка вместо двух: заголовок с числами отказов и отмен (или значки истории) и кнопки
  return `<div class="risky-banner">${flagged && b ? `<b>${esc(I18N.tr(`⚠️ Рисковый покупатель (отказов: ${b.refused}, отмен: ${b.canceled}) — рекомендуется предоплата`))}</b>` : buyerBadge(b)}
    <span class="toolbar" style="margin:0">${asked}
    ${flagged ? `<button class="btn small" data-prepay-ok="${o.id}" data-perm="orders">${esc(I18N.tr("Предоплата получена / снять пометку"))}</button>` : ""}</span></div>`;
}

async function prepayAction(btn) {
  try {
    if (btn.dataset.prepayRequest) {
      await api(`/api/orders/${btn.dataset.prepayRequest}/prepay-request`, { method: "POST" });
      toast(I18N.tr("Просьба о предоплате отправлена покупателю в чат Prom"), "ok");
    } else {
      await api(`/api/orders/${btn.dataset.prepayOk}/prepay`, { method: "POST", json: { state: "ok" } });
      toast(I18N.tr("Пометка снята"), "ok");
    }
  } catch (err) { toast(err.message, "error"); }
  await load();
}

async function remindBuyer(btn) {
  try {
    await api(`/api/orders/${btn.dataset.remind}/remind`, { method: "POST" });
    toast(I18N.tr("Напоминание отправлено покупателю в чат Prom"), "ok");
  } catch (err) { toast(err.message, "error"); }
  await load();
}

function handleTtnClick(e) {
  const prepay = e.target.closest("[data-prepay-request], [data-prepay-ok]");
  if (prepay) { busy(prepay, () => prepayAction(prepay)); return true; }
  const remind = e.target.closest("[data-remind]");
  if (remind) { busy(remind, () => remindBuyer(remind)); return true; }
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
  $("#ttn-buyer").innerHTML = buyerBadge(d.buyer);
  $("#ttn-not-ready").classList.toggle("hidden", d.ready);
  $("#ttn-last").value = d.last_name; $("#ttn-first").value = d.first_name; $("#ttn-middle").value = d.middle_name;
  $("#ttn-phone").value = d.phone;
  $("#ttn-city").value = d.city; $("#ttn-city").dataset.ref = d.city_ref;
  $("#ttn-hint").textContent = d.address_hint ? `(${I18N.tr("в заказе")}: ${d.address_hint})` : "";
  $("#ttn-weight").value = d.weight; $("#ttn-cost").value = d.cost; $("#ttn-desc").value = d.description;
  $("#ttn-payer").innerHTML = Object.entries(d.payers).map(([k, v]) => `<option value="${k}" ${k === d.payer ? "selected" : ""}>${esc(v)}</option>`).join("");
  $("#ttn-cod").checked = d.cod; $("#ttn-cod-amount").value = d.cod_amount || d.cost;
  // «отправить номер в чат» — по умолчанию выключено, выбор запоминается в этом браузере
  try { $("#ttn-notify").checked = localStorage.getItem("promloader-ttn-notify") === "1"; } catch { $("#ttn-notify").checked = false; }
  // статус заказа после ТТН — по умолчанию «не менять», выбор запоминается в этом браузере
  try { $("#ttn-status").value = localStorage.getItem("promloader-ttn-status") || ""; } catch { $("#ttn-status").value = ""; }
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
    cod_amount: $("#ttn-cod-amount").value.replace(",", "."), notify_buyer: $("#ttn-notify").checked,
    set_status: $("#ttn-status").value,
  };
  try {
    localStorage.setItem("promloader-ttn-notify", form.notify_buyer ? "1" : "");
    localStorage.setItem("promloader-ttn-status", form.set_status);
  } catch {}
  if (!confirm(`Создать ТТН для заказа №${ttnOrder}? Покупатель получит SMS от Новой Почты.`)) return;
  try {
    let r;
    try {
      r = await api(`/api/orders/${ttnOrder}/ttn`, { method: "POST", json: form });
    } catch (err) {
      // рисковый покупатель и наложенный платёж: только после явного «да»
      if (!(err.status === 409 && err.body.risky)) throw err;
      if (!confirm(`${err.message}.\n\n${I18N.tr("Всё равно отправить наложенным платежом?")}`)) return;
      r = await api(`/api/orders/${ttnOrder}/ttn`, { method: "POST", json: { ...form, confirm_risky: true } });
    }
    $("#ttn-modal").classList.add("hidden");
    toast(`${I18N.tr("ТТН создана:")} ${r.ttn}`, "ok");
    if (r.prom_error) toast(r.prom_error, "error");
    if (r.chat_sent) toast(I18N.tr("Номер ТТН отправлен покупателю в чат Prom"), "ok");
    else if (r.chat_error) toast(r.chat_error, "error");
    if (r.status_error) toast(r.status_error, "error");
    await load();
  } catch (err) { toast(err.message, "error"); }
});
