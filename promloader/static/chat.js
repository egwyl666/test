// Чат с покупателями Prom: слева диалоги, справа переписка, ответ, шаблоны и черновик ИИ.
// Имена покупателей, сообщения и названия товаров — данные, поэтому translate="no".

const view = { q: "", unread: false, room: new URLSearchParams(location.search).get("room") || "", seq: 0 };
let data = null;

function time(iso) { return iso ? formatDate(iso) : ""; }

function productCard(p) {
  if (!p) return "";
  return `<a class="chat-product" href="/product?id=${p.id}">
    ${p.thumb ? `<img src="${esc(p.thumb)}" alt="" loading="lazy">` : `<div class="noimg"></div>`}
    <span><b translate="no">${esc(p.name)}</b><br><span class="small">${esc(formatPrice(p.price, p.currency || "UAH"))} ·
      ${esc(META.presence[p.presence] || p.presence)}</span></span></a>`;
}

async function loadRooms() {
  const seq = ++view.seq;
  try {
    const r = await api(`/api/chat/rooms?${new URLSearchParams({ q: view.q, ...(view.unread ? { unread: "true" } : {}) })}`);
    if (seq !== view.seq) return;
    data = r;
    $("#chat-off").classList.toggle("hidden", r.enabled);
    // одной фразой: так словарь переведёт и её, и текст ошибки Prom внутри
    $("#error").textContent = r.error ? `Не удалось получить чат: ${r.error}` : "";
    $("#poll-info").textContent = r.last_poll ? `проверено ${time(r.last_poll)}` : "";
    const empty = $("#rooms > p");
    if (r.rooms.length && empty) empty.remove();
    // опрос раз в 30 с: меняются только диалоги, где что-то новое — список не мигает и не теряет наведение
    if (r.rooms.length) patchChildren($("#rooms"), r.rooms, (room) => room.room_ident, (room) => `
      <button class="chat-room-item ${room.room_ident === view.room ? "active" : ""}" data-room="${esc(room.room_ident)}">
        <span class="row" style="gap:6px;align-items:center">
          <b translate="no">${esc(room.buyer_name || I18N.tr("Покупатель"))}</b>
          ${room.unread ? `<span class="nav-badge">${room.unread}</span>` : ""}
          <span class="spacer"></span><span class="small muted">${esc(time(room.last_at))}</span></span>
        ${room.product ? `<span class="small muted" translate="no">${esc(room.product.name)}</span>` : ""}
        <span class="small chat-last" translate="no">${esc(room.last_message)}</span>
      </button>`);
    else $("#rooms").innerHTML = `<p class="muted small">${esc(I18N.tr(view.q || view.unread ? "Ничего не найдено." : "Сообщений от покупателей пока нет. Когда покупатель напишет магазину на Prom, диалог появится здесь сам."))}</p>`;
  } catch (err) { $("#error").textContent = err.message; }
}

async function openRoom(ident, { markRead = true } = {}) {
  view.room = ident;
  history.replaceState(null, "", ident ? `/chat?room=${encodeURIComponent(ident)}` : "/chat");
  $("#layout").classList.toggle("room-open", !!ident);
  $$(".chat-room-item").forEach((b) => b.classList.toggle("active", b.dataset.room === ident));
  if (!ident) return;
  try {
    const r = await api(`/api/chat/rooms/${encodeURIComponent(ident)}/messages`);
    if (view.room !== ident) return;
    renderRoom(r);
    // покупатель видит «прочитано» — отмечаем, только когда диалог действительно открыт и тем, кто отвечает в чате:
    // у роли без права «чат» сервер отказывал, и каждое открытие диалога показывало ошибку
    if (markRead && r.room.unread && can("chat")) {
      api(`/api/chat/rooms/${encodeURIComponent(ident)}/read`, { method: "POST" })
        .then(() => { loadRooms(); loadMeta().catch(() => {}); })
        .catch((err) => toast(err.message, "error"));
    }
  } catch (err) { toast(err.message, "error"); }
}

