"""Чат с покупателями: опрос Prom (подменённый HTTP), связь с товаром и заказом, «непрочитано», уведомления только на
сообщения покупателя, ответ и «прочитано» (что именно уходит на Prom), черновик ИИ."""

import asyncio
import json

import httpx
import pytest

from promloader import ai, chat, db, notify, products, sync
from promloader.prom_api import PromClient

ROOM = "14174056_4225569_buyer"


def msg(mid, body, mine=False, status="unread", when="2026-10-15T10:00:00", **extra):
    return {"id": mid, "room_id": "room-uuid", "room_ident": ROOM, "body": body, "date_sent": when, "type": "message",
            "status": status, "is_sender": mine, "user_name": "Магазин" if mine else "Олена", "user_phone": "",
            "context_item_type": None, "context_item_id": None, "attachments": None, **extra}


class FakeProm:
    """Prom: комнаты, история, приём ответа и «прочитано». Всё, что ушло на Prom, — в self.sent."""

    def __init__(self):
        self.messages = [
            {**msg(1, None, when="2026-10-15T09:59:00"), "type": "context", "context_item_type": "product",
             "context_item_id": 777},
            msg(2, "Добрий день! Є в наявності?"),
        ]
        self.last_id = 2
        self.sent = []
        self.history_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/rooms"):
            return httpx.Response(200, json={"status": "ok", "data": {"rooms": [
                {"id": "room-uuid", "ident": ROOM, "project": "promua", "status": "active",
                 "last_message_id": self.last_id, "date_sent": "2026-10-15T10:00:00"}]}})
        if path.endswith("/chat/messages_history"):
            assert request.url.params["room_ident"] == ROOM
            self.history_calls += 1
            return httpx.Response(200, json={"status": "ok", "data": {"messages": self.messages}})
        if path.endswith("/chat/send_message") or path.endswith("/chat/mark_message_read"):
            self.sent.append((path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={"status": "ok", "data": {"message_id": 900}})
        return httpx.Response(404, json={})

    def client(self):
        return PromClient("t", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(self.handler))


@pytest.fixture
def prom(monkeypatch):
    fake = FakeProm()
    monkeypatch.setattr(sync, "make_client", fake.client)
    return fake


@pytest.fixture
def tg(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append((event, text)))
    return sent


def poll(fake):
    async def run():
        async with fake.client() as c:
            return await chat.poll(c)
    return asyncio.run(run())


def test_first_poll_stores_links_product_and_does_not_notify(prom, tg):
    pid = products.create({"name": "Кружка", "price": 100})
    with db.tx() as c:
        c.execute("UPDATE products SET prom_id = 777 WHERE id = ?", (pid,))
    assert poll(prom) == 0 and tg == []           # старая переписка — без уведомлений
    room = chat.rooms()[0]
    assert room["buyer_name"] == "Олена" and room["unread"] == 1 and room["product"]["id"] == pid
    assert chat.unseen() == 1
    data = chat.messages(ROOM)
    assert [m["type"] for m in data["messages"]] == ["context", "message"]
    assert data["messages"][0]["product"]["id"] == pid

    # ничего не поменялось — историю не запрашиваем
    calls = prom.history_calls
    poll(prom)
    assert prom.history_calls == calls

    # новое сообщение покупателя — уведомление; своё сообщение магазина — нет
    prom.messages += [msg(3, "А доставка коли?", when="2026-10-15T10:05:00"),
                      msg(4, "Сьогодні відправимо", mine=True, status="read", when="2026-10-15T10:06:00")]
    prom.last_id = 4
    assert poll(prom) == 1
    assert len(tg) == 1 and tg[0][0] == "chat" and "А доставка коли?" in tg[0][1] and "Олена" in tg[0][1]
    assert chat.rooms()[0]["last_message"] == "Сьогодні відправимо" and chat.unseen() == 2


def test_send_and_mark_read_payloads(prom, tg, client):
    poll(prom)
    r = client.post(f"/api/chat/rooms/{ROOM}/send", json={"text": "Так, є. Відправимо сьогодні."})
    assert r.status_code == 200
    # Prom ждёт user_id из room_ident и project — так шлёт и открытая библиотека Prom API
    assert prom.sent[0] == ("send_message", {"user_id": "14174056", "body": "Так, є. Відправимо сьогодні.", "project": "promua"})
    assert r.json()["messages"][-1]["body"] == "Так, є. Відправимо сьогодні." and r.json()["messages"][-1]["is_my"]
    assert client.post(f"/api/chat/rooms/{ROOM}/send", json={"text": "  "}).status_code == 400

    r = client.post(f"/api/chat/rooms/{ROOM}/read").json()
    assert prom.sent[1] == ("mark_message_read", {"room_id": "room-uuid", "message_id": 2})
    assert r["unseen"] == 0 and client.get("/api/meta").json()["chat_unseen"] == 0
    assert client.get("/api/chat/rooms", params={"unread": "true"}).json()["rooms"] == []
    assert client.get("/api/chat/rooms", params={"q": "олена"}).json()["rooms"][0]["buyer_name"] == "Олена"


def test_prom_error_status_is_shown(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"status": "error", "message": {"room_id": ["Unknown field."]}})

    async def run():
        async with PromClient("t", "https://x", transport=httpx.MockTransport(handler)) as c:
            await c.chat_messages(ROOM)
    with pytest.raises(Exception, match="Unknown field"):
        asyncio.run(run())


