// «Настройки» → «Приложение на телефоне»: установка значка и push-уведомления этого телефона.

const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

function showInstall() {
  $("#app-install").classList.toggle("hidden", !installPrompt || standalone);
  $("#app-installed").textContent = standalone ? I18N.tr("✓ Открыто как приложение") : "";
}
document.addEventListener("pl-install-ready", showInstall);
showInstall();

$("#app-install").onclick = async () => {
  if (!installPrompt) return;
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
