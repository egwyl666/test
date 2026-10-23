"""Отзывы о магазине и сообщения с сайта (2.31.0).

**Отзывы.** В API Prom их нет (ни в документации, ни среди адресов). Prom показывает их всем на публичной странице
магазина «Відгуки» (`prom.ua/ua/opinions/list/<номер компании>`), а сама страница берёт их у публичного GraphQL
`prom.ua/graphql` (запрос `opinionListing`) — его программа и читает: только чтение, как браузер покупателя.
У отзыва — оценка, текст, теги («Швидко відправили»), товары и номер заказа (связь с «Заказами»), ответ магазина
(`comments`). **Ответить через API нельзя** — программа готовит ответ (сама или ✨ ИИ), его копируют и публикуют
в кабинете Prom; со следующей проверкой ответ подтягивается, и отзыв помечается «отвечено».

Номер компании узнаётся сам по публичной странице любого выгруженного товара (настройка `prom_company_id`).

**Сообщения с сайта** — форма «Написать продавцу» на Prom: `/messages/list`, ответ `/messages/reply` (до 1000
символов, уходит покупателю), «прочитано» — `/messages/set_status`.

Проверка — раз в 30 минут (свежие страницы отзывов; все — раз в сутки). Новый отзыв и новое сообщение —
уведомление («review», «site_message»); при самой первой проверке уведомлений нет: это старые отзывы."""

import json
import logging
import re
import threading
import time
from datetime import datetime

import httpx

from . import config, db, notify

log = logging.getLogger("promloader.reviews")

GRAPHQL = "https://prom.ua/graphql"
POLL_SECONDS = 30 * 60
PER_PAGE = 30
QUICK_PAGES = 3          # обычная проверка: самые свежие 90 отзывов (новые и недавние ответы)
FULL_PAGES = 50          # раз в сутки — все (до 1500), чтобы подтянуть ответы на старые
FULL_EVERY = 24 * 3600
NEGATIVE = 3             # оценка 1–3 — «негативный»: отдельный фильтр и заметное уведомление
REPLY_MAX = 1000         # предел Prom для ответа на сообщение с сайта
TIMEOUT = 30
UA = "Mozilla/5.0 (Prom Loader)"
_lock = threading.Lock()

QUERY = """query OpinionsListQuery($companyId: Int!, $page: Int, $perPage: Int) {
  opinionListing(companyId: $companyId, page: $page, perPage: $perPage) {
    hasNextPage
    opinions {
      id rating authorName commentWork dateCreated publishedWithoutRating isRemovedStatus
      tags { title }
      orderedProducts { id nameForCatalog }
      linkedOrder { id }
      comments { id message dateCreated authorName }
    }
  }
}"""


class ReviewsError(Exception):
    pass


def enabled() -> bool:
    return bool(config.get("prom_token"))


# ---------- номер компании ----------

def company_id(transport=None) -> int | None:
    """Номер компании на Prom: из настройки или по публичной странице любого выгруженного товара."""
    saved = db.get_setting("prom_company_id")
    if saved.isdigit():
        return int(saved)
    rows = db.query("SELECT prom_id FROM products WHERE prom_id IS NOT NULL ORDER BY synced_at DESC LIMIT 3")
    for r in rows:
        try:
            with httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=True,
                              headers={"User-Agent": UA}) as c:
                html = c.get(f"https://prom.ua/ua/p{int(r['prom_id'])}.html").text
        except httpx.HTTPError:
            continue
        m = re.search(r"opinions/list/(\d+)", html) or re.search(r'"companyId"\s*:\s*(\d+)', html)
        if m:
            db.set_setting("prom_company_id", m.group(1))
            return int(m.group(1))
    return None


def public_url() -> str:
    cid = db.get_setting("prom_company_id")
    return f"https://prom.ua/ua/opinions/list/{cid}" if cid.isdigit() else ""


# ---------- отзывы: чтение с Prom ----------

def _fetch_page(cid: int, page: int, transport=None) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport, headers={"User-Agent": UA}) as c:
            r = c.post(GRAPHQL, json={"operationName": "OpinionsListQuery", "query": QUERY,
                                      "variables": {"companyId": cid, "page": page, "perPage": PER_PAGE}})
    except httpx.HTTPError as exc:
        raise ReviewsError(f"Нет связи с Prom: {exc}") from None
    try:
        body = r.json()
    except ValueError:
        raise ReviewsError(f"Prom ответил не так, как ожидалось (HTTP {r.status_code})") from None
    listing = ((body or {}).get("data") or {}).get("opinionListing")
    if r.status_code >= 400 or listing is None:
        errors = "; ".join(str(e.get("message")) for e in (body or {}).get("errors") or [])
        raise ReviewsError(f"Prom не отдал отзывы: {errors or f'HTTP {r.status_code}'}")
    return listing


