// Визуальный редактор описания: кнопки форматирования вместо ручного HTML.
// Под капотом остаётся обычное <textarea name="..."> — автосохранение, ИИ и превью работают с ним как раньше.

const RT_BUTTONS = [
  { cmd: "bold", label: "<b>Ж</b>", title: "Жирный (Ctrl+B)", state: true },
  { cmd: "italic", label: "<i>К</i>", title: "Курсив (Ctrl+I)", state: true },
  { cmd: "underline", label: "<u>Ч</u>", title: "Подчёркнутый (Ctrl+U)", state: true },
  { sep: true },
  { block: "h3", label: "Заголовок", title: "Подзаголовок внутри описания" },
  { block: "p", label: "Текст", title: "Обычный абзац" },
  { sep: true },
  { cmd: "insertUnorderedList", label: "• Список", title: "Маркированный список", state: true },
  { cmd: "insertOrderedList", label: "1. Список", title: "Нумерованный список", state: true },
  { action: "params", label: "▦ Характеристики", title: "Вставить таблицу из характеристик товара" },
  { sep: true },
  { action: "clear", label: "Очистить", title: "Убрать оформление у выделенного текста" },
  { cmd: "undo", label: "↶", title: "Отменить (Ctrl+Z)" },
  { cmd: "redo", label: "↷", title: "Повторить (Ctrl+Y)" },
  { spacer: true },
  { action: "source", label: "&lt;/&gt; HTML", title: "Показать код — для тех, кто знает HTML" },
];

// Приводит HTML из редактора/буфера к аккуратному виду, который понимает Prom.
function tidyHtml(html) {
  const doc = new DOMParser().parseFromString(`<div>${sanitizeHtml(html)}</div>`, "text/html");
  const root = doc.body.firstChild;
  const rename = (el, tag) => {
    const n = doc.createElement(tag);
    n.append(...el.childNodes);
    el.replaceWith(n);
    return n;
  };
  root.querySelectorAll("span").forEach((el) => el.replaceWith(...el.childNodes));
  root.querySelectorAll("strong").forEach((el) => rename(el, "b"));
  root.querySelectorAll("em").forEach((el) => rename(el, "i"));
  root.querySelectorAll("div").forEach((el) => rename(el, "p"));
  // голый текст на верхнем уровне заворачиваем в абзац
  let buffer = [];
  const flush = (before) => {
    if (!buffer.length) return;
    if (buffer.some((n) => n.textContent.trim() || n.nodeName === "BR")) {
      const p = doc.createElement("p");
      root.insertBefore(p, before);
      p.append(...buffer);
    } else {
      buffer.forEach((n) => n.remove());
    }
    buffer = [];
  };
  for (const node of Array.from(root.childNodes)) {
    const inline = node.nodeType === Node.TEXT_NODE || ["B", "I", "U", "A", "BR"].includes(node.nodeName);
    if (inline) buffer.push(node); else flush(node);
  }
  flush(null);
  // пустые абзацы в конце и лишние <br> в конце абзацев
  root.querySelectorAll("p, li, h3, h2, h4").forEach((el) => {
    while (el.lastChild && el.lastChild.nodeName === "BR") el.lastChild.remove();
  });
  root.querySelectorAll("p").forEach((el) => {
    if (!el.textContent.trim() && !el.querySelector("img")) el.remove();
  });
  // неразрывные пробелы, которые браузер ставит после жирного/курсива, заменяем обычными
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) walker.currentNode.nodeValue = walker.currentNode.nodeValue.replace(/\u00a0/g, " ");
  const out = root.innerHTML.trim();
  return root.textContent.trim() || root.querySelector("img") ? out : "";
}

function plainTextToHtml(text) {
  return text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean)
    .map((p) => `<p>${esc(p).replace(/\n/g, "<br>")}</p>`).join("");
}

