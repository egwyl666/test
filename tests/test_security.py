"""Защита от чужих сайтов, открытых в том же браузере."""


def test_cross_site_post_is_rejected(client):
    r = client.post("/api/restart", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403 and "не со страницы программы" in r.json()["detail"]
    r = client.post("/api/products", json={"name": "X"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_own_pages_and_local_tools_are_allowed(client):
    r = client.post("/api/products", json={"name": "Гачок"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert client.post("/api/products", json={"name": "Без Origin (значок у часов)"}).status_code == 200


def test_foreign_host_cannot_read_data(client):
    r = client.get("/api/settings", headers={"Host": "evil.example.com"})
    assert r.status_code == 403
    assert client.get("/api/settings", headers={"Host": "localhost:8765"}).status_code == 200
    # фото и фид Prom забирает по публичному адресу — их адрес не проверяется (у фида своя проверка ключа)
    assert client.get("/media/nope.jpg", headers={"Host": "photos.example.com"}).status_code == 404
    assert "ключ" in client.get("/feed/prom.yml", headers={"Host": "photos.example.com"}).json()["detail"]


def test_password_mode_allows_network_host(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "secret")
    r = client.get("/api/settings", headers={"Host": "shop.example.com"}, auth=("admin", "secret"))
    assert r.status_code == 200