def test_ai_draft_uses_product_and_history(prom, monkeypatch, client):
    pid = products.create({"name": "Кружка", "price": 100, "quantity": 5,
                           "params": [{"name": "Обʼєм", "value": "350 мл"}]})
    with db.tx() as c:
        c.execute("UPDATE products SET prom_id = 777 WHERE id = ?", (pid,))
    poll(prom)
    seen = {}

    def fake_reply(context, transport=None):
        seen["context"] = context
        return "Добрий день! Так, є в наявності.", {"model": "gemini-x", "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.001}
    monkeypatch.setattr(ai, "chat_reply", fake_reply)
    r = client.post(f"/api/chat/rooms/{ROOM}/ai-draft").json()
    assert r["reply"].startswith("Добрий день") and r["usage"]["model"] == "gemini-x"
    assert "Кружка" in seen["context"] and "350 мл" in seen["context"] and "Є в наявності?" in seen["context"]
    assert prom.sent == []  # черновик ничего не отправляет


def test_page_and_rooms_api(client):
    assert client.get("/chat").status_code == 200
    body = client.get("/api/chat/rooms").json()
    assert body["rooms"] == [] and body["unseen"] == 0 and body["enabled"] is True


# ---------- 2.20.0: готовые ответы из настоящих данных и ИИ с заказом и ТТН ----------

def _room_with(order_id=None, product_id=None, buyer_text="Добрий день, коли відправите?", phone=""):
    from promloader import db
    with db.tx() as c:
        c.execute("INSERT INTO chat_rooms (room_ident, buyer_phone, order_id, product_id, last_at, updated_at) "
                  "VALUES ('555_q', ?, ?, ?, ?, ?)", (phone, order_id, product_id, db.now(), db.now()))
        c.execute("INSERT INTO chat_messages (id, room_ident, sender, body, is_my, created_at, type, status) "
                  "VALUES (1, '555_q', 'buyer', ?, 0, ?, 'message', '')", (buyer_text, db.now()))
    return "555_q"


def test_quick_replies_use_real_order_and_ttn_in_buyer_language():
    from promloader import chat, db, orders, products
    from tests.test_novaposhta import prom_order
    orders.store([prom_order(1001)], notify_new=False)
    with db.tx() as c:
        c.execute("INSERT INTO shipments (order_id, ttn, stage, status, created_at) VALUES "
                  "(1001, '20450000000001', 'arrived', 'Прибув у відділення', ?)", (db.now(),))
    pid = products.create({"name": "Кружка белая", "name_ua": "Кухоль білий", "price": 150, "presence": "available"})
    room = _room_with(order_id=1001, product_id=pid)
    quick = {q["key"]: q for q in chat.quick_replies(room)}
    assert "Кухоль білий" in quick["stock"]["text"] and "150 грн" in quick["stock"]["text"]
    assert "20450000000001" in quick["ttn"]["text"] and "відділенні" in quick["ttn"]["text"]
    assert quick["check"]["text"].startswith("Зараз")
    assert quick["ttn"]["label"] == "📦 Номер ТТН и статус"


def test_quick_replies_always_ukrainian_and_order_by_phone():
    """Покупатель пишет по-русски — ответы всё равно по-украински (решение продавца); заказ найден по телефону."""
    from promloader import ai, chat, orders
    from tests.test_novaposhta import prom_order
    orders.store([prom_order(1002, status="received")], notify_new=False)
    room = _room_with(buyer_text="Здравствуйте, когда отправите заказ?", phone="+380671112233")
    quick = {q["key"]: q for q in chat.quick_replies(room)}
    assert quick["order"]["text"] == "Ваше замовлення #1002: прийнято, готуємо до відправлення."
    assert quick["check"]["text"].startswith("Зараз")
    assert "stock" not in quick and "ttn" not in quick
    assert "всегда на украинском" in ai.CHAT_SYSTEM


def test_ai_draft_gets_order_and_ttn_facts(monkeypatch):
    from promloader import ai, chat, db, orders
    from tests.test_novaposhta import prom_order
    orders.store([prom_order(1003)], notify_new=False)
    with db.tx() as c:
        c.execute("INSERT INTO shipments (order_id, ttn, stage, status, created_at) VALUES "
                  "(1003, '20450000000003', 'on_the_way', 'В дорозі', ?)", (db.now(),))
    room = _room_with(order_id=1003)
    seen = []
    monkeypatch.setattr(ai, "chat_reply", lambda context, transport=None: (seen.append(context), ("Відповідь", {}))[1])
    assert chat.ai_draft(room)["reply"] == "Відповідь"
    assert "ТТН 20450000000003" in seen[0] and "Заказ покупателя №1003" in seen[0]


def test_messages_include_quick(client):
    room = _room_with()
    r = client.get(f"/api/chat/rooms/{room}/messages").json()
    assert [q["key"] for q in r["quick"]] == ["check"]
