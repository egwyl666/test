"""Клиент публичного API Prom.ua (https://public-api.docs.prom.ua).

Все пути и имена полей Prom собраны здесь. Если Prom что-то поменяет, править нужно только этот файл.
"""

import json

import httpx

DEFAULT_IMPORT_SETTINGS = {
    # Товары, которых нет в файле, не трогаем: выгружаем только выбранные.
    "mark_missing_product_as": "none",
    "force_update": False,
    "only_available": False,
}

# Статусы импорта: всё, что не «в процессе», считается завершённым.
IMPORT_DONE_OK = {"success", "partial"}
IMPORT_DONE_FAIL = {"fatal", "error", "failed"}


class PromError(Exception):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class PromClient:
    def __init__(self, token: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 120):
        if not token:
            raise PromError("Не указан API-токен Prom (Настройки)")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise PromError("Prom не ответил вовремя", retryable=True)
        except httpx.TransportError as exc:
            raise PromError(f"Нет связи с Prom: {exc}", retryable=True)

        code = response.status_code
        if code in (401, 403):
            raise PromError("Prom отклонил токен: проверьте токен и его права в кабинете", status=code)
        if code == 429 or code >= 500:
            raise PromError(f"Prom временно недоступен (HTTP {code})", retryable=True, status=code)
        try:
            body = response.json()
        except ValueError:
            raise PromError(f"Prom вернул не JSON (HTTP {code}): {response.text[:300]}", retryable=code >= 500, status=code)
        if code >= 400:
            raise PromError(f"Prom отклонил запрос (HTTP {code}): {_error_text(body)}", status=code)
        if isinstance(body, dict) and body.get("error"):
            raise PromError(f"Prom: {_error_text(body)}", status=code)
        return body

    async def check(self) -> dict:
        """Проверка токена: запрашиваем один товар."""
        return await self._request("GET", "/products/list", params={"limit": 1})

    async def list_products(self, limit: int = 100, last_id: int | None = None) -> dict:
        params = {"limit": limit}
        if last_id:
            params["last_id"] = last_id
        return await self._request("GET", "/products/list", params=params)

    async def list_orders(self, limit: int = 100, last_id: int | None = None, **params) -> dict:
        query = {"limit": limit, **{k: v for k, v in params.items() if v not in (None, "")}}
        if last_id:
            query["last_id"] = last_id
        return await self._request("GET", "/orders/list", params=query)

    async def set_order_status(self, ids: list[int], status: str, cancellation_reason: str = "",
                               cancellation_text: str = "") -> dict:
        """Формат — как в официальном примере Prom (company-api-example)."""
        body = {"status": status, "ids": ids}
        if cancellation_reason:
            body["cancellation_reason"] = cancellation_reason
        if cancellation_text:
            body["cancellation_text"] = cancellation_text
        return await self._request("POST", "/orders/set_status", json=body)

    async def import_file(self, content: bytes, settings: dict | None = None, filename: str = "products.xml") -> str:
        """Загружает YML-файл целиком. Фото Prom скачает сам по ссылкам <picture>."""
        body = await self._request(
            "POST",
            "/products/import_file",
            files={"file": (filename, content, "application/xml")},
            data={"data": json.dumps(settings or DEFAULT_IMPORT_SETTINGS)},
        )
        return _import_id(body)

    async def import_url(self, url: str, settings: dict | None = None) -> str:
        body = await self._request("POST", "/products/import_url", json={"url": url, **(settings or DEFAULT_IMPORT_SETTINGS)})
        return _import_id(body)

    async def import_status(self, import_id: str) -> dict:
        return await self._request("GET", f"/products/import/status/{import_id}")

    async def edit_by_external_id(self, items: list[dict]) -> dict:
        """Быстрое изменение цены/наличия уже выгруженных товаров (до 100 за раз)."""
        return await self._request("POST", "/products/edit_by_external_id", json=items)


def _import_id(body: dict) -> str:
    import_id = body.get("id") or body.get("import_id")
    if not import_id:
        raise PromError(f"Prom не вернул номер импорта: {json.dumps(body, ensure_ascii=False)[:300]}")
    return str(import_id)


def _error_text(body) -> str:
    if isinstance(body, dict):
        for key in ("error", "message", "errors", "detail"):
            if body.get(key):
                value = body[key]
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return json.dumps(body, ensure_ascii=False)[:300]


def import_state(status_body: dict) -> str:
    """'running' | 'ok' | 'failed' по ответу /products/import/status."""
    status = str(status_body.get("status", "")).lower()
    if status in IMPORT_DONE_OK:
        return "ok"
    if status in IMPORT_DONE_FAIL:
        return "failed"
    return "running"
