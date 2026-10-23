"""2.31.0: отзывы о магазине (публичная страница Prom) и сообщения с сайта (API «Написать продавцу»)."""

import asyncio
import json

import httpx
import pytest

from promloader import ai, db, notify, orders, products, reviews, sync, today
from promloader.prom_api import PromClient

from .conftest import sign_in
from .test_novaposhta import prom_order


def opinion(oid, rating=5, text="", author="Ірина Б.", order=None, products_=(), reply=None, removed=False, no_rating=False):
    return {"id": oid, "rating": rating, "authorName": author, "commentWork": text, "dateCreated": f"2026-10-{20 - oid % 10:02d}T10:00:00",
            "publishedWithoutRating": no_rating, "isRemovedStatus": removed, "tags": [{"title": "Швидко відправили"}],
            "orderedProducts": [{"id": pid, "nameForCatalog": name} for pid, name in products_],
            "linkedOrder": {"id": order} if order else None,
            "comments": [{"id": 1, "message": reply, "dateCreated": "2026-10-21T09:00:00", "authorName": "Магазин"}] if reply else []}


class FakePromPublic:
    """prom.ua: страница товара (номер компании) и GraphQL отзывов по страницам."""

    def __init__(self, pages):
        self.pages = pages
        self.graphql = []

    def handler(self, request):
        if request.url.path.startswith("/ua/p"):
            return httpx.Response(200, text='<html>…"companyOpinionsUrl":"https://prom.ua/ua/opinions/list/4225569"…</html>')
        if request.url.path == "/graphql":
            body = json.loads(request.content)
            self.graphql.append(body["variables"])
            page = body["variables"]["page"]
            items = self.pages[page - 1] if page <= len(self.pages) else []
            return httpx.Response(200, json={"data": {"opinionListing": {"hasNextPage": page < len(self.pages), "opinions": items}}})
        return httpx.Response(404)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest.fixture
def tg(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append((event, text)))
    return sent


def shop_with_product():
    pid = products.create({"name": "Ліхтар Mos-13", "price": 500})
    with db.tx() as c:
        c.execute("UPDATE products SET prom_id = 3167167243, synced_at = ? WHERE id = ?", (db.now(), pid))
    return pid


def test_company_id_found_from_public_product_page(data_dir):
    shop_with_product()
    fake = FakePromPublic([[]])
    assert reviews.company_id(fake.transport) == 4225569
    assert db.get_setting("prom_company_id") == "4225569"
    assert reviews.public_url() == "https://prom.ua/ua/opinions/list/4225569"


def test_first_poll_is_quiet_then_new_review_notifies_and_links(data_dir, tg):
    pid = shop_with_product()
    orders.store([prom_order(430873116)], notify_new=False)
    fake = FakePromPublic([[opinion(1, text="Супер")], [opinion(2, rating=4)]])
    assert reviews.poll_reviews(fake.transport) == 0 and tg == []  # старые отзывы — без уведомлений
    assert reviews.stats()["total"] == 2 and len(fake.graphql) == 2  # первый раз — все страницы
    fake.pages = [[opinion(3, rating=2, text="Довго відправляли", order=430873116,
                           products_=[(3167167243, "Ліхтар Mos-13")]), opinion(1, text="Супер")]]
    assert reviews.poll_reviews(fake.transport, full=False) == 1
    assert tg and tg[0][0] == "review" and "Негативный отзыв" in tg[0][1] and "Довго відправляли" in tg[0][1]
    r = reviews.list_reviews("negative")[0]
    assert r["id"] == 3 and r["products"][0]["product_id"] == pid and r["order_known"] and r["tags"] == ["Швидко відправили"]
    assert [x["id"] for x in reviews.list_reviews("new")] == [3]
    reviews.mark_seen()
    assert reviews.list_reviews("new") == []


def test_reply_on_prom_is_picked_up_and_removed_reviews_disappear(data_dir, tg):
    shop_with_product()
    fake = FakePromPublic([[opinion(5, text="Дякую"), opinion(6, text="Все ок")]])
    reviews.poll_reviews(fake.transport)
    assert reviews.stats()["no_reply"] == 2
    fake.pages = [[opinion(5, text="Дякую", reply="Дякуємо, що обрали нас!"), opinion(6, text="Все ок", removed=True)]]
    reviews.poll_reviews(fake.transport, full=False)
    items = reviews.list_reviews()
    assert [x["id"] for x in items] == [5] and items[0]["reply"] == "Дякуємо, що обрали нас!"
    assert reviews.stats()["no_reply"] == 0


