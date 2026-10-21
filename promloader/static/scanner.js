// Сканер штрихкодов камерой телефона: ТТН Новой Почты на этикетке, номер заказа, штрихкод товара.
// Распознаёт сам браузер (BarcodeDetector — Chrome на Android, Edge, Chrome на компьютере с камерой) — без библиотек
// и без отправки картинки куда-либо. Где распознавания нет (iPhone/Safari) или камера недоступна (адрес по Wi‑Fi
// http://192.168… — браузер даёт камеру только на https или localhost), остаётся поле для ввода номера.

const SCAN_FORMATS = ["code_128", "code_39", "ean_13", "ean_8", "itf", "qr_code", "upc_a", "data_matrix"];

function scannerSupported() {
  return "BarcodeDetector" in window && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) && window.isSecureContext;
}

// opts: title, hint, placeholder, onCode(code, close) → Promise (может вернуть {text, bad}). Возвращает «закрыть».
function openScanner({ title = "Сканировать", hint = "", placeholder = "", onCode }) {
  const box = document.createElement("div");
  box.className = "modal scanner-modal";
  box.innerHTML = `<div class="modal-box scanner-box">
    <div class="toolbar" style="margin:0 0 8px"><h2 style="margin:0">${esc(title)}</h2><span class="spacer"></span>
      <button type="button" class="btn small" data-close title="Закрыть (Esc)">✕</button></div>
    <div class="scanner-view hidden"><video playsinline muted></video><div class="scanner-frame"></div></div>
    <p class="small muted scanner-hint">${esc(hint)}</p>
    <form class="row scanner-manual" style="gap:8px">
      <input inputmode="numeric" autocomplete="off" placeholder="${esc(placeholder)}">
      <button class="btn primary" type="submit" style="flex:0 0 auto">Найти</button>
    </form>
    <p class="small scanner-status"></p>
  </div>`;
  document.body.appendChild(box);
  const video = box.querySelector("video");
  const status = box.querySelector(".scanner-status");
  const input = box.querySelector("input");
  let stream = null;
  let closed = false;
  let busyCode = false;

  const stop = () => {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
  };
  const close = () => {
    closed = true;
    stop();
    box.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  box.querySelector("[data-close]").onclick = close;
  box.addEventListener("click", (e) => { if (e.target === box) close(); });

  async function handle(code) {
    if (busyCode || !code) return;
    busyCode = true;
    status.textContent = `${I18N.tr("Ищу")} ${code}…`;
    status.className = "small scanner-status";
    try {
      // onCode может вернуть {text, bad} — итог для камеры, которая остаётся открытой (режим упаковки)
      const res = await onCode(code, close);
      if (res && res.text) {
        status.textContent = res.text;
        status.className = `small scanner-status ${res.bad ? "err-text" : res.warn ? "warn-text" : "ok-text"}`;
      }
    } catch (err) {
      status.textContent = err.message;
      status.className = "small scanner-status err-text";
    }
    busyCode = false;
  }

  box.querySelector("form").addEventListener("submit", (e) => {
    e.preventDefault();
    handle(input.value.trim());
  });

  (async () => {
    if (!scannerSupported()) {
      status.textContent = I18N.tr(!window.isSecureContext
        ? "Камера работает только по адресу https (через Cloudflare) или на самом компьютере — введите номер вручную."
        : "Этот браузер не распознаёт штрихкоды (так бывает на iPhone) — введите номер вручную.");
      input.focus();
      return;
    }
    let detector;
    try {
      detector = new BarcodeDetector({ formats: SCAN_FORMATS });
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
    } catch (err) {
      status.textContent = I18N.tr("Нет доступа к камере — разрешите её в браузере или введите номер вручную.");
      input.focus();
      return;
    }
    if (closed) { stop(); return; }
    video.srcObject = stream;
    box.querySelector(".scanner-view").classList.remove("hidden");
    await video.play().catch(() => {});
    status.textContent = I18N.tr("Наведите камеру на штрихкод ТТН или номер заказа");
    const tick = async () => {
      if (closed || !stream) return;
      if (!busyCode && video.readyState >= 2) {
        try {
          const found = await detector.detect(video);
          const code = found.map((f) => f.rawValue).find(Boolean);
          if (code) {
            if (navigator.vibrate) navigator.vibrate(80);
            await handle(code);
          }
        } catch { /* кадр не распознался — следующий */ }
      }
      setTimeout(tick, 250);
    };
    tick();
  })();
  return close;
}
