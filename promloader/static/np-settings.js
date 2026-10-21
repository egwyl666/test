// «Настройки» → «Новая Почта»: ключ API, отправитель, город и отделение отправки, значения по умолчанию для ТТН.

let npCities = [];

async function npLoadWarehouses(cityRef, selected = "") {
  if (!cityRef) { $("#np-warehouse").innerHTML = ""; return; }
  try {
    const r = await api(`/api/np/warehouses?${new URLSearchParams({ city_ref: cityRef })}`);
    $("#np-warehouse").innerHTML = r.items.map((w) => `<option value="${esc(w.ref)}" ${w.ref === selected ? "selected" : ""}>${esc(w.name)}</option>`).join("");
  } catch (err) { toast(err.message, "error"); }
}

function npFill(s, contacts) {
  $("#np-key").value = "";
  $("#np-key-state").textContent = s.key_set ? `(${I18N.tr("сейчас:")} ${s.key})` : I18N.tr("(не задан)");
  $("#np-sender").textContent = s.np_sender_name ? `${I18N.tr("Отправитель:")} ${s.np_sender_name}${s.ready ? " ✓" : ""}` : "";
  $("#np-form").classList.toggle("hidden", !s.key_set);
  if (contacts) {
    $("#np-contact").innerHTML = contacts.map((c) =>
      `<option value="${esc(c.ref)}" data-phone="${esc(c.phone)}" ${c.ref === s.np_contact_ref ? "selected" : ""}>${esc(c.name)}</option>`).join("");
    if (!s.np_sender_phone && contacts[0]) $("#np-phone").value = contacts[0].phone;
  } else if (s.np_contact_ref) {
    $("#np-contact").innerHTML = `<option value="${esc(s.np_contact_ref)}">${esc(s.np_contact_name)}</option>`;
  }
  if (s.np_sender_phone) $("#np-phone").value = s.np_sender_phone;
  $("#np-city").value = s.np_city_name; $("#np-city").dataset.ref = s.np_city_ref;
  $("#np-warehouse").innerHTML = s.np_warehouse_ref ? `<option value="${esc(s.np_warehouse_ref)}">${esc(s.np_warehouse_name)}</option>` : "";
  $("#np-weight").value = s.np_weight; $("#np-desc").value = s.np_description; $("#np-days").value = s.np_storage_days;
  $("#np-risky").checked = !!s.auto_flag_risky_cod;
  $("#np-payer").innerHTML = Object.entries(s.payers).map(([k, v]) => `<option value="${k}" ${k === s.np_payer ? "selected" : ""}>${esc(v)}</option>`).join("");
}

async function npLoad() {
  try { npFill(await api("/api/np/settings")); } catch (err) { if (!err.network) toast(err.message, "error"); }
}

$("#np-check").onclick = () => busy($("#np-check"), async () => {
  try {
    const r = await api("/api/np/check", { method: "POST", json: { key: $("#np-key").value } });
    npFill(r, r.contacts);
    toast(I18N.tr("Ключ работает"), "ok");
  } catch (err) { toast(err.message, "error"); }
});

$("#np-contact").onchange = (e) => {
  const phone = e.target.selectedOptions[0]?.dataset.phone;
  if (phone) $("#np-phone").value = phone;
};

let npTimer = null;
$("#np-city").addEventListener("input", (e) => {
  clearTimeout(npTimer);
  const hit = npCities.find((c) => c.name === e.target.value);
  if (hit) { e.target.dataset.ref = hit.ref; npLoadWarehouses(hit.ref); return; }
  e.target.dataset.ref = "";
  npTimer = setTimeout(async () => {
    try {
      npCities = (await api(`/api/np/cities?${new URLSearchParams({ q: e.target.value })}`)).items;
      $("#np-cities").innerHTML = npCities.map((c) => `<option value="${esc(c.name)}">${esc(c.area)}</option>`).join("");
    } catch (err) { toast(err.message, "error"); }
  }, 300);
});

$("#np-save").onclick = () => busy($("#np-save"), async () => {
  const contact = $("#np-contact").selectedOptions[0];
  const wh = $("#np-warehouse").selectedOptions[0];
  try {
    npFill(await api("/api/np/settings", { method: "POST", json: {
      np_contact_ref: $("#np-contact").value, np_contact_name: contact ? contact.textContent : "",
      np_sender_phone: $("#np-phone").value, np_city_ref: $("#np-city").dataset.ref || "", np_city_name: $("#np-city").value,
      np_warehouse_ref: $("#np-warehouse").value, np_warehouse_name: wh ? wh.textContent : "",
      np_weight: $("#np-weight").value, np_description: $("#np-desc").value, np_payer: $("#np-payer").value,
      np_storage_days: $("#np-days").value } }));
    toast(I18N.tr("Сохранено"), "ok");
  } catch (err) { toast(err.message, "error"); }
});

$("#np-risky").onchange = async (e) => {
  try {
    await api("/api/np/settings", { method: "POST", json: { auto_flag_risky_cod: e.target.checked } });
    toast(I18N.tr("Сохранено"), "ok");
  } catch (err) { toast(err.message, "error"); e.target.checked = !e.target.checked; }
};

npLoad();
