"""2.23.1: гонки и инварианты, которые дорого стоят в жизни — правка человека против ответа ИИ, две ТТН на один
заказ, фото «из прайса», ведущее в домашнюю сеть, секреты в диагностике и копиях, фид с «враждебным» текстом."""

import json
import logging
import socket
import threading
import zipfile
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from promloader import ai, aibulk, backup, cloudbackup, config, db, feed, netguard, novaposhta, orders, products, support
from promloader.main import app

from .conftest import make_image, sign_in
from .test_novaposhta import FakeNP, prom_order, ready_sender

# ---------- 1. правка человека, пока работал массовый ИИ ----------


def test_manual_edit_during_bulk_ai_is_not_overwritten(client):
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    pid = products.create({"name": "Старая назва", "description": "<p>Старий опис</p>", "price": 100,
                           "external_id": "RACE-1"})
    job = aibulk.create([pid], "custom", instruction="Перепиши", only_empty=False)

    def slow_ai(p, action, instruction=""):
        # ИИ «думает», а в это время человек правит название в карточке
        assert client.patch(f"/api/products/{pid}", json={"name": "Назва від людини"}).status_code == 200
        return {"name": "Назва від ШІ", "description": "<p>Опис від ШІ</p>"}

    assert aibulk.process_one(slow_ai) == "done"
    p = products.get(pid)
    assert p["name"] == "Назва від людини"            # правка человека новее — ИИ её не затёр
    assert p["description"] == "<p>Опис від ШІ</p>"   # нетронутое поле ИИ заполнил
    item = db.query_one("SELECT * FROM ai_items WHERE job_id = ?", (job["id"],))
    assert item["status"] == "done" and "Название" in item["message"]
    assert "name" not in json.loads(item["new"])      # «Откатить задание» не тронет правку человека
    last = db.query_one("SELECT source, new FROM product_changes WHERE product_id = ? AND field = 'name' "
                        "ORDER BY id DESC LIMIT 1", (pid,))
    assert last["source"] == "Вручную" and last["new"] == "Назва від людини"


def test_product_deleted_during_bulk_ai(client):
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    pid = products.create({"name": "Товар", "price": 100, "external_id": "RACE-2"})
    job = aibulk.create([pid], "keywords")

    def ai_meanwhile_deleted(p, action, instruction=""):
        with db.tx() as c:
            c.execute("DELETE FROM products WHERE id = ?", (pid,))
        return {"keywords": "ключ"}
    aibulk.process_one(ai_meanwhile_deleted)
    assert db.query_one("SELECT status, message FROM ai_items WHERE job_id = ?", (job["id"],))["status"] == "skipped"


# ---------- 2. две ТТН на один заказ ----------

class SlowNP(FakeNP):
    """Нова Пошта отвечает на «создать ТТН» не сразу — пока первый запрос висит, второй оператор жмёт «Создать»."""

    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def handler(self, request):
        if b'"InternetDocument"' in request.content and b'"save"' in request.content:
            self.entered.set()
            assert self.release.wait(10)
        return super().handler(request)


