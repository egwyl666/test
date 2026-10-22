// Главная «Сегодня»: итоги заказов за сегодня, вчера и месяц и список «Требует внимания» — каждый пункт ведёт прямо
// к нужному отфильтрованному списку. Обновляется сама раз в минуту, пока вкладка на экране.

function money(v) { return formatPrice(v || 0, "UAH"); }

function kpi(label, t, extra = "") {
  return `<div class="kpi"><span class="small muted">${esc(I18N.tr(label))}</span><b>${esc(money(t.revenue))}</b>
    <span class="small muted">${esc(I18N.tr(`заказов: ${t.orders}`))}${extra}</span></div>`;
}

async function loadToday() {
  const r = await api("/api/today");
  const s = r.sales;
  const profit = (t) => (t.profit === null || t.profit === undefined ? "" : ` · ${esc(I18N.tr("прибыль"))} ${esc(money(t.profit))}${t.approx ? " ≈" : ""}`);
  $("#today-kpis").innerHTML = kpi("Сегодня", s.today, profit(s.today)) + kpi("Вчера", s.yesterday)
    + kpi("7 дней", s["7d"]) + kpi("Этот месяц", s.month, profit(s.month));
  patchChildren($("#attention"), r.attention, (a) => a.key, (a) => `<a class="attention-item tone-${a.tone}" href="${esc(a.href)}">
      <span class="attention-icon">${esc(a.icon)}</span><span class="attention-text">${esc(I18N.tr(a.text))}</span>
      <b class="attention-count">${a.count}</b><span class="attention-go">→</span></a>`);
  $("#all-good").classList.toggle("hidden", r.attention.length > 0);
  $("#today-time").textContent = `${I18N.tr("обновлено")} ${new Date().toLocaleTimeString(uiLocale(), { hour: "2-digit", minute: "2-digit" })}`;
}

(async () => {
  const hour = new Date().getHours();
  $("#greet").textContent = I18N.tr(hour < 12 ? "Доброе утро" : hour < 18 ? "Добрый день" : "Добрый вечер");
  $("#today-kpis").innerHTML = skeletonCards(4);
  await loadMeta().catch(() => {});
  if (META.me && META.me.name) $("#greet").textContent += `, ${META.me.name}`;
  await loadToday().catch((err) => toast(err.message, "error"));
  setInterval(() => { if (document.visibilityState === "visible") loadToday().catch(() => {}); }, 60000);
})();
