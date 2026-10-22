// «Настройки» → «Приложение на телефоне»: установка значка и push-уведомления этого телефона.

const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

// Окно установки само (beforeinstallprompt) есть только у Chrome/Edge и только на https. Safari на iPhone его сайтам
// не даёт вовсе, Chrome по Wi‑Fi (http://192.168…) — тоже. Поэтому кнопка видна всегда: где окна нет — показывает
// пошаговую подсказку для этого телефона.
const IOS = /iPhone|iPad|iPod/.test(navigator.userAgent)
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);  // iPad с «настольным» Safari
const ANDROID = /Android/.test(navigator.userAgent);
// значок «Поделиться» Safari: квадрат со стрелкой вверх
const SHARE_ICON = `<svg class="i" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="m8 7 4-4 4 4"/>
  <path d="M8 11H6a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-6a2 2 0 0 0-2-2h-2"/></svg>`;

// шаг — текст; «{share}» в нём заменяется значком «Поделиться» (переводится текст, значок вставляется после)
function installSteps() {
  if (IOS) return [
    "Откройте эту страницу в Safari.",
    "Нажмите «Поделиться» {share} внизу экрана. В новых iOS она в меню «…» справа от адреса.",
    "Прокрутите список вниз → «На экран „Домой“» → «Добавить».",
    "Открывайте Prom Loader значком с экрана «Домой».",
  ];
  if (ANDROID) return [
    "Откройте эту страницу в Chrome.",
    "Меню ⋮ справа вверху → «Добавить на главный экран» (или «Установить приложение»).",
    "Подтвердите — значок появится на главном экране.",
  ];
  return ["В Chrome или Edge: значок установки в конце адресной строки или меню ⋮ → «Установить Prom Loader»."];
}

function showInstall() {
  $("#app-install").classList.toggle("hidden", standalone);
  $("#app-installed").textContent = standalone ? I18N.tr("✓ Открыто как приложение") : "";
}
document.addEventListener("pl-install-ready", showInstall);
showInstall();

function showInstallHelp() {
  let box = $("#install-help");
  if (!box) {
    box = document.createElement("div");
    box.id = "install-help";
    box.className = "modal hidden";
    document.body.appendChild(box);
  }
  const http = !window.isSecureContext;
  box.innerHTML = `<div class="modal-box" style="max-width:440px"><h2>${esc(I18N.tr("Установить на телефон"))}</h2>
    <ol class="install-steps">${installSteps().map((s) => `<li>${esc(I18N.tr(s)).replace("{share}", SHARE_ICON)}</li>`).join("")}</ol>
    ${http ? `<p class="small muted">${esc(I18N.tr("По этому адресу (Wi‑Fi дома) значок добавится, но уведомлений не будет — их браузеры разрешают только на https. Для уведомлений откройте адрес Cloudflare («Доступ с телефона») и установите значок оттуда."))}</p>` : ""}
    <div class="toolbar" style="margin:12px 0 0"><span class="spacer"></span><button class="btn" type="button" data-close>${esc(I18N.tr("Понятно"))}</button></div></div>`;
  box.querySelector("[data-close]").onclick = () => box.classList.add("hidden");
  box.classList.remove("hidden");
}

$("#app-install").onclick = async () => {
  if (!installPrompt) { showInstallHelp(); return; }  // iPhone, http или браузер без окна установки
  installPrompt.prompt();
  await installPrompt.userChoice.catch(() => {});
  installPrompt = null;
  showInstall();
};

// ---------- push ----------

function keyBytes(b64) {
  const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((b64.length + 3) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

async function currentSubscription() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !window.isSecureContext) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

async function loadPush() {
  const supported = "serviceWorker" in navigator && "PushManager" in window && window.isSecureContext;
  let note = "";
  if (!window.isSecureContext) note = I18N.tr("Уведомления работают только по защищённому адресу https — откройте программу по адресу Cloudflare («Доступ с телефона»).");
  else if (!supported) note = I18N.tr("Этот браузер не умеет push-уведомления. На iPhone сначала установите значок на экран «Домой» и откройте программу с него.");
  let state = { available: false, events: {} };
  try {
    const sub = supported ? await currentSubscription() : null;
    state = await api(`/api/push${sub ? `?endpoint=${encodeURIComponent(sub.endpoint)}` : ""}`);
    if (!state.available) note = note || I18N.tr("Push-уведомления недоступны: не установлен компонент cryptography (перезапустите программу).");
    const on = !!(sub && state.subscription);
    const chosen = on ? state.subscription.events : state.default_events || [];
    $("#push-events").innerHTML = Object.entries(state.events).map(([k, v]) =>
      `<label style="display:block"><input type="checkbox" value="${esc(k)}" ${chosen.includes(k) ? "checked" : ""}> ${esc(v)}</label>`).join("");
    $("#push-on").textContent = I18N.tr(on ? "Сохранить выбор" : "🔔 Включить уведомления");
    $("#push-test").classList.toggle("hidden", !on);
    $("#push-off").classList.toggle("hidden", !on);
    if (on && !note) note = I18N.tr("Уведомления на этот телефон включены.");
  } catch (err) { if (!err.network) toast(err.message, "error"); }
  $("#push-note").textContent = note;
  $("#push-on").disabled = !supported || !state.available;
}

$("#push-on").onclick = () => busy($("#push-on"), async () => {
  try {
    if (Notification.permission !== "granted" && await Notification.requestPermission() !== "granted") {
      toast(I18N.tr("Телефон не разрешил уведомления — разрешите их в настройках браузера для этого сайта"), "error");
      return;
    }
    const reg = await navigator.serviceWorker.ready;
    const { public_key } = await api("/api/push");
    const sub = await reg.pushManager.getSubscription()
      || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(public_key) });
    const events = $$("#push-events input:checked").map((i) => i.value);
    await api("/api/push/subscribe", { method: "POST", json: { subscription: sub.toJSON(), events, device: navigator.userAgent } });
    toast(I18N.tr("Уведомления включены"), "ok");
    loadPush();
  } catch (err) { toast(err.message, "error"); }
});

$("#push-test").onclick = () => busy($("#push-test"), async () => {
  try {
    const sub = await currentSubscription();
    await api("/api/push/test", { method: "POST", json: { endpoint: sub.endpoint } });
    toast(I18N.tr("Отправлено — уведомление придёт через несколько секунд"), "ok");
  } catch (err) { toast(err.message, "error"); }
});

$("#push-off").onclick = () => busy($("#push-off"), async () => {
  try {
    const sub = await currentSubscription();
    if (sub) {
      await api("/api/push/unsubscribe", { method: "POST", json: { endpoint: sub.endpoint } });
      await sub.unsubscribe();
    }
    toast(I18N.tr("Уведомления выключены"), "ok");
    loadPush();
  } catch (err) { toast(err.message, "error"); }
});

loadPush();
