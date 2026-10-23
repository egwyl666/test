// «Отзывы и вопросы»: отзывы о магазине (с публичной страницы Prom) и сообщения с сайта («Написать продавцу»).
// Тексты покупателей, имена и товары — данные (translate="no"); ответы покупателям — по-украински.

const params = new URLSearchParams(location.search);
const view = { tab: params.get("tab") === "messages" ? "messages" : "reviews", f: params.get("f") || "" };
const draftTimers = {};

function stars(rating) {
  if (!rating) return `<span class="small muted">${esc(I18N.tr("без оценки"))}</span>`;
  return `<span class="stars r${rating}" title="${rating} / 5">${"★".repeat(rating)}<span class="stars-off">${"★".repeat(5 - rating)}</span></span>`;
}

function setTab(tab) {
  view.tab = tab;
  $$("#tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#tab-reviews").classList.toggle("hidden", tab !== "reviews");
  $("#tab-messages").classList.toggle("hidden", tab !== "messages");
  const u = new URL(location.href);
  u.searchParams.set("tab", tab);
  history.replaceState(null, "", u);
  tab === "reviews" ? loadReviews() : loadMessages();
}

function kpis(st) {
  const k = (label, value, cls = "") => `<div class="kpi ${cls}"><div class="kpi-label">${esc(I18N.tr(label))}</div><div class="kpi-value">${value}</div></div>`;
  return k("Средняя оценка", st.avg !== null ? `${st.avg.toFixed(1)} ${stars(Math.round(st.avg))}` : "—")
    + k("Всего отзывов", st.total) + k("Без ответа", st.no_reply, st.no_reply ? "warn" : "")
    + k("Негативных", st.negative, st.negative ? "bad" : "");
}

