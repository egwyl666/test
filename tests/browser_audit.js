// Проверка страницы изнутри браузера (tests/test_browser_matrix.py): что видно и нажимается, а что сломано —
// вылезает вбок, обрезано родителем (как «Ещё ▾» в 2.29.0), закрыто другим элементом, мелко для пальца,
// нечитаемо по контрасту (WCAG: 4,5, крупный текст — 3), осталось по-русски в украинском интерфейсе.
async (opts) => {
  const out = { overflowX: 0, interactive: [], locked: [], clipped: [], covered: [], smallTargets: [], lowContrast: [],
                ruLeft: [], brokenImg: [], navVisible: [], emptyMain: false, textOverflow: [] };
  const vw = innerWidth;
  out.overflowX = Math.max(0, document.documentElement.scrollWidth - vw);
  out.coarse = matchMedia("(pointer: coarse)").matches;
  const label = (el) => ((el.getAttribute("aria-label") || el.innerText || el.value || el.title || el.getAttribute("href") || "")
    .replace(/\s+/g, " ").trim().slice(0, 60)) || el.tagName.toLowerCase() + (el.id ? "#" + el.id : "");
  const path = (el) => {
    const parts = [];
    for (let e = el; e && e !== document.body && parts.length < 4; e = e.parentElement) {
      parts.unshift(e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (e.classList.length ? "." + [...e.classList].slice(0, 2).join(".") : ""));
    }
    return parts.join(" > ");
  };
  const shown = (el) => {
    if (!el.getClientRects().length) return false;
    if (el.checkVisibility && !el.checkVisibility({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true })) return false;
    const closed = el.closest("details:not([open])");
    if (closed && !(el.tagName === "SUMMARY" && el.parentElement === closed) && !closed.querySelector(":scope > summary")?.contains(el)) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none" && parseFloat(cs.opacity) > 0.05;
  };
  const perms = new Set(((window.META && META.me && META.me.perms) || []));
  const els = [...document.querySelectorAll("button, a[href], summary, select, input:not([type=hidden]), textarea, label.btn")]
    .filter((el) => shown(el) && !el.closest(".modal.hidden, [hidden], .hidden"));
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const item = { t: label(el), p: path(el) };
    out.interactive.push(item.t);
    const perm = el.closest("[data-perm]");
    if ((perm && !perms.has(perm.dataset.perm)) || el.disabled) out.locked.push(item.t);
    // обрезано родителем с overflow (как «Ещё ▾» в меню, которое прокручивается вбок)
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.overflowX === "visible" && cs.overflowY === "visible") continue;
      const ar = a.getBoundingClientRect();
      const outV = r.bottom > ar.bottom + 2 || r.top < ar.top - 2;
      const outH = r.right > ar.right + 2 || r.left < ar.left - 2;
      const scrollV = a.scrollHeight > a.clientHeight + 2 && /(auto|scroll)/.test(cs.overflowY);
      const scrollH = a.scrollWidth > a.clientWidth + 2 && /(auto|scroll)/.test(cs.overflowX);
      if ((outV && !scrollV) || (outH && !scrollH)) { out.clipped.push({ ...item, by: path(a) }); break; }
      if (outV || outH) break;  // до него можно докрутить этот блок — не обрезано
    }
    if (opts.touch && out.coarse && (r.height < 30 || r.width < 30) && !el.matches("input[type=checkbox], input[type=radio], a:not(.btn)")) {
      out.smallTargets.push(`${item.t} (${Math.round(r.width)}×${Math.round(r.height)})`);
    }
  }
  // закрыто другим: прокрутить к элементу и посмотреть, что на самом деле под его серединой
  if (opts.hit) {
    const sample = els.filter((el) => !el.closest(".topbar, .bottom-nav, .help-fab, select")).slice(0, opts.hit);
    for (const el of sample) {
      el.scrollIntoView({ block: "center", inline: "nearest" });
      const r = el.getBoundingClientRect();
      if (r.width < 1) continue;
      const x = Math.min(Math.max(r.left + Math.min(r.width / 2, 20), 1), vw - 1);
      const y = Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1);
      const hit = document.elementFromPoint(x, y);
      if (hit && hit !== el && !el.contains(hit) && !hit.contains(el) && !(el.labels && [...el.labels].some((l) => l.contains(hit)))) {
        out.covered.push({ t: label(el), p: path(el), by: path(hit) });
      }
    }
    scrollTo(0, 0);
  }
  // контраст видимого текста
  const rgb = (c) => {
    const m = (c.match(/[\d.]+/g) || []).map(Number);
    if (c.startsWith("color(")) return [m[0] * 255, m[1] * 255, m[2] * 255, m[3] === undefined ? 1 : m[3]];  // color(srgb r g b / a)
    return m.length ? m : [0, 0, 0, 1];
  };
  const lum = ([r, g, b]) => [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; })
    .reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
  const bgOf = (el) => {
    for (let e = el; e; e = e.parentElement) {
      const c = rgb(getComputedStyle(e).backgroundColor);
      if ((c[3] === undefined ? 1 : c[3]) > 0.5) return c;
      if (getComputedStyle(e).backgroundImage !== "none" && e !== document.body) return null;  // картинка — не судим
    }
    return rgb(getComputedStyle(document.body).backgroundColor);
  };
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) {
    const text = n.nodeValue.replace(/\s+/g, " ").trim();
    if (text.length < 2) continue;
    const el = n.parentElement;
    if (!el || !shown(el) || el.closest("script, style, .hidden, [hidden], svg, .pp, .tile, option")) continue;
    const cs = getComputedStyle(el);
    if (opts.uk && /[ыэъё]/i.test(text) && !el.closest('[translate="no"], textarea, input')) {
      if (out.ruLeft.length < 30) out.ruLeft.push(`${text.slice(0, 70)}  ← ${path(el)}`);
    }
    const bg = bgOf(el);
    if (!bg) continue;
    const fg = rgb(cs.color);
    const alpha = (fg[3] === undefined ? 1 : fg[3]) * parseFloat(cs.opacity);
    if (alpha < 0.3) continue;
    const mix = fg.slice(0, 3).map((v, i) => v * alpha + bg[i] * (1 - alpha));
    const [a, b] = [lum(mix), lum(bg)].sort((x, y) => y - x);
    const ratio = (a + 0.05) / (b + 0.05);
    const disabled = el.closest("button:disabled, [disabled], [data-perm], .locked");
    const large = parseFloat(cs.fontSize) >= 18.5 || (parseFloat(cs.fontSize) >= 14 && parseInt(cs.fontWeight) >= 700);
    if (!disabled && ratio < (large ? 3 : 4.5) - 0.01) {
      const key = path(el) + "|" + text.slice(0, 30);
      if (!seen.has(key) && out.lowContrast.length < 40) {
        seen.add(key);
        out.lowContrast.push(`${ratio.toFixed(2)} «${text.slice(0, 50)}» ${cs.color} on rgb(${bg.slice(0, 3).join(",")}) ← ${path(el)}`);
      }
    }
  }
  for (const im of document.images) if (shown(im) && im.complete && im.naturalWidth === 0) out.brokenImg.push(im.src.slice(-80));
  out.navVisible = [...document.querySelectorAll(".topbar nav a[href]")].filter((a) => !a.classList.contains("hidden"))
    .map((a) => a.getAttribute("href"));
  const main = document.querySelector("main");
  out.emptyMain = !!main && main.innerText.trim().length < 20;
  return out;
}