@pytest.fixture
def slow_np(monkeypatch):
    fake = SlowNP()
    transport = httpx.MockTransport(fake.handler)
    real = httpx.Client
    monkeypatch.setattr(novaposhta.httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    return fake


def test_second_ttn_for_same_order_is_refused_while_first_is_in_flight(slow_np, client):
    orders.store([prom_order(2001)], notify_new=False)
    ready_sender()
    form = novaposhta.draft(2001)
    first = {}
    t = threading.Thread(target=lambda: first.update(s=novaposhta.create(2001, form)))
    t.start()
    assert slow_np.entered.wait(10)
    operator = sign_in(TestClient(app), "op2", "пароль-1234", "operator")
    r = operator.post("/api/orders/2001/ttn", json=form)  # второй оператор — пока первая ТТН создаётся
    assert r.status_code == 409 and "уже создаётся" in r.json()["detail"]
    slow_np.release.set()
    t.join(10)
    assert first["s"]["ttn"] == "20450000000001"
    r = operator.post("/api/orders/2001/ttn", json=form)  # и после — «уже есть»
    assert r.status_code == 409 and "уже есть ТТН" in r.json()["detail"]
    saves = [c for c in slow_np.calls if c[0] == "InternetDocument.save"]
    assert len(saves) == 1  # в Новой Почте — ровно одна накладная
    assert db.query_one("SELECT COUNT(*) AS n FROM shipments WHERE order_id = 2001")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM locks")["n"] == 0  # замок снят


def test_failed_ttn_releases_the_lock(slow_np):
    orders.store([prom_order(2002)], notify_new=False)
    ready_sender()
    form = novaposhta.draft(2002)
    with pytest.raises(novaposhta.NPError):
        novaposhta.create(2002, {**form, "phone": "12345"})
    assert db.query_one("SELECT COUNT(*) AS n FROM locks")["n"] == 0


# ---------- 3. фото «из прайса» ведёт в домашнюю сеть ----------

def product_with_photo(url):
    pid = products.create({"name": "Чохол 123", "price": 100, "external_id": f"SSRF-{abs(hash(url))}"})
    with db.tx() as c:
        c.execute("INSERT INTO images (product_id, file, url, position) VALUES (?, '', ?, 0)", (pid, url))
    return products.get(pid)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/api/settings", "http://localhost/x.jpg", "http://169.254.169.254/latest/meta-data/",
    "http://192.168.1.1/admin.jpg", "http://10.0.0.5/a.jpg", "http://[::1]/a.jpg", "http://[::ffff:127.0.0.1]/a.jpg",
    "http://0.0.0.0/a.jpg", "http://printer.local/a.jpg", "file:///etc/passwd", "ftp://example.com/a.jpg",
])
def test_vision_photo_refuses_local_and_private_urls(url):
    hits = []
    transport = httpx.MockTransport(lambda r: hits.append(str(r.url)) or httpx.Response(200, content=make_image("JPEG")))
    with pytest.raises(ai.AIError):
        ai.product_photo(product_with_photo(url), transport)
    assert hits == []  # запрос даже не ушёл


def test_vision_photo_refuses_name_resolving_to_home_network(monkeypatch):
    monkeypatch.setattr(netguard.socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, 1, 6, "", ("192.168.0.10", 80))])
    hits = []
    transport = httpx.MockTransport(lambda r: hits.append(1) or httpx.Response(200, content=make_image("JPEG")))
    with pytest.raises(ai.AIError, match="домашнюю сеть"):
        ai.product_photo(product_with_photo("http://evil.example/photo.jpg"), transport)
    assert hits == []


def test_vision_photo_redirect_to_localhost_is_refused():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/api/backups"})
        return httpx.Response(200, content=make_image("JPEG"))
    with pytest.raises(ai.AIError, match="домашнюю сеть"):
        ai.product_photo(product_with_photo("http://93.184.216.34/photo.jpg"), httpx.MockTransport(handler))
    assert hits == ["http://93.184.216.34/photo.jpg"]  # по перенаправлению в 127.0.0.1 не пошли


def test_vision_photo_public_redirect_works():
    def handler(request):
        if request.url.path == "/old.jpg":
            return httpx.Response(301, headers={"location": "/new.jpg"})
        return httpx.Response(200, content=make_image("JPEG"))
    mime, data = ai.product_photo(product_with_photo("http://93.184.216.34/old.jpg"), httpx.MockTransport(handler))
    assert mime == "image/jpeg" and data


def test_vision_photo_huge_stream_stops_at_limit():
    sent = {"bytes": 0}

    def endless():
        chunk = b"\0" * 65536
        for _ in range(10_000):  # 650 МБ, если читать до конца
            sent["bytes"] += len(chunk)
            yield chunk

    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=endless()))
    with pytest.raises(ai.AIError, match="больше 4 МБ"):
        ai.product_photo(product_with_photo("http://93.184.216.34/big.jpg"), transport)
    assert sent["bytes"] <= ai.PHOTO_MAX_BYTES + 2 * 65536  # остановились сразу за пределом


def test_vision_photo_declared_huge_length_is_refused_without_reading():
    transport = httpx.MockTransport(lambda r: httpx.Response(
        200, headers={"content-length": str(500 * 1024 * 1024)}, content=b"x"))
    with pytest.raises(ai.AIError, match="больше 4 МБ"):
        ai.product_photo(product_with_photo("http://93.184.216.34/huge.jpg"), transport)


# ---------- 4. секреты не утекают ----------

CANARIES = {
    "prom_token": "CANARY-prom-7f3a9c1e5b",
    "gemini_key": "CANARY-gemini-AIza4d2b8e",
    "anthropic_key": "CANARY-claude-sk-ant-91c0",
    "np_api_key": "CANARY-np-5e6f7a8b9c",
    "r2_secret_access_key": "CANARY-r2-0a1b2c3d4e",
    "push_vapid_private": "CANARY-vapid-6d5c4b3a2f",
    "telegram_token": "1234567:CANARY-tg-AAbbCC",
    "auth_secret": "c4a7a9e1" * 8,
}


