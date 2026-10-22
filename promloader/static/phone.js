// «Настройки» → «Доступ с телефона»: Wi‑Fi, адрес через Cloudflare, ссылки с QR-кодами.
// Адреса — данные (translate="no"); QR рисует программа (/api/phone/qr.svg), только на этом компьютере.

let phonePoll = null;

function phoneUrl(url) {
  return `<div class="phone-url">
    <img src="/api/phone/qr.svg?text=${encodeURIComponent(url)}" alt="" width="140" height="140" onerror="this.remove()">
    <div><a href="${esc(url)}" target="_blank" rel="noopener" translate="no">${esc(url)}</a><br>
      <button class="btn small" data-copy="${esc(url)}">Копировать</button></div></div>`;
}

// form = false — фоновое обновление: только адреса и состояние. Галочки не трогаем — иначе опрос каждые 3 секунды
// возвращал их к сохранённым, и переключить «Wi‑Fi / Cloudflare» до «Сохранить» было невозможно
function fillPhone(st, { form = true } = {}) {
  if (form) {
    $("#phone-lan").checked = st.lan;
    $("#phone-remote").checked = st.remote;
  }
  $("#phone-sign-out").classList.toggle("hidden", !st.password_set);

  $("#phone-lan-box").classList.toggle("hidden", !st.lan);
  let note = "";
  if (st.lan && !st.launcher) note = I18N.tr("Работает, когда программа запущена ярлыком «Prom Loader» (START.bat).");
  else if (st.lan && !st.lan_active) note = I18N.tr("Включится после перезапуска программы.");
  else if (st.lan_active && !st.lan_urls.length) note = I18N.tr("Компьютер не подключён к домашней сети — адрес не найден.");
  else if (st.lan_active) note = I18N.tr("Наведите камеру телефона на QR-код или наберите адрес в браузере телефона. Телефон должен быть подключён к тому же Wi‑Fi, что и компьютер.");
  $("#phone-lan-note").textContent = note;
  // У компьютера бывает несколько адресов (VPN, Hyper-V, WSL, VirtualBox), а с телефона открывается один — сеть
  // роутера. Его показываем, остальные — свёрнуто, на случай если главный не подошёл
  const [main, ...rest] = st.lan_active ? st.lan_urls : [];
  const seen = !!(main && st.lan_seen && main.includes(`//${st.lan_seen}:`));
  $("#phone-lan-urls").innerHTML = main ? `${phoneUrl(main)}
    <p class="small ${seen ? "ok-text" : "muted"}" id="phone-lan-main-note">${esc(I18N.tr(seen
      ? "✓ По этому адресу телефон уже открывал программу."
      : "Это адрес сети, через которую компьютер выходит в интернет, — обычно он и нужен телефону."))}</p>
    ${rest.length ? `<details class="phone-more"><summary>${esc(I18N.tr("Другие адреса компьютера"))} (${rest.length})</summary>
      <p class="small muted">${esc(I18N.tr("Это VPN и виртуальные сети (Hyper-V, WSL, VirtualBox) — с телефона они обычно не открываются. Пробуйте их, только если главный адрес не открылся."))}</p>
      ${rest.map(phoneUrl).join("")}</details>` : ""}` : "";

  $("#phone-remote-box").classList.toggle("hidden", !st.remote);
  let state = "";
  if (st.remote && !st.launcher) state = I18N.tr("Работает, когда программа запущена ярлыком «Prom Loader» (START.bat).");
  else if (st.remote_active) state = I18N.tr("Адрес работает, пока программа запущена. После перезапуска он будет другим.");
  else if (st.remote_error) state = st.remote_error;
  else if (st.remote) state = I18N.tr("Открываю адрес через Cloudflare… обычно это 10–30 секунд.");
  $("#phone-remote-state").textContent = state;
  $("#phone-remote-state").classList.toggle("err-text", !!st.remote_error && !st.remote_active);
  $("#phone-remote-url").innerHTML = st.remote_active ? phoneUrl(st.remote_url) : "";

  // адрес через Cloudflare появляется не сразу — пока ждём, спрашиваем каждые 3 секунды
  clearTimeout(phonePoll);
  if (st.remote && st.launcher && !st.remote_active) phonePoll = setTimeout(() => loadPhone({ form: false }), 3000);
}

async function loadPhone({ form = true } = {}) {
  try {
    fillPhone(await api("/api/phone"), { form });
  } catch (err) {
    if (err.status === 403) {  // открыто с телефона: менять доступ можно только на компьютере
      $("#phone-form").classList.add("hidden");
      $("#phone-remote-only").classList.remove("hidden");
      return;
    }
    if (!err.network) toast(err.message, "error");
  }
}

async function waitRestart() {
  // программа перезапускается: ждём, пока снова ответит, и показываем новые адреса
  await new Promise((r) => setTimeout(r, 4000));
  for (let i = 0; i < 40; i++) {
    try { fillPhone(await api("/api/phone")); return true; } catch { await new Promise((r) => setTimeout(r, 2000)); }
  }
  return false;
}

$("#phone-save").onclick = () => busy($("#phone-save"), async () => {
  const data = { lan: $("#phone-lan").checked, remote: $("#phone-remote").checked };
  try {
    const st = await api("/api/phone", { method: "POST", json: data });
    fillPhone(st);
    if (st.restarting) {
      $("#phone-result").textContent = I18N.tr("Программа перезапускается…");
      const ok = await waitRestart();
      $("#phone-result").textContent = ok ? "" : I18N.tr("Программа не ответила после перезапуска — запустите её ярлыком «Prom Loader»");
      if (ok) toast(I18N.tr("Сохранено"), "ok");
      return;
    }
    if (st.need_restart) toast(I18N.tr("Сохранено. Перезапустите программу, чтобы включить доступ по Wi‑Fi"), "ok");
    else toast(I18N.tr("Сохранено"), "ok");
  } catch (err) { toast(err.message, "error"); }
});

$("#phone-sign-out").onclick = () => busy($("#phone-sign-out"), async () => {
  if (!confirm("Выйти на всех устройствах? Все пользователи (кроме вас на этом компьютере) должны будут снова ввести логин и пароль.")) return;
  try {
    await api("/api/phone/sign-out-all", { method: "POST" });
    toast(I18N.tr("Все устройства вышли из программы"), "ok");
  } catch (err) { toast(err.message, "error"); }
});

$("#phone").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  try {
    await navigator.clipboard.writeText(b.dataset.copy);
    toast(I18N.tr("Адрес скопирован"), "ok");
  } catch { toast(I18N.tr("Не удалось скопировать — выделите адрес и скопируйте вручную"), "error"); }
});

loadPhone();
