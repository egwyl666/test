// «Настройки» по вкладкам: раньше все разделы шли одной длинной лентой, и нужный приходилось искать прокруткой.
// Ссылки вида /settings#ai (из окна ИИ, баннеров) открывают вкладку, где лежит этот раздел.
(function () {
  const tabs = document.getElementById("settings-tabs");
  if (!tabs) return;
  const panels = () => document.querySelectorAll("main > [data-tab]");

  function show(tab, anchor) {
    if (!tabs.querySelector(`[data-tab="${tab}"]`)) tab = "prom";
    tabs.querySelectorAll("a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
    panels().forEach((el) => el.classList.toggle("tab-hidden", el.dataset.tab !== tab));
    if (anchor && anchor.scrollIntoView) anchor.scrollIntoView({ block: "start" });
  }

  function fromHash() {
    const id = decodeURIComponent(location.hash.slice(1));
    const el = id ? document.getElementById(id) : null;
    const owner = el && el.closest("main > [data-tab]");
    if (owner) show(owner.dataset.tab, el);
    else show("prom");
  }

  tabs.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-tab]");
    if (!a) return;
    e.preventDefault();
    history.replaceState(null, "", a.getAttribute("href"));
    show(a.dataset.tab);
    window.scrollTo({ top: 0 });
  });
  window.addEventListener("hashchange", fromHash);
  fromHash();
})();
