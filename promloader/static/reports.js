// Страница «Отчёты»: период, четыре вкладки, таблицы с сортировкой, график на SVG без библиотек, «⬇ Excel».
// Названия товаров, групп, поставщиков и причины отмен от покупателей — данные, поэтому translate="no".

const START = new URLSearchParams(location.search);
const view = {
  tab: START.get("tab") || "sales",
  period: START.get("period") || "30d",
  date_from: START.get("date_from") || "",
  date_to: START.get("date_to") || "",
  dead_days: START.get("dead_days") || "30",
  seq: 0,
};
let report = null;
const sorts = {};  // таблица → {key, dir}

function query() {
  const p = new URLSearchParams({ period: view.period, dead_days: view.dead_days });
  if (view.period === "custom") { p.set("date_from", view.date_from); p.set("date_to", view.date_to); }
  return p;
}

function syncUrl() {
  const url = new URL(location.href);
  url.search = new URLSearchParams({ tab: view.tab, ...Object.fromEntries(query()) }).toString();
  history.replaceState(null, "", url);
  $("#xlsx").href = `/api/reports/${view.tab}.xlsx?${query()}`;
}

const money = (v) => (v === null || v === undefined ? "—" : formatPrice(v, "UAH"));
const num = (v) => Number(v || 0).toLocaleString(uiLocale(), { maximumFractionDigits: 2 });
const approxMark = (on) => on
  ? ` <span class="approx" title="${esc(I18N.tr("Закупка на дату заказа неизвестна — взята текущая"))}">≈</span>` : "";

function kpi(label, value, note = "") {
  return `<div class="kpi"><div class="kpi-label">${esc(label)}</div><div class="kpi-value">${value}</div>${note ? `<div class="kpi-note">${note}</div>` : ""}</div>`;
}

