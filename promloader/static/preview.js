// Превью карточки: приблизительно так товар будет выглядеть на Prom.ua.

const PREVIEW_TEXT = {
  ru: {
    presence: { available: "В наличии", order: "Под заказ", not_available: "Нет в наличии" },
    buy: "Купить", write: "Написать", sku: "Код", desc: "Описание", params: "Характеристики",
    noImage: "Здесь будет фото.<br>Перетащите файлы в окно", noName: "Название товара", seller: "Продавец",
    vendor: "Производитель", country: "Страна производитель", per: "за",
  },
  ua: {
    presence: { available: "В наявності", order: "Під замовлення", not_available: "Немає в наявності" },
    buy: "Купити", write: "Написати", sku: "Код", desc: "Опис", params: "Характеристики",
    noImage: "Тут буде фото.<br>Перетягніть файли у вікно", noName: "Назва товару", seller: "Продавець",
    vendor: "Виробник", country: "Країна виробник", per: "за",
  },
};

function previewFields(p, lang) {
  const ua = lang === "ua";
  const price = p.price === "" || p.price === null || p.price === undefined ? null : Number(p.price);
  const oldPrice = p.old_price === "" || p.old_price === null || p.old_price === undefined ? null : Number(p.old_price);
  const sale = price !== null && oldPrice !== null && oldPrice > price;
  return {
    t: PREVIEW_TEXT[lang] || PREVIEW_TEXT.ru,
    name: (ua ? p.name_ua || p.name : p.name) || "",
    description: ua ? p.description_ua || p.description : p.description,
    price, oldPrice, sale,
    discount: sale ? Math.round((1 - price / oldPrice) * 100) : 0,
    images: p.images || [],
    presence: p.presence || "available",
  };
}

function renderProductPage(p, lang = "ru", activeImage = 0) {
  const f = previewFields(p, lang);
  const t = f.t;
  const img = f.images[Math.min(activeImage, f.images.length - 1)];
  const params = (p.params || []).filter((x) => x.name && x.value);
  if (p.vendor) params.unshift({ name: t.vendor, value: p.vendor });
  if (p.country) params.push({ name: t.country, value: p.country });
  const crumbs = ["Prom.ua", p.group_name || "…"].map((c) => `<span>${esc(c)}</span>`).join("");

  return `
  <div class="pp">
    <div class="crumbs">${crumbs}</div>
    <div class="top">
      <div class="gallery">
        <div class="main">${img ? `<img src="${esc(img.src)}" alt="">` : `<div class="noimg">${t.noImage}</div>`}</div>
        ${f.images.length > 1 ? `<div class="thumbs">${f.images.map((im, i) =>
          `<img src="${esc(im.src)}" data-idx="${i}" class="${i === activeImage ? "active" : ""}" alt="">`).join("")}</div>` : ""}
      </div>
      <div>
        <h1 class="title">${esc(f.name) || `<span style="color:#c2c6cc">${t.noName}</span>`}</h1>
        <div class="sku">${t.sku}: ${esc(p.external_id || "—")}</div>
        <div class="presence ${esc(f.presence)}">${esc(t.presence[f.presence] || "")}</div>
        <div class="prices">
          <span class="price-now ${f.sale ? "sale" : ""}">${f.price !== null ? esc(formatPrice(f.price, p.currency)) : "— ₴"}</span>
          ${f.sale ? `<span class="price-old">${esc(formatPrice(f.oldPrice, p.currency))}</span><span class="discount">−${f.discount}%</span>` : ""}
        </div>
        <div class="unit">${p.unit ? `${t.per} 1 ${esc(p.unit)}` : ""}</div>
        <div class="buy"><div class="b1">${t.buy}</div><div class="b2">${t.write}</div></div>
        <div class="seller">${t.seller}: <b>${esc(META.shop_name || "Ваш магазин")}</b></div>
      </div>
    </div>
    <div class="tabs"><span class="active">${t.desc}</span><span>${t.params}</span></div>
    <div class="desc">${descriptionHtml(f.description)}</div>
    ${params.length ? `<h3>${t.params}</h3><table class="params">${params.map((x) =>
      `<tr><td>${esc(x.name)}</td><td>${esc(x.value)}</td></tr>`).join("")}</table>` : ""}
  </div>`;
}

function renderTile(p, lang = "ru") {
  const f = previewFields(p, lang);
  const t = f.t;
  const img = f.images[0];
  return `
  <div class="tile">
    <div class="img">${img ? `<img src="${esc(img.src)}" alt="" loading="lazy">` : `<div class="noimg">нет фото</div>`}</div>
    <div class="t-name">${esc(f.name) || `<span style="color:#c2c6cc">${t.noName}</span>`}</div>
    <div class="t-presence ${esc(f.presence)}">${esc(t.presence[f.presence] || "")}</div>
    <div class="t-old">${f.sale ? esc(formatPrice(f.oldPrice, p.currency)) : ""}</div>
    <div class="t-price ${f.sale ? "sale" : ""}">${f.price !== null ? esc(formatPrice(f.price, p.currency)) : "— ₴"}</div>
    <div class="t-buy">${t.buy}</div>
  </div>`;
}
