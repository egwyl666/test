// «Заказы»: полоска выручки над списком и вкладка «Статистика товаров» — выручка, штуки и прибыль по каждому товару.
// Отменённые заказы в выручку не входят (как в «Отчётах»). Названия товаров — данные (translate="no").

const stView = { tab: "list", sort: "revenue", seq: 0 };
const stMoney = (v) => (v === null || v === undefined ? "—" : formatPrice(v, "UAH"));
const stNum = (v) => Number(v || 0).toLocaleString(uiLocale(), { maximumFractionDigits: 2 });
const stApprox = (on) => on
  ? ` <span class="approx" title="${esc(I18N.tr("Закупка на дату заказа неизвестна — взята текущая"))}">≈</span>` : "";

function stKpi(label, value, note = "") {
  return `<div class="kpi"><div class="kpi-label">${esc(I18N.tr(label))}</div><div class="kpi-value">${value}</div>${note ? `<div class="kpi-note">${note}</div>` : ""}</div>`;
}

function profitNote(t) {
  return t.profit === null ? "" : `${esc(I18N.tr("прибыль"))} ${stMoney(t.profit)}${stApprox(t.approx)}`;
}

async function loadRevenueStrip() {
  try {
    const s = await api("/api/orders/summary");
    const cell = (label, t) => stKpi(label, stMoney(t.revenue),
      `${esc(I18N.tr("заказов"))}: ${t.orders}${t.profit !== null ? ` · ${profitNote(t)}` : ""}`);
    $("#revenue-strip").innerHTML = cell("Выручка сегодня", s.today) + cell("Вчера", s.yesterday) +
      cell("7 дней", s["7d"]) + cell("Этот месяц", s.month);
  } catch (err) {
    if (!err.network) $("#revenue-strip").innerHTML = "";
  }
}

function stQuery() {
  const p = new URLSearchParams({ period: $("#st-period").value, q: $("#st-q").value.trim(), sort: stView.sort });
  if ($("#st-period").value === "custom") { p.set("date_from", $("#st-from").value); p.set("date_to", $("#st-to").value); }
  return p;
}

const ST_COLS = [
  ["Товар", "name"], ["Заказов", "orders", 1], ["Штук", "units", 1], ["Выручка", "revenue", 1],
  ["Доля", "share", 1], ["Прибыль", "profit", 1], ["Отмен", "canceled", 1],
];

function stRow(r) {
  const name = r.product_id
    ? `<a href="/product?id=${r.product_id}" translate="no">${esc(r.name)}</a>`
    : `<span translate="no">${esc(r.name)}</span> <span class="small muted">${esc(I18N.tr("(нет в программе)"))}</span>`;
  const sub = [r.code, r.group].filter(Boolean).map(esc).join(" · ");
  return `<tr><td>${name}${sub ? `<div class="small muted" translate="no">${sub}</div>` : ""}</td>
    <td class="num">${r.orders}</td><td class="num">${stNum(r.units)}</td>
    <td class="num nowrap"><b>${stMoney(r.revenue)}</b></td><td class="num">${stNum(r.share)}%</td>
    <td class="num nowrap">${stMoney(r.profit)}${stApprox(r.approx)}</td>
    <td class="num">${r.canceled || ""}</td></tr>`;
}

async function loadStats() {
  const seq = ++stView.seq;
  const q = stQuery();
  $("#st-xlsx").href = `/api/orders/stats.xlsx?${q}`;
  try {
    const r = await api(`/api/orders/stats?${q}`);
    if (seq !== stView.seq) return;
    $("#st-error").textContent = "";
    const t = r.totals;
    $("#st-totals").innerHTML = stKpi("Выручка", stMoney(t.revenue)) + stKpi("Заказов", t.orders,
      t.canceled ? `${esc(I18N.tr("отменено"))}: ${t.canceled}` : "") +
      stKpi("Средний чек", stMoney(t.avg_check)) + stKpi("Продано штук", stNum(t.units)) +
      stKpi("Прибыль", `${stMoney(t.profit)}${stApprox(t.approx)}`);
    $("#st-table").innerHTML = r.items.length ? `<div class="table-scroll"><table class="list report-table">
      <thead><tr>${ST_COLS.map(([title, key, n]) => `<th data-sort="${key}" class="${n ? "num" : ""}">${esc(I18N.tr(title))}${stView.sort === key ? " ▼" : ""}</th>`).join("")}</tr></thead>
      <tbody>${r.items.map(stRow).join("")}</tbody></table></div>`
      : `<p class="muted small">${esc(I18N.tr("За этот период товаров в заказах нет"))}</p>`;
    $("#st-note").textContent = r.items.length
      ? I18N.tr("Выручка и штуки — без отменённых заказов. Прибыль — минус закупка на дату заказа; «—» — закупка товара неизвестна.") : "";
  } catch (err) {
    if (seq === stView.seq) $("#st-error").textContent = err.message;
  }
}

function showTab(tab) {
  stView.tab = tab;
  document.querySelectorAll("#order-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#list-view").classList.toggle("hidden", tab !== "list");
  $("#revenue-strip").classList.toggle("hidden", tab !== "list");
  $("#stats-view").classList.toggle("hidden", tab !== "stats");
  history.replaceState(null, "", tab === "stats" ? "#stats" : location.pathname + location.search);
  if (tab === "stats") loadStats();
}

$("#order-tabs").addEventListener("click", (e) => {
  const b = e.target.closest(".tab");
  if (b) showTab(b.dataset.tab);
});
$("#st-table").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  stView.sort = th.dataset.sort === "share" ? "revenue" : th.dataset.sort;
  loadStats();
});
$("#st-period").addEventListener("change", () => {
  $("#st-custom").classList.toggle("hidden", $("#st-period").value !== "custom");
  loadStats();
});
$("#st-from").addEventListener("change", loadStats);
$("#st-to").addEventListener("change", loadStats);
let stTimer;
$("#st-q").addEventListener("input", () => { clearTimeout(stTimer); stTimer = setTimeout(loadStats, 300); });

loadRevenueStrip();
showTab(location.hash === "#stats" ? "stats" : "list");
setInterval(() => { if (document.visibilityState === "visible" && stView.tab === "list") loadRevenueStrip(); }, 60000);
