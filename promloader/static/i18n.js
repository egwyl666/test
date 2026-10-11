// Язык интерфейса: русский (исходный, в коде) или украинский — словарём static/i18n-uk.js поверх готовой страницы.
// Переводится всё, что видно: текст, подсказки (title), placeholder, окна confirm/prompt, заголовок вкладки, ответы
// программы. Данные пользователя (названия товаров, описания, заказы, прайсы) помечены translate="no" и не трогаются.
// Подключается в <head> каждой страницы — до того, как страница нарисуется, чтобы не мелькал русский текст.
(function () {
  const KEY = "promloader-lang";
  let lang = "ru";
  try { lang = localStorage.getItem(KEY) === "uk" ? "uk" : "ru"; } catch { /* хранилище недоступно — русский */ }

  const I18N = (window.I18N = { lang, locale: lang === "uk" ? "uk-UA" : "ru-RU", missing: new Set() });
  I18N.set = (next) => {
    try { localStorage.setItem(KEY, next); } catch { /* не запомнится — не страшно */ }
    location.reload();
  };
  window.uiLocale = () => I18N.locale;
  I18N.tr = (s) => s;
  if (lang !== "uk") return;

  document.documentElement.lang = "uk";
  const CYR = /[А-Яа-яЁёІіЇїЄєҐґ]/;
  const norm = (s) => s.replace(/\s+/g, " ").trim();
  const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  // ---------- словарь ----------
  const exact = new Map();
  const patterns = [];
  for (const [ru, uk] of Object.entries(window.I18N_UK || {})) {
    if (!ru.includes("{}")) { exact.set(norm(ru), uk); continue; }
    const parts = ru.split("{}");
    const literal = parts.join("");
    const letters = (literal.match(/[А-Яа-яЁёІіЇїЄєҐґ]/g) || []).length;
    if (!letters) continue;
    patterns.push({
      // подстановка может оказаться пустой: необязательный хвост `${условие ? "…" : ""}`
      re: new RegExp("^" + parts.map(escRe).join("([\\s\\S]*?)") + "$"),
      key: parts.reduce((a, b) => (norm(b).length > norm(a).length ? b : a), "").trim(),
      size: literal.length,
      // короткий шаблон («{} ч», «{} в {}») задел бы и название товара — он срабатывает, только когда
      // каждая подстановка — число или известная фраза («по будням в 09:00»)
      short: letters < 3,
      uk,
    });
    // «Товаров: {}», когда подстановка — отдельный тег: на экране текстовый узел просто «Товаров:»
    if (letters >= 3 && parts.length === 2 && (parts[0] === "" || parts[1] === "")) {
      const rest = norm(parts.join(""));
      const ukRest = uk.split("{}");
      if (ukRest.length === 2 && (ukRest[0] === "" || ukRest[1] === "") && CYR.test(rest) && !exact.has(rest)) {
        exact.set(rest, norm(ukRest.join("")));
      }
    }
  }
  patterns.sort((a, b) => b.size - a.size);  // длинные (точные) шаблоны раньше коротких

  function lookup(key, depth) {
    const hit = exact.get(key);
    if (hit !== undefined) return hit;
    for (const p of patterns) {
      if (p.key && !key.includes(p.key)) continue;
      const m = p.re.exec(key);
      if (!m) continue;
      let i = 0, ok = true;
      const out = p.uk.replace(/\{(\d*)\}/g, (_, n) => {
        const v = m[n ? Number(n) : ++i] ?? "";
        const t = sub(v, depth);
        if (t === undefined) { if (p.short) ok = false; return v; }
        return t;
      });
      if (ok) return out;
    }
    // «Prom: …», «Anthropic Claude — нет ключа» — имя сервиса (латиницей) перед фразой программы
    const first = key.search(CYR);
    if (first > 0) {
      for (const sep of key.slice(0, first).matchAll(/:?\s+/g)) {
        if (!/[A-Za-z0-9]/.test(key.slice(0, sep.index))) continue;
        const cut = sep.index + sep[0].length;
        const t = lookup(key.slice(cut), depth);
        if (t !== undefined) return key.slice(0, cut) + t;
      }
    }
    // «· в прайсе 6 товаров ·» — разделитель от соседнего куска шаблона
    const m = /^([·•|]\s*)?([\s\S]*?)(\s*[·•|])?$/.exec(key);
    if (m[2] && (m[1] || m[3])) {
      const [a, b, c] = [m[1] || "", m[2], m[3] || ""];
      for (const [pre, mid, post] of [["", a + b, c], [a, b + c, ""], [a, b, c]]) {
        const t = mid.trim() && mid !== key ? lookup(mid.trim(), depth) : undefined;
        if (t !== undefined) return pre + t + post;
      }
    }
    // «08.10, 20:58 · Курс валют (…)» — куски, склеенные страницей через « · »
    if (key.includes(" · ")) {
      const parts = key.split(" · ").map((x) => (CYR.test(x) ? lookup(x, depth) : x));
      if (parts.every((x) => x !== undefined)) return parts.join(" · ");
    }
    return undefined;
  }

  // подстановка сама может быть фразой программы («…: {}» + текст ошибки сервера) — переводим и её.
  // undefined — в подстановке текст, которого нет в словаре (название товара, ответ Prom)
  function sub(v, depth) {
    if (!CYR.test(v)) return v;
    if (depth >= 3) return undefined;
    const key = norm(v);
    const hit = lookup(key, depth + 1);
    if (hit !== undefined) return hit;
    // перечисление («имя бакета, публичный адрес», «Пн, Ср») — по частям
    if (key.includes(", ")) {
      const parts = key.split(", ").map((x) => (CYR.test(x) ? lookup(x, depth + 1) : x));
      if (parts.every((x) => x !== undefined)) return parts.join(", ");
    }
    return undefined;
  }

  function tr(text) {
    if (!text || !CYR.test(text)) return text;
    const key = norm(text);
    const out = lookup(key, 0);
    if (out === undefined) {
      // в «missing» — только русский текст: уже украинский (свой перевод, собранный страницей из кусков) не нужен
      if (!(/[іїєґІЇЄҐ']/.test(key) && !/[ыэъёЫЭЪЁ]/.test(key))) I18N.missing.add(key);
      return text;
    }
    return text.match(/^\s*/)[0] + out + text.match(/\s*$/)[0];
  }
  I18N.tr = tr;

  // ---------- страница ----------
  const SKIP = '[translate="no"], [contenteditable="true"], textarea, script, style, noscript';
  const ATTRS = ["title", "placeholder", "aria-label", "alt"];
  const done = new WeakMap();  // узел -> текст, который поставили мы (не переводить свой же перевод)

  function translateText(node) {
    const parent = node.parentElement;
    // уже убранный со страницы узел (страница перерисовала блок) не трогаем: вне страницы не видно и translate="no"
    if (!parent || !node.isConnected || parent.closest(SKIP)) return;
    if (done.get(node) === node.data) return;
    const out = tr(node.data);
    if (out !== node.data) node.data = out;
    done.set(node, node.data);
  }

  const doneAttrs = new WeakMap();  // элемент -> {атрибут: значение, которое поставили мы}

  function translateAttrs(el) {
    if (!el.isConnected || el.closest(SKIP)) return;
    for (const name of ATTRS) {
      const v = el.getAttribute(name);
      if (!v || !CYR.test(v)) continue;
      const mine = doneAttrs.get(el) || {};
      if (mine[name] === v) continue;
      const out = tr(v);
      mine[name] = out;
      doneAttrs.set(el, mine);
      if (out !== v) el.setAttribute(name, out);
    }
    if (el.tagName === "INPUT" && /^(button|submit)$/i.test(el.type) && CYR.test(el.value)) el.value = tr(el.value);
  }

  function translateTree(root) {
    if (root.nodeType === Node.TEXT_NODE) { translateText(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE || !root.isConnected || root.closest(SKIP)) return;
    translateAttrs(root);
    root.querySelectorAll("[title], [placeholder], [aria-label], [alt], input[type=button], input[type=submit]").forEach(translateAttrs);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) translateText(n);
  }

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type === "childList") m.addedNodes.forEach(translateTree);
      else if (m.type === "characterData") translateText(m.target);
      else if (m.type === "attributes" && m.target.nodeType === Node.ELEMENT_NODE) translateAttrs(m.target);
    }
  });

  // системные окна — тоже по-украински
  const nativeConfirm = window.confirm.bind(window);
  const nativePrompt = window.prompt.bind(window);
  const nativeAlert = window.alert.bind(window);
  window.confirm = (msg) => nativeConfirm(tr(String(msg ?? "")));
  window.prompt = (msg, value) => nativePrompt(tr(String(msg ?? "")), value);
  window.alert = (msg) => nativeAlert(tr(String(msg ?? "")));

  // пока страница не переведена — её не видно (не дольше 1,5 с)
  const hide = document.createElement("style");
  hide.textContent = "html.i18n-wait body { visibility: hidden; }";
  (document.head || document.documentElement).appendChild(hide);
  document.documentElement.classList.add("i18n-wait");
  const reveal = () => document.documentElement.classList.remove("i18n-wait");
  setTimeout(reveal, 1500);

  function start() {
    translateTree(document.documentElement);
    observer.observe(document.documentElement, {
      childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS,
    });
    reveal();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
