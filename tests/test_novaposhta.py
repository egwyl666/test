"""Новая Почта на подменённом API (живую ТТН не создаём: покупателю пришло бы SMS):
черновик из заказа Prom, создание ТТН (наложенный платёж, плательщик, получатель), номер в Prom, удаление,
этикетка, отслеживание (ТТН из кабинета Prom тоже), уведомления «прибыла / не забирают / отказ»."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from promloader import db, notify, novaposhta, orders, sync
from promloader.prom_api import PromClient

WH = "1ec09dce-e1c2-11e3-8c4a-0050568002cf"


def prom_order(oid, cod=True, ttn=None, status="pending"):
    return {"id": oid, "status": status, "date_created": "2026-10-15T10:00:00", "client_first_name": "Олена",
            "client_last_name": "Коваль", "client_second_name": "", "phone": "+380671112233",
            "delivery_recipient": {"first_name": "Олена", "last_name": "Коваль", "second_name": "Іванівна", "phone": "+380671112233"},
            "delivery_option": {"id": 1, "name": "Нова Пошта"},
            "delivery_address": "смт. Арбузинка, №1: вул. Центральна, 86",
            "delivery_provider_data": {"provider": "nova_poshta", "type": "W2W", "recipient_warehouse_id": WH,
                                       "declaration_number": ttn, "recipient_address": {"warehouse_id": WH}},
            "payment_option": {"id": 2, "name": "Наложенный платеж" if cod else "Оплата картой"},
            "full_price": "1 460 грн", "price": "1 460 грн", "products": []}


class FakeNP:
    def __init__(self):
        self.calls = []
        self.status = {}  # номер ТТН → код статуса

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "printMarking" in str(request.url):
            return httpx.Response(200, content=b"%PDF-1.4 label")
        body = json.loads(request.content)
        m, p = f"{body['modelName']}.{body['calledMethod']}", body["methodProperties"]
        self.calls.append((m, p, body["apiKey"]))
        data = {
            "Counterparty.getCounterparties": [{"Ref": "S1", "Description": "ФОП Тест", "City": "C0", "CityDescription": "Київ"}],
            "Counterparty.getCounterpartyContactPersons": [{"Ref": "P1", "Description": "Тест Тестович", "Phones": "380501112233"}],
            "Address.getCities": [{"Ref": "C1", "Description": "Арбузинка", "AreaDescription": "Миколаївська"}],
            "Counterparty.save": [{"Ref": "R1", "ContactPerson": {"data": [{"Ref": "RC1"}]}}],
            "InternetDocument.save": [{"Ref": "D1", "IntDocNumber": "20450000000001", "CostOnSite": 70}],
            "InternetDocument.delete": [{"Ref": "D1"}],
        }.get(m)
        if m == "AddressGeneral.getWarehouses":
            data = [{"Ref": WH, "Description": "Відділення №1: вул. Центральна, 86", "Number": "1", "CityRef": "C1",
                     "CityDescription": "Арбузинка"}]
        if m == "TrackingDocument.getStatusDocuments":
            data = [{"Number": d["DocumentNumber"], "StatusCode": self.status.get(d["DocumentNumber"], "5"),
                     "Status": "статус", "WarehouseRecipient": "Відділення №1"} for d in p["Documents"]]
        return httpx.Response(200, json={"success": True, "data": data or [], "errors": [], "warnings": []})


@pytest.fixture
def np(monkeypatch):
    fake = FakeNP()
    transport = httpx.MockTransport(fake.handler)
    real = httpx.Client
    monkeypatch.setattr(novaposhta.httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    return fake


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append((event, text)))
    return sent


def ready_sender():
    for k, v in {"np_api_key": "np-key-SECRET", "np_sender_ref": "S1", "np_contact_ref": "P1",
                 "np_sender_phone": "380501112233", "np_city_ref": "C0", "np_warehouse_ref": "W0"}.items():
        db.set_setting(k, v)


def test_draft_from_prom_order(np):
    orders.store([prom_order(1001)], notify_new=False)
    d = novaposhta.draft(1001)
    assert (d["last_name"], d["first_name"], d["middle_name"], d["phone"]) == ("Коваль", "Олена", "Іванівна", "380671112233")
    assert d["warehouse_ref"] == WH and d["city_ref"] == "C1" and d["city"] == "Арбузинка"
    assert d["cost"] == 1460.0 and d["cod"] is True and d["cod_amount"] == 1460.0 and d["payer"] == "Recipient"
    orders.store([prom_order(1002, cod=False)], notify_new=False)
    assert novaposhta.draft(1002)["cod"] is False


def test_create_ttn_payload_and_rules(np):
    orders.store([prom_order(1001), prom_order(1003, ttn="20451560371673")], notify_new=False)
    form = {**novaposhta.draft(1001)}
    with pytest.raises(novaposhta.NPError, match="Сначала подключите"):
        novaposhta.create(1001, form)
    ready_sender()
    with pytest.raises(novaposhta.NPError, match="наложенный платёж|Наложенный платёж"):
        novaposhta.create(1001, {**form, "cod": False})
    with pytest.raises(novaposhta.NPError, match="380XXXXXXXXX"):
        novaposhta.create(1001, {**form, "phone": "12345"})
    s = novaposhta.create(1001, form)
    assert s["ttn"] == "20450000000001" and s["own"] == 1 and s["stage"] == "created"
    (_, doc, key), = [c for c in np.calls if c[0] == "InternetDocument.save"]
    assert key == "np-key-SECRET"
    assert doc["RecipientAddress"] == WH and doc["CityRecipient"] == "C1" and doc["Recipient"] == "R1"
    assert doc["ContactRecipient"] == "RC1" and doc["RecipientsPhone"] == "380671112233" and doc["PayerType"] == "Recipient"
    assert doc["BackwardDeliveryData"] == [{"PayerType": "Recipient", "CargoType": "Money", "RedeliveryString": "1460.0"}]
    assert doc["Sender"] == "S1" and doc["SenderAddress"] == "W0" and doc["Cost"] == "1460"
    with pytest.raises(novaposhta.NPError, match="уже есть ТТН"):
        novaposhta.create(1001, form)
    with pytest.raises(novaposhta.NPError, match="уже есть ТТН"):  # ТТН из кабинета Prom
        novaposhta.draft(1003)


def test_api_create_sends_number_to_prom(np, client, monkeypatch):
    ready_sender()
    orders.store([prom_order(1001)], notify_new=False)
    sent = []

    def prom(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "success"})
    monkeypatch.setattr(sync, "make_client", lambda: PromClient("t", "https://x", transport=httpx.MockTransport(prom)))
    form = client.get("/api/orders/1001/ttn/draft").json()
    r = client.post("/api/orders/1001/ttn", json=form)
    assert r.status_code == 200 and r.json()["ttn"] == "20450000000001" and r.json()["prom_error"] == ""
    assert sent == [{"order_id": 1001, "declaration_id": "20450000000001", "delivery_type": "nova_poshta"}]
    item = client.get("/api/orders").json()["items"][0]
    assert item["shipment"]["ttn"] == "20450000000001" and item["nova_poshta"] is True

    # Prom не принял номер — ТТН остаётся, ошибка видна у заказа
    orders.store([prom_order(1002)], notify_new=False)
    monkeypatch.setattr(sync, "make_client", lambda: PromClient("t", "https://x", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"status": "error", "message": "Validation error"}))))
    r = client.post("/api/orders/1002/ttn", json=client.get("/api/orders/1002/ttn/draft").json()).json()
    assert r["ttn"] and "Prom не сохранил номер ТТН" in r["prom_error"]


def test_label_and_delete(np, client):
    ready_sender()
    orders.store([prom_order(1001), prom_order(1003, ttn="20451560371673")], notify_new=False)
    novaposhta.create(1001, novaposhta.draft(1001))
    r = client.get("/api/orders/1001/ttn/label.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and b"np-key-SECRET" not in r.content
    np.status = {"20450000000001": "1"}  # своя ТТН ещё не отправлена
    novaposhta.track()  # ТТН из кабинета Prom взята под отслеживание
    assert client.delete("/api/orders/1003/ttn").status_code == 400  # не наша
    assert client.delete("/api/orders/1001/ttn").status_code == 200
    assert novaposhta.shipment(1001) is None
    novaposhta.create(1001, novaposhta.draft(1001))
    np.status = {"20450000000001": "5"}  # уже в пути — удалить нельзя
    novaposhta.track()
    r = client.delete("/api/orders/1001/ttn")
    assert r.status_code == 400 and "уже отправлена" in r.json()["detail"]


def test_tracking_alerts_once_each(np, alerts, monkeypatch):
    orders.store([prom_order(2001, ttn="20451560371673", status="received"),
                  prom_order(2002, ttn="20451560371674", status="received"),
                  prom_order(2003, ttn="20451560371675", status="canceled")], notify_new=False)
    assert novaposhta.track() == 2  # отменённый заказ не отслеживаем
    assert all(c[2] == "" for c in np.calls)  # без ключа: отслеживание работает и так
    np.status = {"20451560371673": "7", "20451560371674": "103"}
    novaposhta.track()
    novaposhta.track()
    assert [e for e, _ in alerts] == ["parcel_arrived", "parcel_refused"]
    assert "2001" in alerts[0][1] and "Відділення №1" in alerts[0][1]
    # через 6 дней посылку всё ещё не забрали
    old = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat(timespec="seconds")
    with db.tx() as c:
        c.execute("UPDATE shipments SET arrived_at = ? WHERE order_id = 2001", (old,))
    novaposhta.track()
    novaposhta.track()
    assert [e for e, _ in alerts].count("parcel_waiting") == 1
    np.status["20451560371673"] = "9"
    novaposhta.track()
    calls = len(np.calls)
    novaposhta.track()  # получена и отказ — больше не проверяем
    assert len(np.calls) == calls


def test_settings_check_and_save(np, client):
    r = client.post("/api/np/check", json={"key": "  np-key-SECRET "})
    assert r.status_code == 200 and r.json()["sender"]["name"] == "ФОП Тест" and r.json()["contacts"][0]["ref"] == "P1"
    assert db.get_setting("np_api_key") == "np-key-SECRET" and "np-key-SECRET" not in r.text
    bad = client.post("/api/np/settings", json={"np_sender_phone": "123"})
    assert bad.status_code == 400
    ok = client.post("/api/np/settings", json={"np_contact_ref": "P1", "np_sender_phone": "0501112233",
                                               "np_city_ref": "C0", "np_warehouse_ref": "W0", "np_weight": "1,5"}).json()
    assert ok["ready"] and ok["np_sender_phone"] == "380501112233" and ok["np_weight"] == "1.5"
    assert client.get("/api/np/warehouses", params={"city_ref": "C1"}).json()["items"][0]["ref"] == WH
    assert client.get("/api/np/cities", params={"q": "Ар"}).json()["items"][0]["name"] == "Арбузинка"


def test_np_error_is_readable(monkeypatch):
    real = httpx.Client
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False, "errors": ["API key expired"], "data": []}))
    monkeypatch.setattr(novaposhta.httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": t}))
    with pytest.raises(novaposhta.NPError, match="Нова Пошта: API key expired"):
        novaposhta.check("x")
