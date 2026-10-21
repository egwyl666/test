// Сканер штрихкодов камерой телефона: ТТН Новой Почты на этикетке, номер заказа, штрихкод товара.
// Распознаёт сам браузер (BarcodeDetector — Chrome на Android, Edge, Chrome на компьютере), а где его нет (iPhone,
// Firefox) — ZXing (static/vendor, загружается только тогда, 336 КБ). Картинка никуда не отправляется. Камеру браузер
// даёт только на https (через Cloudflare) или на самом компьютере — по адресу Wi‑Fi http://192.168… остаётся поле
// для ввода номера.

const SCAN_FORMATS = ["code_128", "code_39", "ean_13", "ean_8", "itf", "qr_code", "upc_a", "data_matrix"];
const ZXING_SRC = "/static/vendor/zxing-0.21.3.min.js";
const SCAN_WIDTH = 720;  // кадр для ZXing уменьшаем: штрихкоду ТТН хватает, а старый телефон не тормозит

function cameraAvailable() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) && window.isSecureContext;
}

let zxingLoading = null;
function loadZxing() {
  if (window.ZXing) return Promise.resolve(window.ZXing);
  if (!zxingLoading) {
    zxingLoading = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = `${ZXING_SRC}?v=${encodeURIComponent((window.META && META.version) || "")}`;
      s.onload = () => (window.ZXing ? resolve(window.ZXing) : reject(new Error("ZXing")));
      s.onerror = () => { zxingLoading = null; reject(new Error("ZXing")); };
      document.head.appendChild(s);
    });
  }
  return zxingLoading;
}

// Распознаватель кадра видео: detect(video) → строка или "". Сначала свой у браузера, иначе ZXing.
async function makeDetector() {
  if ("BarcodeDetector" in window) {
    try {
      const native = new BarcodeDetector({ formats: SCAN_FORMATS });
      return async (video) => (await native.detect(video)).map((f) => f.rawValue).find(Boolean) || "";
    } catch { /* формат не поддерживается — ZXing */ }
  }
  const Z = await loadZxing();
  const hints = new Map();
  hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, [Z.BarcodeFormat.CODE_128, Z.BarcodeFormat.CODE_39, Z.BarcodeFormat.EAN_13,
    Z.BarcodeFormat.EAN_8, Z.BarcodeFormat.ITF, Z.BarcodeFormat.UPC_A, Z.BarcodeFormat.QR_CODE, Z.BarcodeFormat.DATA_MATRIX]);
  hints.set(Z.DecodeHintType.TRY_HARDER, true);
  const reader = new Z.MultiFormatReader();
  reader.setHints(hints);
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  return async (video) => {
    const w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return "";
    const scale = Math.min(1, SCAN_WIDTH / w);
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    try {
      const bitmap = new Z.BinaryBitmap(new Z.HybridBinarizer(new Z.HTMLCanvasElementLuminanceSource(canvas)));
      return reader.decodeWithState(bitmap).getText() || "";
    } catch {
      return "";  // NotFoundException: на этом кадре штрихкода нет
    }
  };
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
    if (!cameraAvailable()) {
      status.textContent = I18N.tr("Камера работает только по адресу https (через Cloudflare) или на самом компьютере — введите номер вручную.");
      input.focus();
      return;
    }
    let detect;
    try {
      detect = await makeDetector();
    } catch {
      status.textContent = I18N.tr("Не удалось загрузить распознавание штрихкодов — введите номер вручную.");
      input.focus();
      return;
    }
    try {
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
          const code = await detect(video);
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