def _row(o: dict) -> dict:
    """Отзыв Prom → строка таблицы reviews."""
    comments = [c for c in o.get("comments") or [] if (c.get("message") or "").strip()]
    return {
        "id": int(o["id"]),
        "rating": None if o.get("publishedWithoutRating") else o.get("rating"),
        "author": o.get("authorName") or "",
        "text": (o.get("commentWork") or "").strip(),
        "tags": json.dumps([t.get("title") for t in o.get("tags") or [] if t.get("title")], ensure_ascii=False),
        "products": [{"prom_id": p.get("id"), "name": p.get("nameForCatalog") or ""} for p in o.get("orderedProducts") or []],
        "order_id": (o.get("linkedOrder") or {}).get("id"),
        "reply": "\n\n".join(c["message"].strip() for c in comments),
        "reply_at": max((c.get("dateCreated") or "" for c in comments), default=""),
        "created_at": o.get("dateCreated") or "",
        "removed": bool(o.get("isRemovedStatus")),
    }


def _store(rows: list[dict], first_run: bool) -> list[dict]:
    """Сохранить; вернуть новые отзывы (для уведомления)."""
    new = []
    now = db.now()
    with db.tx() as c:
        for r in rows:
            if r["removed"]:
                c.execute("DELETE FROM reviews WHERE id = ?", (r["id"],))
                continue
            for p in r["products"]:
                hit = c.execute("SELECT id FROM products WHERE prom_id = ?", (p["prom_id"],)).fetchone() if p["prom_id"] else None
                p["product_id"] = hit["id"] if hit else None
            products = json.dumps(r["products"], ensure_ascii=False)
            known = c.execute("SELECT id FROM reviews WHERE id = ?", (r["id"],)).fetchone()
            if known:
                c.execute("""UPDATE reviews SET rating = ?, author = ?, text = ?, tags = ?, products = ?, order_id = ?,
                             reply = ?, reply_at = ?, updated_at = ? WHERE id = ?""",
                          (r["rating"], r["author"], r["text"], r["tags"], products, r["order_id"], r["reply"],
                           r["reply_at"], now, r["id"]))
                continue
            c.execute("""INSERT INTO reviews (id, rating, author, text, tags, products, order_id, reply, reply_at, seen,
                         created_at, first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (r["id"], r["rating"], r["author"], r["text"], r["tags"], products, r["order_id"], r["reply"],
                       r["reply_at"], 1 if first_run else 0, r["created_at"], now, now))
            if not first_run:
                new.append(r)
    return new


def stars(rating) -> str:
    return "★" * int(rating) + "☆" * (5 - int(rating)) if rating else "без оценки"


def _notify_reviews(new: list[dict]) -> None:
    for r in new[:5]:
        bad = r["rating"] is not None and r["rating"] <= NEGATIVE
        what = ", ".join(p["name"] for p in r["products"])[:150]
        notify.send("review", f"{'⚠️ <b>Негативный отзыв</b>' if bad else '⭐ <b>Новый отзыв</b>'} {stars(r['rating'])} "
                              f"от {notify.esc(r['author'] or 'покупателя')}"
                              + (f"\n«{notify.esc(r['text'][:400])}»" if r["text"] else "")
                              + (f"\nТовар: {notify.esc(what)}" if what else "")
                              + "\nОтветить — «Отзывы» в программе.")
    if len(new) > 5:
        notify.send("review", f"⭐ Ещё новых отзывов: {len(new) - 5} — откройте «Отзывы» в программе.")


def poll_reviews(transport=None, full: bool | None = None) -> int:
    """Забрать отзывы с Prom. Возвращает, сколько новых. full — все страницы (иначе только свежие)."""
    if not _lock.acquire(blocking=False):
        return 0
    try:
        cid = company_id(transport)
        if cid is None:
            raise ReviewsError("Не удалось узнать номер магазина на Prom: отзывы появятся, когда хотя бы один товар "
                               "будет выгружен на Prom")
        first_run = not db.get_setting("reviews_initialized")
        if full is None:
            last_full = float(db.get_setting("reviews_full_at") or 0)
            full = first_run or time.time() - last_full > FULL_EVERY
        rows = []
        for page in range(1, (FULL_PAGES if full else QUICK_PAGES) + 1):
            listing = _fetch_page(cid, page, transport)
            rows += [_row(o) for o in listing.get("opinions") or []]
            if not listing.get("hasNextPage"):
                break
        new = _store(rows, first_run)
        if new:
            _notify_reviews(new)
        db.set_setting("reviews_initialized", "1")
        if full:
            db.set_setting("reviews_full_at", str(time.time()))
        db.set_setting("reviews_error", "")
        db.set_setting("reviews_last_poll", db.now())
        return len(new)
    except Exception as exc:
        db.set_setting("reviews_error", str(exc))
        raise
    finally:
        _lock.release()


# ---------- отзывы: в программе ----------

FILTERS = {
    "": "1 = 1",
    "no_reply": "reply = '' AND text != ''",       # с текстом и без ответа — их стоит ответить
    "negative": f"rating IS NOT NULL AND rating <= {NEGATIVE}",
    "with_text": "text != ''",
    "new": "seen = 0",
}


def list_reviews(flt: str = "", limit: int = 200) -> list[dict]:
    where = FILTERS.get(flt, FILTERS[""])
    out = []
    for r in db.query(f"SELECT * FROM reviews WHERE {where} ORDER BY created_at DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["products"] = json.loads(d["products"] or "[]")
        d["order_known"] = bool(d["order_id"] and db.query_one("SELECT 1 FROM orders WHERE id = ?", (d["order_id"],)))
        out.append(d)
    return out


def stats() -> dict:
    r = db.query_one(f"""SELECT COUNT(*) AS total, AVG(rating) AS avg, SUM(rating IS NOT NULL AND rating <= {NEGATIVE}) AS negative,
                         SUM(reply = '' AND text != '') AS no_reply, SUM(seen = 0) AS new FROM reviews""")
    return {"total": r["total"], "avg": round(r["avg"], 2) if r["avg"] is not None else None,
            "negative": r["negative"] or 0, "no_reply": r["no_reply"] or 0, "new": r["new"] or 0,
            "public_url": public_url(), "last_poll": db.get_setting("reviews_last_poll"),
            "error": db.get_setting("reviews_error")}


def mark_seen() -> int:
    with db.tx() as c:
        return c.execute("UPDATE reviews SET seen = 1 WHERE seen = 0").rowcount


def save_draft(review_id: int, text: str) -> dict:
    with db.tx() as c:
        if not c.execute("UPDATE reviews SET draft = ? WHERE id = ?", (str(text or "")[:2000], review_id)).rowcount:
            raise KeyError(review_id)
    return {"ok": True}


REVIEW_SYSTEM = """Ты помогаешь продавцу интернет-магазина на Prom.ua публично отвечать на отзывы покупателей.
Пиши на украинском языке, тепло и по-человечески, 1–3 предложения, без шаблонных канцеляризмов и без эмодзи-спама.
Хороший отзыв — поблагодари, можно упомянуть товар. Плохой или средний — извинись, не спорь и не оправдывайся,
предложи решить вопрос в чате Prom. Не обещай компенсаций, скидок и сроков, которых нет в данных. Ответ увидят все
покупатели магазина. Верни JSON с одним полем reply — текст ответа."""


def ai_draft(review_id: int) -> dict:
    """Черновик ответа на отзыв (ИИ). Сохраняется как draft — его правят, копируют и публикуют на Prom."""
    from . import ai, aiprice

    r = db.query_one("SELECT * FROM reviews WHERE id = ?", (review_id,))
    if r is None:
        raise KeyError(review_id)
    products = ", ".join(p["name"] for p in json.loads(r["products"] or "[]"))
    tags = ", ".join(json.loads(r["tags"] or "[]"))
    context = "\n".join(x for x in (
        f"Оценка: {r['rating']} из 5." if r["rating"] else "Без оценки.",
        f"Покупатель: {r['author']}." if r["author"] else "",
        f"Товары: {products}." if products else "",
        f"Отмеченное покупателем: {tags}." if tags else "",
        f"Текст отзыва: {r['text']}" if r["text"] else "Текста нет — только оценка.",
    ) if x)
    reply, used = ai.chat_reply(context, system=REVIEW_SYSTEM, action="review_reply")
    save_draft(review_id, reply)
    used = dict(used or {})
    if used.get("cost_usd") is not None:
        used["cost_uah"] = aiprice.to_uah(used["cost_usd"])
    return {"reply": reply, "usage": used}


# ---------- сообщения с сайта ----------

def _store_messages(items: list[dict], first_run: bool) -> list[dict]:
    new = []
    now = db.now()
    with db.tx() as c:
        for m in items:
            if not m.get("id") or m.get("status") == "deleted":
                continue
            prom_pid = m.get("product_id")
            hit = c.execute("SELECT id FROM products WHERE prom_id = ?", (prom_pid,)).fetchone() if prom_pid else None
            known = c.execute("SELECT status FROM site_messages WHERE id = ?", (m["id"],)).fetchone()
            values = (m.get("client_full_name") or "", m.get("phone") or "", m.get("subject") or "",
                      (m.get("message") or "").strip(), m.get("status") or "unread", prom_pid, hit["id"] if hit else None)
            if known:
                c.execute("""UPDATE site_messages SET client = ?, phone = ?, subject = ?, text = ?, status = ?,
                             prom_product_id = ?, product_id = ?, updated_at = ? WHERE id = ?""", (*values, now, m["id"]))
                continue
            c.execute("""INSERT INTO site_messages (client, phone, subject, text, status, prom_product_id, product_id, id,
                         created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (*values, m["id"], m.get("date_created") or now, now))
            if not first_run and values[4] == "unread":
                new.append(m)
    return new


async def poll_messages(client) -> int:
    first_run = not db.get_setting("site_messages_initialized")
    items = await client.list_messages(limit=100)
    new = _store_messages(items, first_run)
    for m in new[:3]:
        notify.send("site_message", f"✉️ <b>{notify.esc(m.get('client_full_name') or 'Покупатель')}</b> пишет через сайт "
                                    f"Prom:\n{notify.esc((m.get('message') or '')[:500])}")
    db.set_setting("site_messages_initialized", "1")
    return len(new)


def list_messages(limit: int = 200) -> list[dict]:
    rows = db.query("""SELECT m.*, p.name AS product_name FROM site_messages m LEFT JOIN products p ON p.id = m.product_id
                       ORDER BY m.created_at DESC LIMIT ?""", (limit,))
    return [dict(r) for r in rows]


def unread_messages() -> int:
    return db.query_one("SELECT COUNT(*) AS n FROM site_messages WHERE status = 'unread'")["n"]


async def reply_message(client, message_id: int, text: str, who: str = "") -> dict:
    text = (text or "").strip()
    if not text:
        raise ReviewsError("Напишите ответ")
    if len(text) > REPLY_MAX:
        raise ReviewsError(f"Ответ длиннее {REPLY_MAX} символов — Prom такой не примет, сократите")
    if db.query_one("SELECT 1 FROM site_messages WHERE id = ?", (message_id,)) is None:
        raise KeyError(message_id)
    await client.reply_message(message_id, text)
    await client.set_message_status([message_id], "read")
    with db.tx() as c:
        c.execute("UPDATE site_messages SET reply = ?, replied_at = ?, replied_by = ?, status = 'read', updated_at = ? "
                  "WHERE id = ?", (text, db.now(), who, db.now(), message_id))
    return {"ok": True}


async def mark_message_read(client, message_id: int) -> None:
    row = db.query_one("SELECT status FROM site_messages WHERE id = ?", (message_id,))
    if row is None:
        raise KeyError(message_id)
    if row["status"] != "read":
        await client.set_message_status([message_id], "read")
        with db.tx() as c:
            c.execute("UPDATE site_messages SET status = 'read', updated_at = ? WHERE id = ?", (db.now(), message_id))


def ai_message_draft(message_id: int) -> dict:
    from . import ai, aiprice

    m = db.query_one("""SELECT m.*, p.name AS pname, p.price, p.currency, p.presence, p.quantity FROM site_messages m
                        LEFT JOIN products p ON p.id = m.product_id WHERE m.id = ?""", (message_id,))
    if m is None:
        raise KeyError(message_id)
    lines = []
    if m["pname"]:
        qty = f", {m['quantity']} шт." if m["quantity"] else "."
        lines.append(f"Товар: {m['pname']}. Цена: {m['price']} {m['currency']}. Наличие: {m['presence']}{qty}")
    lines.append(f"Покупатель: {m['client']}." if m["client"] else "")
    lines.append(f"Тема: {m['subject']}" if m["subject"] else "")
    lines.append(f"Сообщение покупателя: {m['text']}")
    reply, used = ai.chat_reply("\n".join(x for x in lines if x))
    used = dict(used or {})
    if used.get("cost_usd") is not None:
        used["cost_uah"] = aiprice.to_uah(used["cost_usd"])
    return {"reply": reply[:REPLY_MAX], "usage": used}


def overview() -> dict:
    return {"reviews": stats(), "messages_unread": unread_messages(),
            "checked": datetime.now().isoformat(timespec="minutes")}
