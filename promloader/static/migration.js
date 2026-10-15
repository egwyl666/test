// «Настройки» → «Переезд на другой компьютер или сервер»: пакет переезда, код подключения к облаку, восстановление.
// Пароль копии на сервер уходит только для шифрования/проверки; адреса и имена копий — данные (translate="no").

let migState = {};

async function loadMigration() {
  try { migState = await api("/api/migration"); } catch (err) { if (!err.network) toast(err.message, "error"); return; }
  $("#mig-cloud-wrap").classList.toggle("hidden", !migState.r2);
  $("#mig-code-btn").classList.toggle("hidden", !migState.r2);
  $("#mig-save-pass").classList.toggle("hidden", !migState.r2);
  $("#mig-pass-state").textContent = migState.password_saved
    ? I18N.tr("Пароль копии запомнен: ежедневные копии в облаке везут ключи и токены зашифрованными.") : "";
}

function migPassword() {
  const p = $("#mig-pass").value;
  if (p.length < 8) { toast(I18N.tr("Придумайте пароль копии — не меньше 8 символов"), "error"); $("#mig-pass").focus(); return null; }
  return p;
}

function migSummary(m) {
  const parts = [`товаров: ${m.products ?? "?"}`, `поставщиков: ${m.suppliers ?? "?"}`, `заказов: ${m.orders ?? "?"}`,
                 `чатов: ${m.chats ?? "?"}`, `фото в файле: ${m.photos_in_file ?? 0}`];
  return parts.map((x) => I18N.tr(x)).join(", ");
}

$("#mig-create").onclick = () => busy($("#mig-create"), async () => {
  const password = migPassword();
  if (!password) return;
  const pause = $("#mig-pause").checked;
  if (pause && !confirm("После создания пакета этот компьютер перестанет забирать заказы и чат и отправлять товары. Продолжить?")) return;
  $("#mig-result").textContent = I18N.tr("Собираю пакет… с фото это может занять несколько минут.");
  try {
    const r = await api("/api/migration/bundle", { method: "POST",
      json: { password, photos: $("#mig-photos").checked, cloud: $("#mig-cloud").checked, pause } });
    $("#mig-result").innerHTML = `✓ ${esc(I18N.tr("Пакет готов"))} (${esc(fmtSize(r.size))}): ${esc(migSummary(r.manifest))}.
      <a class="btn small" href="/api/backups/${encodeURIComponent(r.name)}">${esc(I18N.tr("Скачать файл"))}</a>
      ${r.cloud ? esc(I18N.tr("Отправлен в облако R2.")) : ""}`;
    if (typeof loadBackups === "function") loadBackups().catch(() => {});
    if (r.paused) loadMeta().catch(() => {});
  } catch (err) { $("#mig-result").textContent = ""; toast(err.message, "error"); }
});

$("#mig-code-btn").onclick = () => busy($("#mig-code-btn"), async () => {
  const password = migPassword();
  if (!password) return;
  try {
    const r = await api("/api/migration/code", { method: "POST", json: { password } });
    $("#mig-code").value = r.code;
    $("#mig-code-box").classList.remove("hidden");
  } catch (err) { toast(err.message, "error"); }
});
$("#mig-code-copy").onclick = async () => {
  try { await navigator.clipboard.writeText($("#mig-code").value); toast(I18N.tr("Код скопирован"), "ok"); }
  catch { $("#mig-code").select(); }
};

$("#mig-save-pass").onclick = () => busy($("#mig-save-pass"), async () => {
  const password = migPassword();
  if (!password) return;
  try {
    await api("/api/migration/password", { method: "POST", json: { password } });
    toast(I18N.tr("Пароль запомнен"), "ok");
    loadMigration();
  } catch (err) { toast(err.message, "error"); }
});

// ---------- новый компьютер ----------

function migPreview(m) {
  $("#mig-preview").classList.remove("hidden");
  const keys = m.secrets_encrypted ? I18N.tr("ключи и токены — в копии, зашифрованы паролем")
                                   : I18N.tr("ключей и токенов в копии нет — их нужно будет ввести заново");
  const from = m.hostname ? ` (${I18N.tr("с компьютера")} ${m.hostname})` : "";
  $("#mig-preview-text").innerHTML = `${esc(I18N.tr("В копии"))}${esc(from)}: ${esc(migSummary(m))}; ${esc(keys)}.`;
  $("#mig-apply-pass-wrap").classList.toggle("hidden", !m.secrets_encrypted);
  $("#mig-preview").scrollIntoView({ block: "nearest" });
}

$("#mig-file").onclick = async () => {
  const [file] = await pickFiles({ accept: ".zip", multiple: false });
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try { migPreview(await api("/api/migration/inspect", { method: "POST", body })); }
  catch (err) { toast(err.message, "error"); }
};

$("#mig-connect").onclick = () => busy($("#mig-connect"), async () => {
  try {
    const r = await api("/api/migration/connect", { method: "POST",
      json: { code: $("#mig-connect-code").value, password: $("#mig-connect-pass").value } });
    $("#mig-apply-pass").value = $("#mig-connect-pass").value;
    $("#mig-cloud-list").innerHTML = r.items.map((b) => `
      <tr><td>☁ ${esc(formatDate(b.created))}</td><td class="muted">${esc(b.reason_label)}</td><td class="muted">${fmtSize(b.size)}</td>
        <td style="text-align:right"><button class="btn small" data-mig-cloud="${esc(b.name)}">Выбрать</button></td></tr>`).join("")
      || `<tr><td class="muted">${esc(I18N.tr("Облако подключено, но копий в нём пока нет"))}</td></tr>`;
    toast(I18N.tr("Облако подключено"), "ok");
  } catch (err) { toast(err.message, "error"); }
});

$("#mig-cloud-list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mig-cloud]");
  if (!b) return;
  busy(b, async () => {
    try { migPreview(await api("/api/migration/inspect-cloud", { method: "POST", json: { name: b.dataset.migCloud } })); }
    catch (err) { toast(err.message, "error"); }
  });
});

$("#mig-apply").onclick = () => busy($("#mig-apply"), async () => {
  if (!confirm("Заменить данные этого компьютера данными из копии? Текущие данные перед этим сохранятся в копию.")) return;
  try {
    const r = await api("/api/migration/apply", { method: "POST", json: { password: $("#mig-apply-pass").value } });
    waitForRestart("Восстанавливаю данные…", r.restarting);
  } catch (err) { toast(err.message, "error"); }
});

loadMigration();
if (location.hash === "#migration") $("#migration").scrollIntoView();
