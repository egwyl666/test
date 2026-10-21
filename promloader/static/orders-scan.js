// «Заказы» → склад: 📷 скан ТТН (штрихкод на этикетке Новой Почты) или номера заказа и «📦 Упакован».
// Обычный режим: скан открывает заказ (список фильтруется по нему). Режим упаковки: каждый скан сразу отмечает заказ
// упакованным, а камера остаётся открытой для следующей посылки; отменённый, уже упакованный, рисковый — видно сразу.
// Подключается после orders-np.js и до основного скрипта страницы (orderCard() вызывает packBlock()).

const PACK_ACTIVE = ["pending", "received", "paid"];

function packBlock(o) {
  if (o.packed_at) {
    return `<div class="toolbar" style="margin:8px 0 0"><span class="badge synced">📦 ${esc(I18N.tr("Упакован"))} ${esc(formatDate(o.packed_at))}${o.packed_by ? ` · <span translate="no">${esc(o.packed_by)}</span>` : ""}</span>
      <button class="btn small link" data-unpack="${o.id}" data-perm="orders">${esc(I18N.tr("снять отметку"))}</button></div>`;
  }
  if (!PACK_ACTIVE.includes(o.status)) return "";
  return `<div class="toolbar" style="margin:8px 0 0"><button class="btn small" data-pack="${o.id}" data-perm="orders">📦 ${esc(I18N.tr("Упакован"))}</button></div>`;
}

function handlePackClick(e) {
  const pack = e.target.closest("[data-pack]");
  if (pack) { busy(pack, () => markPacked(Number(pack.dataset.pack))); return true; }
  const unpack = e.target.closest("[data-unpack]");
  if (unpack) {
    busy(unpack, async () => {
      try { await api(`/api/orders/${unpack.dataset.unpack}/packed`, { method: "POST", json: { packed: false } }); }
      catch (err) { toast(err.message, "error"); }
      await load();
    });
    return true;
  }
  return false;
}

async function markPacked(id) {
  try {
    const r = await api(`/api/orders/${id}/packed`, { method: "POST", json: { packed: true } });
    showScanResult(r.order, { ok: true, warnings: r.warnings, packed: true });
  } catch (err) { toast(err.message, "error"); }
  await load();
}

// Итог скана над списком: зелёный — всё хорошо, жёлтый — упакован с предупреждением, красный — не упаковывать
function showScanResult(o, { ok = true, warnings = [], packed = false, error = "" } = {}) {
  const box = $("#scan-result");
  const kind = error ? "bad" : warnings.length ? "warn" : "good";
  const head = error ? esc(error)
    : packed ? `✅ ${esc(I18N.tr(`Заказ №${o.id} упакован`))}` : `🔎 ${esc(I18N.tr(`Найден заказ №${o.id}`))}`;
  const ship = o && o.shipment ? ` · 🚚 <span translate="no">${esc(o.shipment.ttn)}</span> · ${esc(I18N.tr(o.shipment.stage_label || ""))}` : "";
  box.className = `scan-result ${kind}`;
  box.innerHTML = `<div><b>${head}</b></div>
    ${o ? `<div class="small"><span class="badge ${STATUS_CLASS[o.status] || "draft"}">${esc(o.status_label)}</span>
      <span translate="no">${esc(o.client || "")}</span> ${esc(o.price || "")}${ship}</div>
      <div class="small" translate="no">${(o.items || []).slice(0, 6).map((i) => `• ${esc(i.name)} × ${esc(i.quantity)}`).join("<br>")}</div>` : ""}
    ${warnings.map((w) => `<div class="small">⚠️ ${esc(I18N.tr(w))}</div>`).join("")}
    <div class="toolbar" style="margin:6px 0 0">
      <button class="btn small primary" data-scan-next>📷 ${esc(I18N.tr("Сканировать следующий"))}</button>
      ${o && !packed && !o.packed_at && PACK_ACTIVE.includes(o.status) ? `<button class="btn small" data-pack="${o.id}" data-perm="orders">📦 ${esc(I18N.tr("Упакован"))}</button>` : ""}
      <button class="btn small" data-scan-close>${esc(I18N.tr("Скрыть"))}</button></div>`;
  box.classList.remove("hidden");
  box.scrollIntoView({ block: "nearest" });
}

async function onScanned(code, close) {
  const packing = $("#pack-mode").checked;
  let found;
  try {
    found = await api(`/api/orders/scan?${new URLSearchParams({ code })}`);
  } catch (err) {
    if (packing) showScanResult(null, { error: err.message });
    throw err;  // сообщение — в окне сканера, камера остаётся открытой
  }
  const o = found.order;
  // список — на этом заказе
  view.q = String(o.id);
  $("#q").value = view.q;
  if (!packing) {
    close();
    showScanResult(o);
    await reload();
    return;
  }
  try {
    const r = await api(`/api/orders/${o.id}/packed`, { method: "POST", json: { packed: true } });
    showScanResult(r.order, { warnings: r.warnings, packed: true });
    if (navigator.vibrate) navigator.vibrate(r.warnings.length ? [80, 80, 80] : 120);
    load().catch(() => {});
    // камера остаётся открытой — следующая посылка
    return { text: `✅ ${I18N.tr(`Заказ №${o.id} упакован`)}${r.warnings.length ? " · ⚠️ " + r.warnings.map((w) => I18N.tr(w)).join(" · ") : ""}`,
             warn: r.warnings.length > 0 };
  } catch (err) {
    showScanResult(o, { error: err.message });
    if (navigator.vibrate) navigator.vibrate([300, 100, 300]);
    load().catch(() => {});
    return { text: err.message, bad: true };
  }
}

function openOrderScanner() {
  const packing = $("#pack-mode").checked;
  openScanner({
    title: I18N.tr(packing ? "📦 Упаковка: сканируйте ТТН" : "Найти заказ"),
    placeholder: I18N.tr("ТТН или № заказа"),
    hint: I18N.tr(packing
      ? "Каждый скан сразу отмечает заказ «Упакован». Отменённый или уже упакованный заказ программа не отметит."
      : "Наведите камеру на штрихкод ТТН на этикетке Новой Почты или введите номер."),
    onCode: onScanned,
  });
}

$("#scan-btn").addEventListener("click", openOrderScanner);
$("#pack-mode").addEventListener("change", (e) => {
  try { localStorage.setItem("promloader-pack-mode", e.target.checked ? "1" : ""); } catch {}
});
try { $("#pack-mode").checked = localStorage.getItem("promloader-pack-mode") === "1"; } catch {}
$("#scan-result").addEventListener("click", (e) => {
  if (e.target.closest("[data-scan-next]")) { openOrderScanner(); return; }
  if (e.target.closest("[data-scan-close]")) { $("#scan-result").classList.add("hidden"); return; }
  handlePackClick(e);
});