def seed_canaries(data_dir):
    for key, value in CANARIES.items():
        db.set_setting(key, value)
    from promloader import users
    service = users.service_token()
    return [*CANARIES.values(), service]


def test_secrets_never_reach_diagnostics_logs_or_cloud_copy(data_dir):
    from promloader.main import setup_log_file

    canaries = seed_canaries(data_dir)
    handler = setup_log_file(data_dir)
    log = logging.getLogger("promloader.audit")
    try:
        # так секреты попадают в журнал в жизни: текст ошибки сервиса, адрес запроса, заголовок
        log.error("Gemini ответил 400 для ключа %s", CANARIES["gemini_key"])
        log.error("Prom: Authorization: Bearer %s", CANARIES["prom_token"])
        log.warning("НП: {\"apiKey\": \"%s\"}", CANARIES["np_api_key"])
        log.warning("Значок у часов: %s", canaries[-1])
        log.warning("https://api.telegram.org/bot%s/sendMessage", CANARIES["telegram_token"])
        log.warning("R2 подпись %s, vapid %s, claude %s, вход %s", CANARIES["r2_secret_access_key"],
                    CANARIES["push_vapid_private"], CANARIES["anthropic_key"], CANARIES["auth_secret"])
        handler.flush()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    # и вывод значка у часов (console.log) — он пишется мимо фильтра журнала
    (data_dir / "logs" / "console.log").write_text("\n".join(f"ошибка: {c}" for c in canaries), encoding="utf-8")

    logs = (data_dir / "logs" / "promloader.log").read_text(encoding="utf-8")
    report = json.dumps(support.diagnostics(), ensure_ascii=False)
    for c in canaries:
        assert c not in logs, c
        assert c not in report, c
    assert "Gemini ответил 400" in logs  # само сообщение осталось — пропал только секрет

    info = backup.create("manual")
    cloud = data_dir / "cloud.zip"
    cloudbackup.strip_secrets(backup.path_of(info["name"]), cloud)
    with zipfile.ZipFile(cloud) as z:
        raw = b"".join(z.read(n) for n in z.namelist())
    for c in canaries:
        assert c.encode() not in raw, c


def test_redact_keeps_short_and_ordinary_text():
    db.set_setting("prom_token", "abc")  # слишком короткое «значение» не должно вырезать обычные слова
    assert config.redact("abc и abcdef") == "abc и abcdef"


# ---------- 5. фид с «враждебным» текстом ----------

HOSTILE = [
    "Кружка ]]> <![CDATA[ злом", "Чай & кава <b>жирно", "<script>alert('x')</script>", "Тест\x00\x01\x02\x03\x08 керування",
    "Незакритий <p><div><span>тег", "&amp; &lt; &#0; &#x1; &bogus;", "￾￿ межі", "Емодзі 😀 і   рядок",
    'Лапки "подвійні" і \'одинарні\'', "--> <!-- коментар", "<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]>&a;",
]


def test_feed_is_well_formed_with_hostile_text(data_dir):
    for n, text in enumerate(HOSTILE):
        pid = products.create({"name": f"Товар {n} {text}"[:250], "description": f"<p>{text}</p>{text}",
                               "keywords": text, "price": 100, "external_id": f"X-{n}", "vendor": text,
                               "group_name": f"Група {text}", "params": [{"name": text, "value": text}]})
        with db.tx() as c:
            c.execute("INSERT INTO images (product_id, file, url, position) VALUES (?, '', ?, 0)",
                      (pid, f"https://cdn.example.com/{n}.jpg?a=1&b=2"))
            c.execute("UPDATE products SET status = 'synced' WHERE id = ?", (pid,))
    for ids in (None, [r["id"] for r in db.query("SELECT id FROM products")]):
        xml = feed.build(ids, "https://shop.example.com")
        root = ET.fromstring(xml)  # без исключения — значит, файл корректный
        offers = root.findall("./shop/offers/offer")
        assert len(offers) == len(HOSTILE)
        names = [o.findtext("name") for o in offers]
        assert any("<script>" in n for n in names)          # текст сохранён как текст, а не как разметка
        assert all("\x00" not in n and "\x08" not in n for n in names)
        assert b"\x00" not in xml
