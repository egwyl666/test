// Service worker приложения на телефоне. Ничего из программы не кеширует: скрипты и стили всегда берутся с компьютера
// (иначе после обновления телефон показывал бы старую версию). Задачи только две:
// - если компьютер выключен — показать понятную страницу вместо ошибки браузера;
// - push-уведомления о заказах и сообщениях покупателей.

const OFFLINE = `<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Prom Loader</title>
<style>body{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1d2129;margin:0;padding:40px 20px;text-align:center}
.box{max-width:420px;margin:0 auto;background:#fff;border-radius:10px;padding:24px}button{margin-top:12px;padding:10px 18px;
border:0;border-radius:8px;background:#6d3fe0;color:#fff;font-size:16px}</style></head><body><div class="box">
<h2>Нет связи с программой</h2><p>Компьютер с Prom Loader выключен, программа не запущена или у телефона нет интернета.</p>
<p style="color:#6b7280;font-size:14px">Ні зв'язку з програмою: комп'ютер вимкнено, програму не запущено або на телефоні немає інтернету.</p>
<button onclick="location.reload()">Повторить</button></div></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;  // всё остальное — напрямую в сеть, как без service worker
  event.respondWith(fetch(event.request).catch(() =>
    new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(data.title || "Prom Loader", {
    body: data.body || "", icon: "/static/icon-192.png", badge: "/static/icon-192.png",
    data: { url: data.url || "/" }, tag: data.tag || undefined,
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "/";
  event.waitUntil(self.clients.matchAll({ type: "window" }).then((list) => {
    for (const c of list) if ("focus" in c) { c.navigate(url); return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