def test_review_without_rating_and_graphql_error(data_dir):
    shop_with_product()
    fake = FakePromPublic([[opinion(7, no_rating=True)]])
    reviews.poll_reviews(fake.transport)
    assert reviews.list_reviews()[0]["rating"] is None
    bad = httpx.MockTransport(lambda r: httpx.Response(200, json={"errors": [{"message": "boom"}]}))
    with pytest.raises(reviews.ReviewsError, match="boom"):
        reviews.poll_reviews(bad, full=False)
    assert "boom" in reviews.stats()["error"]


def test_no_company_yet_is_explained(data_dir):
    with pytest.raises(reviews.ReviewsError, match="хотя бы один товар"):
        reviews.poll_reviews(FakePromPublic([[]]).transport)


def test_ai_draft_is_saved_and_uses_review_task(data_dir, monkeypatch):
    shop_with_product()
    reviews.poll_reviews(FakePromPublic([[opinion(8, rating=1, text="Прийшов битий")]]).transport)
    seen = {}

    def fake_reply(context, transport=None, system=ai.CHAT_SYSTEM, action="chat_reply"):
        seen.update(context=context, system=system, action=action)
        return "Перепрошуємо! Напишіть нам у чат Prom — вирішимо.", {"cost_usd": 0.001}
    monkeypatch.setattr(ai, "chat_reply", fake_reply)
    r = reviews.ai_draft(8)
    assert "Перепрошуємо" in r["reply"] and seen["system"] is reviews.REVIEW_SYSTEM and seen["action"] == "review_reply"
    assert "Оценка: 1 из 5" in seen["context"] and "Прийшов битий" in seen["context"]
    assert reviews.list_reviews()[0]["draft"] == r["reply"]


class FakeMessages:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def handler(self, request):
        self.calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/messages/list"):
            return httpx.Response(200, json={"messages": self.items})
        return httpx.Response(200, json={"processed_ids": [1]})

    def client(self):
        return PromClient("t", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(self.handler))


def message(mid, text="Чи є в наявності?", status="unread"):
    return {"id": mid, "date_created": "2026-10-22T10:00:00+00:00", "client_full_name": "Олег", "phone": "+380671112233",
            "message": text, "subject": "Питання", "status": status, "product_id": 3167167243}


def test_site_messages_poll_reply_and_read(data_dir, tg):
    pid = shop_with_product()
    fake = FakeMessages([message(11, status="read")])

    async def run(fn):
        async with fake.client() as c:
            return await fn(c)
    assert asyncio.run(run(reviews.poll_messages)) == 0 and tg == []  # первая проверка — тихо
    fake.items.append(message(12))
    assert asyncio.run(run(reviews.poll_messages)) == 1 and tg[0][0] == "site_message"
    m = {x["id"]: x for x in reviews.list_messages()}
    assert m[12]["product_id"] == pid and reviews.unread_messages() == 1
    with pytest.raises(reviews.ReviewsError):
        asyncio.run(run(lambda c: reviews.reply_message(c, 12, "x" * 1001)))
    asyncio.run(run(lambda c: reviews.reply_message(c, 12, "Так, є в наявності.", "Олег")))
    assert ("POST", "/api/v1/messages/reply", {"id": 12, "message": "Так, є в наявності."}) in fake.calls
    assert ("POST", "/api/v1/messages/set_status", {"status": "read", "ids": [12]}) in fake.calls
    assert reviews.unread_messages() == 0 and {x["id"]: x for x in reviews.list_messages()}[12]["reply"]


def test_today_and_meta_show_reviews(anon, tg):
    c = sign_in(anon)
    shop_with_product()
    fake = FakePromPublic([[]])
    reviews.poll_reviews(fake.transport)
    fake.pages = [[opinion(9, rating=1, text="Погано")]]
    reviews.poll_reviews(fake.transport, full=False)
    keys = {x["key"] for x in today.attention()}
    assert {"reviews_negative", "reviews_new"} <= keys
    assert c.get("/api/meta").json()["reviews_unseen"] == 1
    page = c.get("/reviews")
    assert page.status_code == 200 and "Отзывы и вопросы" in page.text


def test_permissions(anon, monkeypatch):
    shop_with_product()
    reviews.poll_reviews(FakePromPublic([[opinion(10, text="Ок")]]).transport)
    viewer = sign_in(anon, login="v31", role="viewer")
    assert viewer.get("/api/reviews").status_code == 200
    assert viewer.post("/api/reviews/seen").status_code == 200  # «просмотрено» — только в программе
    assert viewer.post("/api/reviews/10/draft", json={"text": "x"}).status_code == 403
    assert viewer.post("/api/reviews/10/ai-draft").status_code == 403
    assert viewer.post("/api/reviews/messages/1/reply", json={"text": "x"}).status_code == 403
    monkeypatch.setattr(reviews, "poll_reviews", lambda *a, **k: 0)
    monkeypatch.setattr(sync, "make_client", FakeMessages([]).client)
    assert viewer.post("/api/reviews/refresh").status_code == 200  # проверить новые — только чтение Prom