function reviewCard(r) {
  const products = r.products.map((p) => p.product_id
    ? `<a href="/product?id=${p.product_id}" translate="no">${esc(p.name)}</a>`
    : `<span translate="no">${esc(p.name)}</span>`).join(", ");
  const order = r.order_id ? (r.order_known ? `<a class="badge ready" href="/orders?q=${r.order_id}">${esc(I18N.tr("Заказ"))} №${r.order_id}</a>`
    : `<span class="small muted">${esc(I18N.tr("Заказ"))} №${r.order_id}</span>`) : "";
  const bad = r.rating !== null && r.rating <= 3;
  const reply = r.reply
    ? `<div class="review-reply"><span class="small ok-text">✓ ${esc(I18N.tr("Ответ магазина на Prom"))}</span>
        <div translate="no">${esc(r.reply).replace(/\n/g, "<br>")}</div></div>`
    : `<div class="review-answer">
        <textarea rows="3" data-draft="${r.id}" data-perm="chat" placeholder="${esc(I18N.tr("Ответ покупателю (будет виден всем на Prom)"))}" translate="no">${esc(r.draft || "")}</textarea>
        <div class="toolbar" style="margin:6px 0 0">
          <button class="btn small" data-ai="${r.id}" data-perm="ai">✨ ${esc(I18N.tr("Предложить ответ"))}</button>
          <button class="btn small primary" data-copy="${r.id}">${esc(I18N.tr("Скопировать"))}</button>
          ${$("#public-link").href ? `<a class="btn small" href="${esc($("#public-link").href)}" target="_blank" rel="noopener">${esc(I18N.tr("Ответить на Prom ↗"))}</a>` : ""}
        </div></div>`;
  return `<div class="panel review${bad ? " review-bad" : ""}${r.seen ? "" : " review-new"}" data-id="${r.id}">
    <div class="toolbar" style="margin:0 0 6px">
      ${stars(r.rating)} <b translate="no">${esc(r.author || I18N.tr("Покупатель"))}</b>
      <span class="small muted">${esc(formatDate(r.created_at))}</span>
      ${r.seen ? "" : `<span class="badge ready">${esc(I18N.tr("новый"))}</span>`}
      <span class="spacer"></span>${order}
    </div>
    ${r.tags.length ? `<div class="chips review-tags">${r.tags.map((t) => `<span class="chip" translate="no">${esc(t)}</span>`).join("")}</div>` : ""}
    ${r.text ? `<p class="review-text" translate="no">${esc(r.text).replace(/\n/g, "<br>")}</p>` : `<p class="small muted">${esc(I18N.tr("Только оценка, без текста."))}</p>`}
    ${products ? `<div class="small">${esc(I18N.tr("Товар:"))} ${products}</div>` : ""}
    ${reply}
  </div>`;
}

async function loadReviews() {
  if (!$("#reviews").children.length) $("#reviews").innerHTML = skeletonCards(3);
  let data;
  try { data = await api(`/api/reviews?filter=${encodeURIComponent(view.f)}`); } catch (err) { toast(err.message, "error"); return; }
  $("#off").classList.toggle("hidden", data.enabled);
  const st = data.stats;
  $("#error").textContent = st.error || "";
  $("#poll-info").textContent = st.last_poll ? `${I18N.tr("проверено")} ${formatDate(st.last_poll)}` : "";
  $("#public-link").classList.toggle("hidden", !st.public_url);
  if (st.public_url) $("#public-link").href = st.public_url;
  $("#review-kpis").innerHTML = kpis(st);
  $("#reviews-badge").textContent = st.no_reply;
  $("#reviews-badge").classList.toggle("hidden", !st.no_reply);
  $$("#review-filters .chip").forEach((c) => c.classList.toggle("active", c.dataset.f === view.f));
  $("#reviews").innerHTML = data.items.length ? data.items.map(reviewCard).join("")
    : `<div class="panel muted">${esc(I18N.tr(st.total ? "Таких отзывов нет." : "Отзывов пока нет — они появятся после проверки (раз в 30 минут или «↻ Проверить»)."))}</div>`;
  if (st.new) api("/api/reviews/seen", { method: "POST" }).catch(() => {});  // увидели — на «Сегодня» больше не «новые»
}

function messageCard(m) {
  const product = m.product_id ? `<a href="/product?id=${m.product_id}" translate="no">${esc(m.product_name || "")}</a>` : "";
  const answer = m.reply
    ? `<div class="review-reply"><span class="small ok-text">✓ ${esc(I18N.tr("Ответили"))} ${esc(formatDate(m.replied_at))}${m.replied_by ? ` · <span translate="no">${esc(m.replied_by)}</span>` : ""}</span>
        <div translate="no">${esc(m.reply).replace(/\n/g, "<br>")}</div></div>`
    : `<div class="review-answer">
        <textarea rows="3" maxlength="1000" data-msg="${m.id}" data-perm="chat" placeholder="${esc(I18N.tr("Ответ покупателю (до 1000 символов)"))}" translate="no"></textarea>
        <div class="toolbar" style="margin:6px 0 0">
          <button class="btn small" data-msg-ai="${m.id}" data-perm="ai">✨ ${esc(I18N.tr("Предложить ответ"))}</button>
          <button class="btn small primary" data-msg-send="${m.id}" data-perm="chat">${esc(I18N.tr("Отправить"))}</button>
          ${m.status === "unread" ? `<button class="btn small" data-msg-read="${m.id}" data-perm="chat">${esc(I18N.tr("Прочитано"))}</button>` : ""}
        </div></div>`;
  return `<div class="panel review${m.status === "unread" ? " review-new" : ""}">
    <div class="toolbar" style="margin:0 0 6px">
      <b translate="no">${esc(m.client || I18N.tr("Покупатель"))}</b>
      ${m.phone ? `<a class="small" href="tel:${esc(m.phone)}" translate="no">${esc(m.phone)}</a>` : ""}
      <span class="small muted">${esc(formatDate(m.created_at))}</span>
      ${m.status === "unread" ? `<span class="badge ready">${esc(I18N.tr("новое"))}</span>` : ""}
    </div>
    ${m.subject ? `<div class="small" translate="no"><b>${esc(m.subject)}</b></div>` : ""}
    <p class="review-text" translate="no">${esc(m.text).replace(/\n/g, "<br>")}</p>
    ${product ? `<div class="small">${esc(I18N.tr("Товар:"))} ${product}</div>` : ""}
    ${answer}
  </div>`;
}

async function loadMessages() {
  let data;
  try { data = await api("/api/reviews/messages"); } catch (err) { toast(err.message, "error"); return; }
  $("#error").textContent = data.error || "";
  $("#messages-badge").textContent = data.unread;
  $("#messages-badge").classList.toggle("hidden", !data.unread);
  $("#messages").innerHTML = data.items.length ? data.items.map(messageCard).join("")
    : `<div class="panel muted">${esc(I18N.tr("Сообщений с сайта пока нет."))}</div>`;
}

async function copyText(text, area) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {  // по адресу http (Wi‑Fi дома) браузер не даёт буфер обмена — копируем через выделение
    area.select();
    document.execCommand("copy");
  }
}

$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (b) setTab(b.dataset.tab);
});
$("#review-filters").addEventListener("click", (e) => {
  const c = e.target.closest("[data-f]");
  if (!c) return;
  view.f = c.dataset.f;
  const u = new URL(location.href);
  if (view.f) u.searchParams.set("f", view.f); else u.searchParams.delete("f");
  history.replaceState(null, "", u);
  loadReviews();
});
$("#refresh").addEventListener("click", () => busy($("#refresh"), async () => {
  try {
    const r = await api("/api/reviews/refresh", { method: "POST" });
    toast(I18N.tr(`Новых отзывов: ${r.new}, сообщений: ${r.new_messages}`), "ok");
  } catch (err) { toast(err.message, "error"); }
  view.tab === "reviews" ? loadReviews() : loadMessages();
}));

document.addEventListener("input", (e) => {
  const area = e.target.closest("[data-draft]");
  if (!area) return;
  clearTimeout(draftTimers[area.dataset.draft]);  // черновик сохраняется сам — можно вернуться позже
  draftTimers[area.dataset.draft] = setTimeout(() => api(`/api/reviews/${area.dataset.draft}/draft`,
    { method: "POST", json: { text: area.value } }).catch(() => {}), 800);
});

document.addEventListener("click", async (e) => {
  const ai = e.target.closest("[data-ai]");
  const copy = e.target.closest("[data-copy]");
  const mAi = e.target.closest("[data-msg-ai]");
  const send = e.target.closest("[data-msg-send]");
  const read = e.target.closest("[data-msg-read]");
  if (ai) {
    busy(ai, async () => {
      try {
        const r = await api(`/api/reviews/${ai.dataset.ai}/ai-draft`, { method: "POST" });
        $(`[data-draft="${ai.dataset.ai}"]`).value = r.reply;
      } catch (err) { toast(err.message, "error"); }
    });
  } else if (copy) {
    const area = $(`[data-draft="${copy.dataset.copy}"]`);
    if (!area.value.trim()) { toast(I18N.tr("Сначала напишите ответ или нажмите «✨ Предложить ответ»"), "error"); return; }
    await copyText(area.value, area);
    toast(I18N.tr("Ответ скопирован — вставьте его под этим отзывом в кабинете Prom"), "ok");
  } else if (mAi) {
    busy(mAi, async () => {
      try {
        const r = await api(`/api/reviews/messages/${mAi.dataset.msgAi}/ai-draft`, { method: "POST" });
        $(`[data-msg="${mAi.dataset.msgAi}"]`).value = r.reply;
      } catch (err) { toast(err.message, "error"); }
    });
  } else if (send) {
    const text = $(`[data-msg="${send.dataset.msgSend}"]`).value;
    busy(send, async () => {
      try {
        await api(`/api/reviews/messages/${send.dataset.msgSend}/reply`, { method: "POST", json: { text } });
        toast(I18N.tr("Ответ отправлен покупателю"), "ok");
        loadMessages();
      } catch (err) { toast(err.message, "error"); }
    });
  } else if (read) {
    busy(read, async () => {
      try { await api(`/api/reviews/messages/${read.dataset.msgRead}/read`, { method: "POST" }); loadMessages(); }
      catch (err) { toast(err.message, "error"); }
    });
  }
});

loadMeta().catch(() => {}).finally(() => {
  setTab(view.tab);
  if (view.tab === "reviews") api("/api/reviews/messages").then((d) => {
    $("#messages-badge").textContent = d.unread;
    $("#messages-badge").classList.toggle("hidden", !d.unread);
  }).catch(() => {});
});
