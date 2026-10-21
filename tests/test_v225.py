"""2.25.0: «посылка ждёт» в Telegram, рисковый покупатель (пометка, наложенный платёж только с подтверждением,
просьба о предоплате), ИИ подбирает группу и категорию Prom."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from promloader import ai, aibulk, aicategory, db, notify, novaposhta, orders, phone, products, sync
from promloader.main import app
from promloader.prom_api import PromClient

from .conftest import sign_in
from .test_novaposhta import FakeNP, _room, prom_order, ready_sender


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: out.append((event, text)))
    return out


@pytest.fixture
def np(monkeypatch):
    fake = FakeNP()
    real = httpx.Client
    monkeypatch.setattr(novaposhta.httpx, "Client",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(fake.handler)}))
    return fake


@pytest.fixture
def prom_chat(monkeypatch):
    out = []

    def prom(request):
        if request.url.path.endswith("/chat/send_message"):
            out.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "success", "data": {"message_id": 1}})
        return httpx.Response(200, json={"status": "success"})
    monkeypatch.setattr(sync, "make_client", lambda: PromClient("t", "https://x", transport=httpx.MockTransport(prom)))
    return out


def ago(days=0, hours=0):
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


# ---------- 1. «посылка ждёт» в Telegram ----------

def arrive(np, order_id, ttn, days):
    np.status[ttn] = "7"
    novaposhta.track()
    with db.tx() as c:
        c.execute("UPDATE shipments SET arrived_at = ? WHERE order_id = ?", (ago(days), order_id))


def waiting(sent):
    return [t for e, t in sent if e == "parcel_waiting_alert"]


def test_waiting_parcel_alert_once_with_details(np, sent):
    orders.store([prom_order(61, ttn="20451560371673", status="received")], notify_new=False)
    novaposhta.track()
    arrive(np, 61, "20451560371673", 2)
    novaposhta.track()
    assert waiting(sent) == []  # 2 дня — рано
    with db.tx() as c:
        c.execute("UPDATE shipments SET arrived_at = ? WHERE order_id = 61", (ago(3, 1),))
    novaposhta.track()
    novaposhta.track()
    msgs = waiting(sent)
    assert len(msgs) == 1  # не каждый раз, а один раз на «ожидание»
    text = msgs[0]
    assert "№61" in text and "Коваль Олена" in text and "+380671112233" in text and "20451560371673" in text
    assert "Відділення №1" in text and "3 дн." in text and "Напомнить в чат" in text
    assert "«Заказы» → поиск по номеру" in text  # доступа из интернета нет — ссылки localhost в Telegram не даём
    assert db.query_one("SELECT waiting_alert_sent_at FROM shipments WHERE order_id = 61")["waiting_alert_sent_at"]


def test_no_alert_when_buyer_already_reminded(np, sent):
    orders.store([prom_order(62, ttn="20451560371673", status="received")], notify_new=False)
    novaposhta.track()
    arrive(np, 62, "20451560371673", 4)
    with db.tx() as c:
        c.execute("UPDATE shipments SET reminded_at = ? WHERE order_id = 62", (ago(hours=1),))
    novaposhta.track()
    assert waiting(sent) == []


def test_new_waiting_window_alerts_again(np, sent):
    orders.store([prom_order(63, ttn="20451560371673", status="received")], notify_new=False)
    novaposhta.track()
    arrive(np, 63, "20451560371673", 5)
    novaposhta.track()
    assert len(waiting(sent)) == 1
    # посылку переадресовали — прибыла снова (новое «ожидание»), а покупателю тогда напоминали раньше
    with db.tx() as c:
        c.execute("UPDATE shipments SET arrived_at = ?, reminded_at = ?, waiting_alert_sent_at = ? WHERE order_id = 63",
                  (ago(3, 2), ago(10), ago(4)))
    novaposhta.track()
    assert len(waiting(sent)) == 2


def test_alert_link_when_phone_access_is_on(np, sent, monkeypatch):
    monkeypatch.setattr(phone, "remote_enabled", lambda: True)
    monkeypatch.setattr(phone, "remote_url", lambda: "https://abc-def.trycloudflare.com/")
    orders.store([prom_order(64, ttn="20451560371673", status="received")], notify_new=False)
    novaposhta.track()
    arrive(np, 64, "20451560371673", 3)
    novaposhta.track()
    assert "https://abc-def.trycloudflare.com/orders?q=64" in waiting(sent)[0]


def test_waiting_alert_event_reaches_existing_recipients():
    with db.tx() as c:
        c.execute("INSERT INTO tg_recipients (chat_id, name, events, created_at) VALUES ('1', 'Олег', '[\"order\"]', ?)",
                  (db.now(),))
    db.set_setting("tg_events_version", "5")
    db.set_setting("telegram_token", "1:x")
    assert "parcel_waiting_alert" in notify.recipients()[0]["events"]
    assert "parcel_waiting_alert" in notify.DEFAULT_EVENTS and notify.NEW_EVENTS[6] == ["parcel_waiting_alert"]


# ---------- 2. рисковый покупатель ----------

def risky_history(new_id=72, status="pending"):
    orders.store([prom_order(71, status="canceled")], notify_new=False)
    with db.tx() as c:
        c.execute("INSERT INTO shipments (order_id, ttn, own, stage, created_at) VALUES (71, '20450000000071', 1, 'refused', ?)",
                  (db.now(),))
    orders.store([prom_order(new_id, status=status)], notify_new=False)


def test_new_risky_order_is_flagged_and_telegram_warns(sent):
    orders.store([prom_order(71, status="canceled")], notify_new=False)
    with db.tx() as c:
        c.execute("INSERT INTO shipments (order_id, ttn, own, stage, created_at) VALUES (71, '20450000000071', 1, 'refused', ?)",
                  (db.now(),))
    db.set_setting("orders_initialized", "1")
    orders.store([prom_order(73)])  # новый заказ с Prom
    assert db.query_one("SELECT prepay_state FROM orders WHERE id = 73")["prepay_state"] == "required"
    text = next(t for e, t in sent if e == "order")
    assert "Рисковый покупатель (отказов: 1, отмен: 1) — рекомендуется предоплата" in text


def test_flag_setting_off_and_old_orders_flagged_on_view(client):
    db.set_setting("auto_flag_risky_cod", "0")
    risky_history()
    items = {o["id"]: o for o in client.get("/api/orders").json()["items"]}
    assert items[72]["prepay_state"] == "" and items[72]["buyer"]["risky"]  # значок риска есть, пометки нет
    client.post("/api/np/settings", json={"auto_flag_risky_cod": True})
    assert client.get("/api/np/settings").json()["auto_flag_risky_cod"] is True
    items = {o["id"]: o for o in client.get("/api/orders").json()["items"]}
    assert items[72]["prepay_state"] == "required"   # пришёл до включения — помечен при просмотре
    assert items[71]["prepay_state"] == ""            # отменённый не помечаем


def test_cod_ttn_for_risky_buyer_needs_confirmation(np, client):
    ready_sender()
    risky_history()
    client.get("/api/orders")  # пометка
    form = client.get("/api/orders/72/ttn/draft").json()
    assert form["cod"] is True
    r = client.post("/api/orders/72/ttn", json=form)
    assert r.status_code == 409 and r.json()["risky"] and "Наложенный платёж заблокирован" in r.json()["detail"]
    assert not [c for c in np.calls if c[0] == "InternetDocument.save"]
    r = client.post("/api/orders/72/ttn", json=form | {"confirm_risky": True})
    assert r.status_code == 200 and r.json()["ttn"]


def test_prepaid_risky_order_ships_without_confirmation(np, client):
    ready_sender()
    risky_history()
    client.get("/api/orders")
    form = client.get("/api/orders/72/ttn/draft").json()
    r = client.post("/api/orders/72/ttn", json=form | {"cod": False})
    assert r.status_code == 400 and "наложенным платежом" in r.json()["detail"]  # заказ с наложкой — как и раньше
    client.post("/api/orders/72/prepay", json={"state": "ok"})  # «Предоплата получена»
    assert client.post("/api/orders/72/ttn", json=form | {"cod": False}).status_code == 200


def test_cleared_flag_allows_cod(np, client):
    ready_sender()
    risky_history()
    client.get("/api/orders")
    assert client.post("/api/orders/72/prepay", json={"state": "ok"}).json() == {"prepay_state": "ok"}
    form = client.get("/api/orders/72/ttn/draft").json()
    assert client.post("/api/orders/72/ttn", json=form).status_code == 200
    items = {o["id"]: o for o in client.get("/api/orders").json()["items"]}
    assert items[72]["prepay_state"] == "ok"  # снятую пометку программа обратно не ставит
    assert client.post("/api/orders/72/prepay", json={"state": "bogus"}).status_code == 400


def test_prepay_request_message(client, prom_chat):
    risky_history()
    _room("555_abc", order_id=72)
    operator = sign_in(TestClient(app), "ira", "пароль-1234", "operator")
    r = operator.post("/api/orders/72/prepay-request")
    assert r.status_code == 200
    assert prom_chat[0]["user_id"] == "555" and prom_chat[0]["body"].startswith("Вітаємо! Дякуємо за замовлення #72.")
    assert "передоплату" in prom_chat[0]["body"]
    row = db.query_one("SELECT prepay_state, prepay_requested_at FROM orders WHERE id = 72")
    assert row["prepay_state"] == "requested" and row["prepay_requested_at"]
    assert client.post("/api/orders/72/prepay-request").status_code == 409 and len(prom_chat) == 1
    viewer = sign_in(TestClient(app), "ivan", "пароль-1234", "viewer")
    assert viewer.post("/api/orders/72/prepay-request").status_code == 403
    assert client.post("/api/orders/999/prepay-request").status_code == 404


def test_prepay_request_without_chat(client, prom_chat):
    risky_history()
    r = client.post("/api/orders/72/prepay-request")
    assert r.status_code == 400 and "нет чата" in r.json()["detail"] and prom_chat == []
    assert db.query_one("SELECT prepay_state FROM orders WHERE id = 72")["prepay_state"] == ""


# ---------- 3. ИИ подбирает группу и категорию Prom ----------

def shop_with_groups():
    from promloader import categories
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    products.create({"name": "Павербанк A", "price": 500, "group_name": "Павербанки", "external_id": "G-1"})
    products.create({"name": "Чохол B", "price": 100, "group_name": "Чохли", "external_id": "G-2"})
    with db.tx() as c:
        categories.remember(c, 500901, "Зовнішні акумулятори", "Електроніка / Зовнішні акумулятори")
        categories.remember(c, 380210, "Чохли для телефонів", "Аксесуари / Чохли для телефонів")
    return [products.create({"name": n, "price": 100, "external_id": f"D-{i}"})
            for i, n in enumerate(["Павербанк 20000 мАг", "Чохол iPhone 15", "Гірлянда Роса"])]


def fake_gemini(monkeypatch, answer: dict):
    calls = []

    def gemini(s, prompt, transport=None, action="", switch=True, system="", fields=(), image=None):
        calls.append({"prompt": prompt, "fields": fields, "system": system, "action": action})
        return {k: answer.get(k, "") for k in fields}, {"model": "gemini-x"}
    monkeypatch.setattr(ai, "_gemini", gemini)
    return calls


def test_match_categories_only_accepts_listed_choices(monkeypatch):
    ids = shop_with_groups()
    calls = fake_gemini(monkeypatch, {"i0_group": "павербанки", "i0_category": "500901",
                                      "i1_group": "Чохли для ноутбуків", "i1_category": "[380210]",
                                      "i2_group": "", "i2_category": "999999"})
    results, _ = aicategory.match([products.get(i) for i in ids])
    assert results[0] == {"group_name": "Павербанки", "target_category_id": 500901}  # регистр — неважен
    assert results[1] == {"target_category_id": 380210}  # выдуманной группы нет в списке — отброшена
    assert isinstance(results[2], ai.AIError)            # ничего подходящего
    assert len(calls) == 1 and calls[0]["action"] == "match_category"  # три товара — один запрос
    assert "- Павербанки" in calls[0]["prompt"] and "500901: Електроніка / Зовнішні акумулятори" in calls[0]["prompt"]
    assert set(calls[0]["fields"]) == {"i0_group", "i0_category", "i1_group", "i1_category", "i2_group", "i2_category"}


def test_match_category_bulk_job_end_to_end(monkeypatch):
    ids = shop_with_groups()
    products.update(ids[1], {"group_name": "Чохли"})  # группа уже есть — «только где пусто» её не трогает
    fake_gemini(monkeypatch, {"i0_group": "Павербанки", "i0_category": "500901",
                              "i1_group": "Павербанки", "i1_category": "380210", "i2_group": "", "i2_category": ""})
    job = aibulk.create(ids, "match_category")
    assert job["label"] == aicategory.LABEL
    while aibulk.process_one() != "idle":
        pass
    a, b, c = (products.get(i) for i in ids)
    assert a["group_name"] == "Павербанки" and a["target_category_id"] == 500901
    assert b["group_name"] == "Чохли" and b["target_category_id"] == 380210
    assert c["group_name"] == "" and not c["target_category_id"]
    assert aibulk.get(job["id"])["counts"] == {"done": 2, "error": 1}
    row = db.query_one("SELECT source, new FROM product_changes WHERE product_id = ? AND field = 'target_category_id' "
                       "ORDER BY id DESC LIMIT 1", (ids[0],))
    assert row["source"].startswith("ИИ") and "500901" in row["new"]
    # откат возвращает как было
    aibulk.revert(job["id"])
    assert products.get(ids[0])["group_name"] == "" and not products.get(ids[0])["target_category_id"]


def test_match_category_skips_done_and_needs_choices(monkeypatch, client):
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    pid = products.create({"name": "Товар", "price": 100, "external_id": "N-1"})
    r = client.post("/api/ai/bulk", json={"ids": [pid], "action": "match_category"})
    assert r.status_code == 400 and "Выбирать не из чего" in r.json()["detail"]
    ids = shop_with_groups()
    products.update(ids[0], {"group_name": "Павербанки", "target_category_id": 500901})
    calls = fake_gemini(monkeypatch, {})
    job = aibulk.create([ids[0]], "match_category")
    while aibulk.process_one() != "idle":
        pass
    assert calls == [] and aibulk.get(job["id"])["counts"] == {"skipped": 1}  # уже заполнено — без запроса
    meta = client.get("/api/meta").json()["ai"]
    assert meta["bulk_only"]["match_category"] == aicategory.LABEL and "match_category" not in meta["actions"]
    assert client.post("/api/ai/bulk/estimate", json={"ids": ids, "action": "match_category"}).json()["batch"] == aicategory.BATCH