function createRichText(textarea, { getParams = () => [], placeholder = "" } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "rt";
  wrap.innerHTML = `
    <div class="rt-toolbar">${RT_BUTTONS.map((b, i) => b.sep ? `<span class="rt-sep"></span>`
      : b.spacer ? `<span class="rt-spacer"></span>`
      : `<button type="button" data-i="${i}" title="${esc(b.title)}">${b.label}</button>`).join("")}</div>
    <div class="rt-area" contenteditable="true" data-placeholder="${esc(placeholder)}"></div>
    <div class="rt-foot"><span class="rt-count"></span><span class="rt-hint"></span></div>`;
  textarea.insertAdjacentElement("beforebegin", wrap);
  const area = wrap.querySelector(".rt-area");
  area.insertAdjacentElement("afterend", textarea);
  textarea.classList.add("rt-source", "hidden");
  const count = wrap.querySelector(".rt-count");
  let sourceMode = false;
  let syncing = false;

  // Любая запись в textarea.value снаружи (загрузка товара, ИИ, «вернуть значение поставщика») перерисовывает редактор.
  const native = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
  Object.defineProperty(textarea, "value", {
    configurable: true,
    get() { return native.get.call(this); },
    set(v) {
      native.set.call(this, v);
      if (!syncing) render();
    },
  });

  function render() {
    area.innerHTML = descriptionHtml(textarea.value);
    updateCount();
  }

  function updateCount() {
    const n = (sourceMode ? new DOMParser().parseFromString(textarea.value, "text/html").body.textContent : area.textContent).trim().length;
    count.textContent = n ? `${n} символов` : "";
    count.classList.toggle("warn", n > 0 && n < 150);
    wrap.querySelector(".rt-hint").textContent = n > 0 && n < 150 ? "коротковато — Prom лучше ранжирует подробные описания" : "";
  }

  function commit() {
    syncing = true;
    native.set.call(textarea, tidyHtml(area.innerHTML));
    syncing = false;
    updateCount();
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function updateButtons() {
    RT_BUTTONS.forEach((b, i) => {
      const btn = wrap.querySelector(`[data-i="${i}"]`);
      if (!btn) return;
      if (b.state) {
        let on = false;
        try { on = document.queryCommandState(b.cmd); } catch { /* ignore */ }
        btn.classList.toggle("on", on && area.contains(document.getSelection().anchorNode));
      }
      if (b.action === "source") btn.classList.toggle("on", sourceMode);
      if (!b.action || b.action !== "source") btn.disabled = sourceMode;
    });
  }

  function exec(cmd, value = null) {
    area.focus();
    document.execCommand(cmd, false, value);
    commit();
    updateButtons();
  }

  function paramsTable() {
    const rows = getParams().filter((p) => p.name && p.value);
    if (!rows.length) {
      toast("Сначала заполните характеристики товара ниже", "error");
      return;
    }
    exec("insertHTML", `<h3>Характеристики</h3><table><tbody>${rows.map((p) =>
      `<tr><td>${esc(p.name)}</td><td>${esc(p.value)}</td></tr>`).join("")}</tbody></table><p><br></p>`);
  }

  wrap.querySelector(".rt-toolbar").addEventListener("mousedown", (e) => {
    if (e.target.closest("button")) e.preventDefault();  // не терять выделение в тексте
  });
  wrap.querySelector(".rt-toolbar").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-i]");
    if (!btn) return;
    const b = RT_BUTTONS[Number(btn.dataset.i)];
    if (b.cmd) exec(b.cmd);
    else if (b.block) exec("formatBlock", `<${b.block}>`);
    else if (b.action === "params") paramsTable();
    else if (b.action === "clear") { exec("removeFormat"); exec("formatBlock", "<p>"); }
    else if (b.action === "source") {
      sourceMode = !sourceMode;
      if (sourceMode) {
        area.classList.add("hidden");
        textarea.classList.remove("hidden");
        textarea.focus();
      } else {
        render();
        textarea.classList.add("hidden");
        area.classList.remove("hidden");
      }
      updateButtons();
      updateCount();
    }
  });

  area.addEventListener("input", commit);
  textarea.addEventListener("input", () => { if (sourceMode) updateCount(); });

  // Вставка из Word/сайтов: чистим стили и мусор; картинки-файлы уходят в фото товара (обработчик страницы).
  area.addEventListener("paste", (e) => {
    const cd = e.clipboardData;
    if (!cd || (cd.files && cd.files.length)) return;
    e.preventDefault();
    e.stopPropagation();
    const html = cd.getData("text/html");
    const text = cd.getData("text/plain");
    document.execCommand("insertHTML", false, html ? tidyHtml(html) : plainTextToHtml(text));
    commit();
  });

  area.addEventListener("keydown", (e) => {
    // Enter в пустом редакторе должен создавать <p>, а не <div>
    if (!area.textContent && e.key.length === 1) document.execCommand("formatBlock", false, "<p>");
  });
  document.addEventListener("selectionchange", () => {
    if (area.contains(document.getSelection().anchorNode)) updateButtons();
  });

  try { document.execCommand("defaultParagraphSeparator", false, "p"); } catch { /* старые браузеры */ }
  render();
  updateButtons();
  return { render, area };
}