// Таблица с сортировкой по клику на заголовок. cols: [заголовок, ключ, формат(row) | null, {num}]
function table(id, rows, cols, empty = "Нет данных за этот период") {
  if (!rows.length) return `<p class="muted small">${esc(empty)}</p>`;
  const s = sorts[id];
  const sorted = s ? [...rows].sort((a, b) => {
    const x = a[s.key], y = b[s.key];
    const r = typeof x === "number" || typeof y === "number" ? (x ?? -Infinity) - (y ?? -Infinity) : String(x ?? "").localeCompare(String(y ?? ""));
    return s.dir * r;
  }) : rows;
  return `<div class="table-scroll"><table class="list report-table" data-table="${id}">
    <thead><tr>${cols.map(([title, key, , opt]) => `<th data-sort="${key}" class="${opt && opt.num ? "num" : ""}">${esc(title)}${s && s.key === key ? (s.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead>
    <tbody>${sorted.map((r) => `<tr>${cols.map(([, key, fmt, opt]) => `<td class="${opt && opt.num ? "num nowrap" : ""}">${fmt ? fmt(r) : esc(r[key] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody>
  </table></div>`;
}

const productCell = (r) => r.product_id
  ? `<a href="/product?id=${r.product_id}" translate="no">${esc(r.name)}</a>${r.code ? `<div class="small muted" translate="no">${esc(r.code)}</div>` : ""}`
  : `<span translate="no">${esc(r.name)}</span> <span class="small muted">${esc(I18N.tr("(нет в программе)"))}</span>`;
const dataCell = (key, fallback = "—") => (r) => r[key] ? `<span translate="no">${esc(r[key])}</span>` : `<span class="muted">${esc(I18N.tr(fallback))}</span>`;

// ---------- график ----------
function chart(days) {
  if (!days.length) return "";
  const W = 760, H = 220, pad = { l: 56, r: 10, t: 12, b: 26 };
  const max = Math.max(1, ...days.map((d) => Math.max(d.revenue, d.profit)));
  const min = Math.min(0, ...days.map((d) => d.profit));
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - (v - min) / (max - min));
  const bw = (W - pad.l - pad.r) / days.length;
  const bars = days.map((d, i) => `<rect x="${pad.l + i * bw + bw * 0.15}" y="${y(Math.max(0, d.revenue))}" width="${Math.max(1, bw * 0.7)}"
      height="${Math.max(0, y(0) - y(Math.max(0, d.revenue)))}" class="bar"><title>${esc(formatDate(d.date).split(",")[0])}: ${esc(money(d.revenue))} · ${d.orders}</title></rect>`).join("");
  const line = days.map((d, i) => `${pad.l + i * bw + bw / 2},${y(d.profit)}`).join(" ");
  const ticks = [max, max / 2, 0].map((v) => `<text x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end">${esc(num(Math.round(v)))}</text>
      <line x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}" class="grid"/>`).join("");
  const step = Math.ceil(days.length / 8);
  const labels = days.map((d, i) => i % step ? "" : `<text x="${pad.l + i * bw + bw / 2}" y="${H - 6}" text-anchor="middle">${esc(d.date.slice(8, 10) + "." + d.date.slice(5, 7))}</text>`).join("");
  return `<div class="chart-box"><svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${esc(I18N.tr("Выручка и прибыль по дням"))}">
      ${ticks}${bars}<polyline points="${line}" class="profit-line"/>${labels}</svg>
    <div class="small muted"><span class="legend bar-l"></span> ${esc(I18N.tr("выручка"))} &nbsp; <span class="legend line-l"></span> ${esc(I18N.tr("прибыль"))}</div></div>`;
}

// ---------- вкладки ----------
function renderSales(r) {
  const profitNote = r.profit === null ? I18N.tr("закупка товаров неизвестна")
    : `${I18N.tr("закупка и комиссия Prom вычтены")} · ${I18N.tr("известна для")} ${r.profit_coverage}% ${I18N.tr("выручки")}`;
  return `<div class="kpis">
      ${kpi(I18N.tr("Заказы"), num(r.orders), `${num(r.units)} ${I18N.tr("шт.")}`)}
      ${kpi(I18N.tr("Выручка"), esc(money(r.revenue)))}
      ${kpi(I18N.tr("Средний чек"), esc(money(r.avg_check)))}
      ${kpi(I18N.tr("Прибыль"), esc(money(r.profit)) + approxMark(r.approx), esc(profitNote))}
      ${kpi(I18N.tr("Комиссия Prom"), esc(money(r.commission)))}
      ${kpi(I18N.tr("Отменено"), `${num(r.canceled)} <span class="small muted">(${r.cancel_rate}%)</span>`)}
    </div>
    ${r.orders || r.canceled ? chart(r.days) : `<p class="muted">${esc(I18N.tr("Заказов за этот период нет."))}</p>`}
    <h3>${esc(I18N.tr("Причины отмен"))}</h3>
    ${table("reasons", r.cancel_reasons, [["Причина", "reason", dataCell("reason")], ["Заказов", "count", null, { num: true }]], "Отмен не было")}`;
}

function renderProducts(r) {
  return `<div class="kpis">
      ${kpi("A", num(r.abc.A), esc(I18N.tr("80% выручки")))}${kpi("B", num(r.abc.B), esc(I18N.tr("следующие 15%")))}${kpi("C", num(r.abc.C), esc(I18N.tr("остальные 5%")))}
    </div>
    <h3>${esc(I18N.tr("Что продаётся"))}</h3>
    ${table("top", r.top, [["Товар", "name", productCell], ["Группа", "group", dataCell("group")],
      ["Штук", "units", (x) => num(x.units), { num: true }], ["Выручка", "revenue", (x) => esc(money(x.revenue)), { num: true }],
      ["Прибыль", "profit", (x) => esc(money(x.profit)) + approxMark(x.approx), { num: true }], ["ABC", "abc"]])}
    <h3>${esc(I18N.tr("Лежат без продаж"))}
      <label class="small muted" style="font-weight:normal">— <input type="number" id="dead-days" min="1" max="3650" value="${esc(view.dead_days)}" style="width:70px"> ${esc(I18N.tr("дней"))}</label></h3>
    <p class="small muted" style="margin-top:-6px">${esc(I18N.tr("В наличии и на Prom, но ни одной продажи за это время (по всем заказам, а не только за период)."))}</p>
    ${table("dead", r.dead, [["Товар", "name", productCell], ["Группа", "group", dataCell("group")], ["Поставщик", "supplier", dataCell("supplier")],
      ["Цена", "price", (x) => esc(money(x.price)), { num: true }],
      ["Последняя продажа", "last_sale", (x) => x.last_sale ? esc(formatDate(x.last_sale).split(",")[0]) : `<span class="muted">${esc(I18N.tr("не было"))}</span>`]],
      "Таких товаров нет")}
    <h3>${esc(I18N.tr("Чаще отменяют"))}</h3>
    ${table("canceled", r.canceled, [["Товар", "name", productCell], ["Отмен", "canceled", null, { num: true }]], "Отмен не было")}`;
}

function renderSuppliers(r) {
  const rows = r.suppliers.map((s) => ({ ...s, name: s.name || "" }));
  return `<h3>${esc(I18N.tr("Поставщики"))}</h3>
    ${table("suppliers", rows, [
      ["Поставщик", "name", (x) => x.id ? `<a href="/supplier?id=${x.id}" translate="no">${esc(x.name)}</a>` : `<span class="muted">${esc(I18N.tr("без поставщика"))}</span>`],
      ["Товаров", "products", (x) => x.products ?? "—", { num: true }], ["На Prom", "on_prom", (x) => x.on_prom ?? "—", { num: true }],
      ["Пропали из прайса", "missing", (x) => x.missing ? `<a href="/?supplier=${x.id}&gone=1">${x.missing}</a>` : (x.missing ?? "—"), { num: true }],
      ["Штук продано", "units", (x) => num(x.units), { num: true }], ["Выручка", "revenue", (x) => esc(money(x.revenue)), { num: true }],
      ["Прибыль", "profit", (x) => esc(money(x.profit)) + approxMark(x.approx), { num: true }]], "Поставщиков пока нет")}
    <h3>${esc(I18N.tr("Группы"))}</h3>
    ${table("groups", r.groups, [["Группа", "name", (x) => x.name ? `<a href="/?group=${encodeURIComponent(x.name)}" translate="no">${esc(x.name)}</a>` : `<span class="muted">${esc(I18N.tr("без группы"))}</span>`],
      ["Штук", "units", (x) => num(x.units), { num: true }], ["Выручка", "revenue", (x) => esc(money(x.revenue)), { num: true }],
      ["Прибыль", "profit", (x) => esc(money(x.profit)) + approxMark(x.approx), { num: true }]])}`;
}

function renderStore(r) {
  const q = r.quality;
  const link = (label, n, params) => `<a class="kpi kpi-link" href="/?${new URLSearchParams(params)}"><div class="kpi-label">${esc(I18N.tr(label))}</div><div class="kpi-value">${num(n)}</div></a>`;
  return `<div class="kpis">
      ${kpi(I18N.tr("Товаров"), num(r.total))}
      ${Object.entries(r.statuses).map(([k, n]) => link(META.statuses[k] || k, n, { status: k })).join("")}
    </div>
    <h3>${esc(I18N.tr("Что улучшить в карточках"))}</h3>
    <div class="kpis">
      ${link("Без фото", q.no_photo, { no_photo: 1 })}
      ${link("С ошибками заполнения", q.errors, { errors: 1 })}
      ${kpi(I18N.tr("Без описания"), num(q.no_description))}
      ${kpi(I18N.tr("Без украинского названия"), num(q.no_ua))}
    </div>
    <h3>${esc(I18N.tr("Цены за период"))}</h3>
    <div class="kpis">
      ${kpi(I18N.tr("Изменений цен"), num(r.prices.changed))}
      ${kpi(I18N.tr("Подорожало"), num(r.prices.up))}
      ${kpi(I18N.tr("Подешевело"), num(r.prices.down))}
      ${kpi(I18N.tr("В среднем"), `${r.prices.avg_pct > 0 ? "+" : ""}${r.prices.avg_pct}%`)}
    </div>
    <p class="small"><a href="/changes">${esc(I18N.tr("Подробно — в журнале изменений"))}</a></p>
    <h3>${esc(I18N.tr("Расходы ИИ"))} — ${esc(r.ai_uah != null ? money(r.ai_uah) : "$" + r.ai_usd)}</h3>
    ${table("ai", r.ai, [["Модель", "model", dataCell("model")], ["Действие", "action"], ["Запросов", "requests", null, { num: true }],
      ["Токенов", "tokens", (x) => num(x.tokens), { num: true }], ["$", "usd", (x) => x.usd.toFixed(4), { num: true }],
      ["грн", "uah", (x) => x.uah != null ? num(x.uah) : "—", { num: true }]], "Запросов к ИИ не было")}`;
}

const RENDER = { sales: renderSales, products: renderProducts, suppliers: renderSuppliers, store: renderStore };

function render() {
  $$("#tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === view.tab));
  $$("#periods .chip").forEach((b) => b.classList.toggle("active", b.dataset.period === view.period));
  $("#custom-range").classList.toggle("hidden", view.period !== "custom");
  if (!report) return;
  $("#report").innerHTML = `<p class="small muted">${esc(formatDate(report.from).split(",")[0])} — ${esc(formatDate(report.to).split(",")[0])}</p>` +
    RENDER[report.tab](report);
  const dd = $("#dead-days");
  if (dd) dd.addEventListener("change", () => { view.dead_days = String(Math.max(1, Number(dd.value) || 30)); load(); });
}

async function load() {
  syncUrl();
  render();
  const seq = ++view.seq;
  // считается — заготовка карточек итогов на месте отчёта (и при первом открытии, и при смене вкладки/периода)
  if (!report || report.tab !== view.tab) $("#report").innerHTML = skeletonCards(3);
  else $("#report").classList.add("is-stale");
  try {
    const r = await api(`/api/reports/${view.tab}?${query()}`);
    if (seq !== view.seq) return;  // пока считали, поменяли вкладку или период
    report = r;
    $("#error").textContent = "";
    render();
  } catch (err) {
    if (seq === view.seq) {
      $("#error").textContent = err.message;
      if (!report) $("#report").innerHTML = "";
    }
  } finally {
    if (seq === view.seq) $("#report").classList.remove("is-stale");
  }
}

$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (!b) return;
  view.tab = b.dataset.tab;
  report = null;
  load();
});
$("#periods").addEventListener("click", (e) => {
  const b = e.target.closest("[data-period]");
  if (!b) return;
  view.period = b.dataset.period;
  if (view.period === "custom" && !view.date_from) {
    const d = new Date();
    view.date_to = d.toISOString().slice(0, 10);
    d.setDate(d.getDate() - 29);
    view.date_from = d.toISOString().slice(0, 10);
    $("#date-from").value = view.date_from;
    $("#date-to").value = view.date_to;
  }
  load();
});
$("#date-from").addEventListener("change", (e) => { view.date_from = e.target.value; load(); });
$("#date-to").addEventListener("change", (e) => { view.date_to = e.target.value; load(); });
$("#report").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const id = th.closest("table").dataset.table;
  const key = th.dataset.sort;
  const cur = sorts[id];
  // числа сначала по убыванию — так обычно и нужно (самые продаваемые)
  sorts[id] = { key, dir: cur && cur.key === key ? -cur.dir : (th.classList.contains("num") ? -1 : 1) };
  render();
});

$("#date-from").value = view.date_from;
$("#date-to").value = view.date_to;
loadMeta().catch(() => {}).finally(load);
