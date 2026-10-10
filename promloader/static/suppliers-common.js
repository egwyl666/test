// Отображение запусков обновления поставщиков (список и страница поставщика).

const RUN_STATUS = {
  running: ["sending", "Обновляется"],
  ok: ["synced", "Обновлён"],
  failed: ["error", "Ошибка"],
};

function runSummary(stats) {
  if (!stats || stats.total === undefined) return "";
  const parts = [
    ["created", "новых"], ["updated", "изменено"], ["price_changed", "цен изменилось"], ["missing", "пропало"],
    ["returned", "вернулось"], ["joined", "объединено с другими поставщиками"], ["errors", "с ошибками"],
    ["ignored", "пропущено (удалены вами)"], ["queued", "отправлено на Prom"],
  ].filter(([k]) => stats[k]).map(([k, label]) => `${label}: ${stats[k]}`);
  return parts.join(" · ") || "без изменений";
}

function runLine(s) {
  const r = s.last_run;
  if (s.running) return `<div class="run-line">${statusPill("running")} идёт обновление…</div>`;
  if (!r) return `<div class="run-line muted">Ещё не обновлялся</div>`;
  return `<div class="run-line">${statusPill(r.status)} <span class="muted">${esc(formatDate(r.finished_at || r.started_at))}</span>
      <span>${esc(r.status === "ok" ? runSummary(r.stats) : "")}</span></div>
    ${r.status === "failed" ? `<div class="run-msg">${esc(r.message)}</div>` : ""}`;
}

function statusPill(status) {
  const [cls, label] = RUN_STATUS[status] || ["draft", status];
  return `<span class="badge ${cls}">${label}</span>`;
}

function scheduleText(s) {
  if (!s.interval_hours) return "Обновление: вручную";
  const every = s.interval_hours >= 24 && s.interval_hours % 24 === 0
    ? `раз в ${s.interval_hours / 24} сут.` : `каждые ${s.interval_hours} ч`;
  return `Обновление: ${every}${s.next_run_at ? ` · следующее ${formatDate(s.next_run_at)}` : ""}` +
    (s.auto_sync ? " · изменения сразу уходят на Prom" : "");
}