function renderRoom(r) {
  $("#room-empty").classList.add("hidden");
  $("#room-body").classList.remove("hidden");
  $("#buyer").textContent = r.room.buyer_name || I18N.tr("Покупатель");
  $("#buyer-phone").textContent = r.room.buyer_phone || "";
  const order = $("#order-link");
  order.classList.toggle("hidden", !r.room.order_id);
  if (r.room.order_id) {
    order.textContent = `${I18N.tr("Заказ №")}${r.room.order_id}`;
    order.href = `/orders?q=${r.room.order_id}`;
  }
  $("#room-product").innerHTML = productCard(r.product);
  // готовые ответы из настоящих данных (наличие, ТТН, статус заказа) — по-украински, без ИИ
  $("#quick").innerHTML = (r.quick || []).map((q) =>
    `<button class="chip" data-quick="${esc(q.text)}" title="${esc(q.text)}">${esc(I18N.tr(q.label))}</button>`).join("");
  const box = $("#messages");
  // прокрутка вниз — только при открытии диалога или если и так были внизу: читая старое, не прыгаем в конец
  const sameRoom = box.dataset.room === r.room.room_ident;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.dataset.room = r.room.room_ident;
  if (!sameRoom) box.innerHTML = "";
  patchChildren(box, r.messages, (m) => m.id ?? `${m.created_at}|${m.type}`, (m) => {
    if (m.type === "context") {
      const what = m.context_type === "order" ? `${I18N.tr("Заказ №")}${m.context_id}` : "";
      return `<div class="chat-context small muted">${m.product ? productCard(m.product) : esc(what || I18N.tr("Покупатель смотрит товар"))}</div>`;
    }
    // вложения Prom: фото показываем миниатюрой, остальное — ссылкой
    const files = (m.attachments || []).map((a) => {
      const raw = a.attachment_url || a.url || "";
      const url = safeUrl(raw) ? raw : "";  // только http/https: ссылка пришла извне
      const name = a.attachment_name || a.name || I18N.tr("файл");
      return (a.attachment_type || "").toUpperCase() === "IMAGE" && url
        ? `<a href="${esc(url)}" target="_blank" rel="noopener"><img class="chat-photo" src="${esc(url)}" alt="${esc(name)}" loading="lazy"></a>`
        : `<a href="${esc(url || "#")}" target="_blank" rel="noopener">📎 ${esc(name)}</a>`;
    }).join(" ");
    const text = m.type === "attachment" && files ? "" : esc(m.body);
    return `<div class="chat-msg ${m.is_my ? "mine" : ""}">
      <div class="chat-bubble" translate="no">${text}${files ? `<div>${files}</div>` : ""}</div>
      <div class="small muted">${esc(time(m.created_at))}</div></div>`;
  });
  if (!sameRoom || atBottom) box.scrollTop = box.scrollHeight;
}

$("#rooms").addEventListener("click", (e) => {
  const b = e.target.closest("[data-room]");
  if (b) openRoom(b.dataset.room);
});
$("#back").onclick = () => openRoom("");
$$("[data-filter]").forEach((b) => (b.onclick = () => {
  view.unread = b.dataset.filter === "unread";
  $$("[data-filter]").forEach((x) => x.classList.toggle("active", x === b));
  loadRooms();
}));
let searchTimer;
$("#q").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { view.q = e.target.value.trim(); loadRooms(); }, 300);
});

// шаблон вставляется как есть — по-украински
$("#templates").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tpl]");
  if (!b) return;
  const t = $("#reply");
  t.value = (t.value.trim() ? t.value.trim() + " " : "") + b.textContent.trim();
  t.focus();
});

$("#quick").addEventListener("click", (e) => {
  const b = e.target.closest("[data-quick]");
  if (!b) return;
  const t = $("#reply");
  t.value = (t.value.trim() ? t.value.trim() + " " : "") + b.dataset.quick;
  t.focus();
});

$("#send").onclick = () => busy($("#send"), async () => {
  const text = $("#reply").value.trim();
  if (!text) { toast(I18N.tr("Напишите текст ответа"), "error"); return; }
  try {
    const r = await api(`/api/chat/rooms/${encodeURIComponent(view.room)}/send`, { method: "POST", json: { text } });
    $("#reply").value = "";
    $("#ai-usage").textContent = "";
    renderRoom(r);
    toast(I18N.tr("Отправлено покупателю"), "ok");
    loadRooms();
  } catch (err) { toast(err.message, "error"); }
});
$("#reply").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#send").click();
});

$("#ai-draft").onclick = () => busy($("#ai-draft"), async () => {
  try {
    const r = await api(`/api/chat/rooms/${encodeURIComponent(view.room)}/ai-draft`, { method: "POST" });
    $("#reply").value = r.reply;
    $("#ai-usage").textContent = r.usage ? aiUsageLine({ ...r.usage, free: r.usage.free }) : "";
    $("#reply").focus();
    toast(I18N.tr("Черновик готов — проверьте и отправьте"), "ok");
  } catch (err) { toast(err.message, "error"); }
});

$("#refresh").onclick = () => busy($("#refresh"), async () => {
  try {
    const r = await api("/api/chat/refresh", { method: "POST" });
    toast(r.new ? `${I18N.tr("Новых сообщений:")} ${r.new}` : I18N.tr("Новых сообщений нет"), "ok");
    await loadRooms();
    if (view.room) openRoom(view.room, { markRead: false });
    loadMeta().catch(() => {});
  } catch (err) { toast(err.message, "error"); }
});

loadMeta().catch(() => {}).finally(async () => {
  await loadRooms();
  if (view.room) openRoom(view.room);
});
// пока вкладка на экране, список и открытый диалог обновляются сами (сообщения программа забирает раз в минуту)
setInterval(() => {
  if (document.visibilityState !== "visible") return;
  loadRooms();
  if (view.room) openRoom(view.room, { markRead: false });
}, 30000);
